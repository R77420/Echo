"""audio.py — couche audio d'Écho : découverte des périphériques, constantes,
segmentation VAD et helpers (RMS, conversion WAV, filtre de silence).

Module pur : aucune dépendance à l'état global de transcription_consultation.py.
Tous les paramètres sont passés explicitement. Les threads de capture
(capturer_patient / capturer_medecin) restent dans le module principal pour
l'instant (trop liés à l'état global) — extraction prévue à l'étape suivante.
"""

import io

import numpy as np
import soundcard as sc
import webrtcvad


# ----------------------------- CONSTANTES ------------------------------------

SAMPLE_RATE   = 16000      # imposé par Whisper et webrtcvad
CHANNELS      = 1          # mono
FRAME_MS      = 30         # webrtcvad accepte 10/20/30 ms (= CHUNK_MS)
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000   # 480 échantillons / frame
VAD_LEVEL          = 2          # 0 (permissif) .. 3 (agressif) — niveau 2 pour audio WebRTC compressé
SILENCE_MS         = 350        # silence marquant la fin d'un tour de parole (télé)
SILENCE_MS_CABINET = 250        # cabinet : échanges plus rapides (pas de latence réseau) → couper plus tôt sépare mieux les locuteurs
MIN_SPEECH_MS      = 300        # ignore les bruits trop courts (réduit pour capturer parole courte)
MIN_SPEECH_MS_CABINET = 400     # cabinet : un vrai échange dure ≥ 0.5 s → élimine les micro-bruits brefs
RMS_MIN            = 0.008      # énergie minimale globale (assoupli)
RMS_MIN_LOOPBACK   = 0.006      # seuil loopback : audio compressé/normalisé a un RMS plus bas
RMS_MIN_MIC        = 0.015      # seuil micro (télé) : médecin près du casque, strict contre l'écho
RMS_MIN_CABINET    = 0.005      # seuil micro (cabinet) : repli si le calibrage adaptatif échoue
# Calibrage adaptatif du seuil cabinet : mesure du bruit ambiant × facteur,
# borné entre un plancher et un plafond. S'adapte au cabinet calme ou bruyant.
CABINET_CALIB_MS     = 2000     # durée de mesure du bruit ambiant en début de consultation
CABINET_RMS_FACTOR   = 2.5      # seuil = bruit_ambiant × facteur
CABINET_RMS_FLOOR    = 0.008    # plancher (cabinet très calme)
CABINET_RMS_CEIL     = 0.020    # plafond (cabinet très bruyant)
MAX_SEG_MS         = 4000       # flush forcé : un segment au moins toutes les 4 s


# ----------------------------- DÉCOUVERTE PÉRIPHÉRIQUES -----------------------
# Résolveurs robustes : renvoient None/[] plutôt que de lever une exception.

def lister_sorties():
    try:
        return sc.all_speakers()
    except Exception:
        return []


def lister_micros():
    try:
        return sc.all_microphones()
    except Exception:
        return []


def nom_sortie_defaut():
    try:
        return str(sc.default_speaker().name)
    except Exception:
        return ""


def nom_micro_defaut():
    try:
        return str(sc.default_microphone().name)
    except Exception:
        return ""


def resoudre_loopback(nom):
    """Loopback dont le nom contient `nom`, sinon None (jamais d'exception)."""
    if not nom:
        return None
    try:
        for m in sc.all_microphones(include_loopback=True):
            if nom.lower() in m.name.lower():
                return m
    except Exception:
        pass
    return None


def loopback_defaut():
    """Loopback de la sortie Windows par défaut, sinon None."""
    return resoudre_loopback(nom_sortie_defaut())


def micro_defaut():
    """Micro Windows par défaut (objet device), sinon None (jamais d'exception)."""
    try:
        return sc.default_microphone()
    except Exception:
        return None


def resoudre_micro(nom):
    """Entrée réelle dont le nom contient `nom`, sinon None."""
    if not nom:
        return None
    try:
        for m in sc.all_microphones():
            if m.name == nom or nom.lower() in m.name.lower():
                return m
    except Exception:
        pass
    return None


def loopback_par_nom(fragment):
    """Microphone-loopback dont le nom contient `fragment` (lève si introuvable)."""
    micros = sc.all_microphones(include_loopback=True)
    for m in micros:
        if fragment.lower() in m.name.lower():
            return m
    raise RuntimeError("Loopback introuvable pour '%s'. Dispo : %s"
                       % (fragment, [m.name for m in micros]))


def micro_par_nom(fragment):
    """Entrée réelle (sans loopback) dont le nom contient `fragment` (lève si introuvable)."""
    micros = sc.all_microphones()
    for m in micros:
        if m.name == fragment or fragment.lower() in m.name.lower():
            return m
    raise RuntimeError("Micro introuvable pour '%s'. Dispo : %s"
                       % (fragment, [m.name for m in micros]))


# ----------------------------- HELPERS AUDIO ---------------------------------

def rms(audio):
    """Énergie RMS d'un segment audio (numpy float). 0.0 si vide/erreur.
    Un quasi-silence (RMS faible) fait halluciner Whisper ; une vraie voix
    (« Merci » réellement prononcé) a un RMS élevé et passe normalement."""
    try:
        if audio is None or len(audio) == 0:
            return 0.0
        return float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    except Exception:
        return 0.0


def garder_si_audible(audio, seuil=RMS_MIN):
    """Filtre anti-hallucination : renvoie `audio` si son RMS >= seuil,
    sinon None (quasi-silence à ne pas envoyer à la transcription).
    Passer seuil=RMS_MIN_LOOPBACK pour le patient, RMS_MIN_MIC pour le médecin."""
    return audio if rms(audio) >= seuil else None


def audio_to_wav_bytes(audio, sample_rate=SAMPLE_RATE):
    """Convertit un numpy array float en octets WAV PCM 16 bits (en mémoire)."""
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def audio_to_wav_buffer(audio, sample_rate=SAMPLE_RATE, name="audio.wav"):
    """Convertit un numpy array en BytesIO WAV nommé (prêt pour l'API Groq :
    le client lit `.name` pour déduire le format)."""
    buf = io.BytesIO(audio_to_wav_bytes(audio, sample_rate))
    buf.seek(0)
    buf.name = name
    return buf


# ----------------------------- SEGMENTATION VAD ------------------------------

class VADSegmenter:
    """Découpe un flux audio en segments de parole (webrtcvad + silence).

    State machine pure : on lui pousse des frames mono float (longueur
    FRAME_SAMPLES) et elle renvoie un segment complété quand un tour de parole
    se termine (silence prolongé) ou que la durée max est atteinte. Les
    segments trop courts (< MIN_SPEECH_MS) sont ignorés.

    La logique de décision (`ajouter`) est séparée de la détection VAD
    (`detecter_parole`) pour être testable de façon déterministe.
    """

    def __init__(self, vad_level=VAD_LEVEL, sample_rate=SAMPLE_RATE,
                 frame_ms=FRAME_MS, silence_ms=SILENCE_MS,
                 min_speech_ms=MIN_SPEECH_MS, max_seg_ms=MAX_SEG_MS):
        self.vad = webrtcvad.Vad(vad_level)
        self.sample_rate = sample_rate
        self.silence_frames_limit = silence_ms // frame_ms
        self.min_speech_frames    = min_speech_ms // frame_ms
        self.max_frames           = max_seg_ms // frame_ms
        self.buffer = []
        self.silence_run = 0
        self.speaking = False

    def detecter_parole(self, mono):
        """Vrai si la frame (mono float) contient de la parole selon webrtcvad."""
        pcm16 = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
        return self.vad.is_speech(pcm16.tobytes(), self.sample_rate)

    def ajouter(self, is_speech, mono):
        """Met à jour la machine d'état avec une frame. Renvoie un segment
        (np.array concaténé) si un tour de parole vient de se terminer, sinon
        None. `is_speech` est fourni explicitement (testable)."""
        if is_speech:
            self.buffer.append(mono.astype(np.float32))
            self.silence_run = 0
            self.speaking = True
        elif self.speaking:
            self.buffer.append(mono.astype(np.float32))
            self.silence_run += 1

        fin_de_tour = self.speaking and self.silence_run >= self.silence_frames_limit
        trop_long   = len(self.buffer) >= self.max_frames

        segment = None
        if (fin_de_tour or trop_long) and len(self.buffer) >= self.min_speech_frames:
            segment = np.concatenate(self.buffer)
            self.buffer, self.silence_run, self.speaking = [], 0, False
        elif fin_de_tour:
            # Tour terminé mais trop court → on jette (bruit).
            self.buffer, self.silence_run, self.speaking = [], 0, False
        return segment

    def push(self, mono):
        """Détecte la parole puis met à jour l'état.
        Renvoie (segment_ou_None, is_speech) — `is_speech` sert à l'indicateur
        VAD temps réel côté appelant."""
        is_speech = self.detecter_parole(mono)
        return self.ajouter(is_speech, mono), is_speech
