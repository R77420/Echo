# -*- coding: utf-8 -*-
"""retranscription.py — mode cabinet : enregistrement continu de la consultation
et retranscription finale de l'audio complet (Whisper via Groq).

Retour terrain : en présentiel, un micro unique à distance et des segments
courts (250 ms de silence) transcrits sans contexte donnent une transcription
médiocre, alors que la télé est bonne. Le principe ici :

  1. Pendant la consultation, TOUT l'audio du micro est écrit dans un WAV
     temporaire (EnregistreurWav), en parallèle de la capture temps réel qui
     reste un simple aperçu.
  2. Au clic « Terminer », l'audio complet est prétraité (passe-haut, réduction
     de bruit légère, normalisation) puis envoyé à Whisper en un seul appel —
     ou en blocs contigus de ~10 min coupés au silence si le fichier dépasse
     la limite de l'API. Le résultat REMPLACE les segments temps réel avant
     l'attribution des locuteurs, la correction et l'extraction du compte-rendu.
  3. Le WAV est supprimé après la finalisation, sauf en MODE TEST (flag fichier
     %APPDATA%\\Echo\\enregistrement_test) où il est conservé dans
     %APPDATA%\\Echo\\enregistrements\\ pour analyse. Désactivé par défaut —
     jamais en production sans consentement.

Module pur (numpy + stdlib) : aucune dépendance à l'état global de
transcription_consultation.py, tout est passé explicitement. L'appelant garde
le fail-safe : si quoi que ce soit échoue ici, il conserve le temps réel.
"""

import datetime
import io
import os
import shutil
import tempfile
import threading
import uuid
import wave

import numpy as np

from audio import SAMPLE_RATE, audio_to_wav_buffer


# ----------------------------- CONSTANTES ------------------------------------

# Découpage : WAV 16 kHz / 16 bits mono = 32 Ko/s → 10 min ≈ 19 Mo, sous la
# limite Groq (25 Mo sur l'offre de base). Blocs CONTIGUS coupés au point le
# plus silencieux dans ±MARGE_COUPE_S autour de la frontière nominale : aucun
# mot coupé, aucun doublon (un chevauchement d'audio ferait retranscrire deux
# fois les segments longs de Whisper à cheval sur la jonction — constaté).
BLOC_S          = 600      # durée nominale d'un bloc (~10 min)
MARGE_COUPE_S   = 10       # fenêtre de recherche du silence autour de la frontière
FENETRE_COUPE_MS = 300     # durée du silence recherché (une pause de phrase)
DUREE_MIN_S     = 1.0      # en dessous : rien à retranscrire
# Limite de taille de fichier de l'API Groq Whisper : 25 Mo sur l'offre de
# base (100 Mo en offre développeur). On se cale sur la plus stricte, avec 10 %
# de marge : la durée maximale d'un bloc découle de la TAILLE, pas seulement
# de la durée (cf. bloc_max_s), et la taille envoyée est journalisée.
LIMITE_OCTETS_API = 25 * 1024 * 1024
MARGE_OCTETS      = 0.90
OCTETS_ENTETE_WAV = 44

# Garde énergétique sur la transcription finale : un segment dont l'audio est
# au niveau du bruit de fond (silence de la pièce) est une invention de
# Whisper, quel que soit son texte. Plancher = 10e percentile du RMS par
# fenêtre de 100 ms (le silence de la pièce), plafonné pour un fichier sans
# pause ; seuil = plancher × facteur.
SILENCE_FENETRE_MS   = 100
SILENCE_PERCENTILE   = 10
SILENCE_FACTEUR      = 1.3
SILENCE_PLANCHER_MAX = 0.10 / 3.0        # RMS_CIBLE / 3 : au-delà, pas de silence exploitable
SILENCE_SEUIL_MIN    = 1e-3              # pièce parfaitement silencieuse
TIMEOUT_APPEL_S = 120      # par appel API (un bloc de 10 min prend ~5 s chez Groq)
CONTEXTE_CHARS  = 150      # fin du bloc précédent injectée dans le prompt suivant

# Prétraitement (audio de micro lointain : voix faible, souffle de pièce).
RMS_CIBLE       = 0.10     # ≈ -20 dBFS : niveau de parole attendu par Whisper
PIC_MAX         = 0.95     # jamais d'écrêtage
PASSE_HAUT_HZ   = 80       # coupe le ronflement / les chocs sur le bureau
DENOISE_ACTIF   = True     # réduction de bruit spectrale légère (numpy pur)
DENOISE_PLANCHER = 0.30    # gain minimal ≈ -10 dB : léger, ne creuse pas la voix
DENOISE_N_FFT   = 512      # 32 ms à 16 kHz
DENOISE_HOP     = 256      # 50 % de recouvrement (Hann)
DENOISE_BLOC_S  = 60       # profil de bruit estimé par tranche de 60 s (s'adapte)
DENOISE_PERCENTILE = 20    # magnitude « bruit » = 20e percentile par bande

_MODELE = "whisper-large-v3"


# ----------------------------- MODE TEST / CHEMINS ---------------------------

def _dossier_echo():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Echo")


def mode_test_actif():
    """Vrai si le flag fichier %APPDATA%\\Echo\\enregistrement_test existe.
    Option cachée de diagnostic : le WAV complet est alors conservé."""
    return os.path.exists(os.path.join(_dossier_echo(), "enregistrement_test"))


def dossier_enregistrements():
    return os.path.join(_dossier_echo(), "enregistrements")


def chemin_temporaire():
    """WAV temporaire unique dans le dossier temp système."""
    return os.path.join(tempfile.gettempdir(),
                        "echo_cabinet_%s.wav" % uuid.uuid4().hex)


def conserver_pour_analyse(chemin_tmp, quand=None):
    """Mode test : déplace le WAV vers %APPDATA%\\Echo\\enregistrements\\
    (nom horodaté). Renvoie le nouveau chemin, ou l'ancien si le déplacement
    échoue (le fichier reste alors exploitable au chemin temporaire)."""
    quand = quand or datetime.datetime.now()
    dossier = dossier_enregistrements()
    try:
        os.makedirs(dossier, exist_ok=True)
        nom = "cabinet_%s.wav" % quand.strftime("%Y-%m-%d_%Hh%M%S")
        dest = os.path.join(dossier, nom)
        shutil.move(chemin_tmp, dest)
        return dest
    except Exception:
        return chemin_tmp


def supprimer(chemin):
    """Suppression best-effort du WAV temporaire (jamais d'exception)."""
    try:
        if chemin and os.path.exists(chemin):
            os.remove(chemin)
            return True
    except Exception:
        pass
    return False


def nettoyer_orphelins(journal=None):
    """Au démarrage de l'app : supprime les WAV temporaires de consultation
    (echo_cabinet_*.wav dans le dossier temp) laissés par une session
    précédente (plantage, coupure). Ne touche jamais au dossier
    enregistrements\\ du mode test. Chaque suppression est journalisée.
    Renvoie la liste des fichiers supprimés."""
    supprimes = []
    try:
        dossier = tempfile.gettempdir()
        for nom in os.listdir(dossier):
            if not (nom.startswith("echo_cabinet_") and nom.endswith(".wav")):
                continue
            chemin = os.path.join(dossier, nom)
            if supprimer(chemin):
                supprimes.append(chemin)
                if journal:
                    journal("retranscription: WAV temporaire orphelin supprimé "
                            "au démarrage : %s" % chemin)
    except Exception:
        if journal:
            journal("retranscription: nettoyage des WAV orphelins impossible")
    return supprimes


# ----------------------------- ENREGISTREMENT CONTINU ------------------------

class EnregistreurWav:
    """Écrit en continu des frames mono float32 dans un WAV PCM 16 bits.

    Appelé frame par frame depuis le thread de capture ; `fermer()` depuis le
    thread API après l'arrêt de la capture. Toute erreur d'écriture (disque
    plein…) désactive l'enregistreur sans jamais casser la capture."""

    def __init__(self, chemin, sample_rate=SAMPLE_RATE):
        self.chemin = chemin
        self.sample_rate = sample_rate
        self.n_frames = 0
        self.erreur = None
        self._lock = threading.Lock()
        self._w = wave.open(chemin, "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(sample_rate)

    @property
    def duree_s(self):
        return self.n_frames / float(self.sample_rate)

    def ecrire(self, mono):
        """Ajoute une frame (numpy float, [-1, 1]). Renvoie False si
        l'enregistreur est hors service."""
        with self._lock:
            if self._w is None or self.erreur is not None:
                return False
            try:
                pcm16 = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
                self._w.writeframes(pcm16.tobytes())
                self.n_frames += len(pcm16)
                return True
            except Exception as exc:     # disque plein, fichier verrouillé…
                self.erreur = str(exc)
                return False

    def fermer(self):
        """Clôt le fichier (idempotent). Renvoie le chemin du WAV."""
        with self._lock:
            if self._w is not None:
                try:
                    self._w.close()
                except Exception:
                    pass
                self._w = None
        return self.chemin


def charger_wav(chemin):
    """Lit un WAV PCM 16 bits mono → numpy float32 dans [-1, 1]."""
    with wave.open(chemin, "rb") as w:
        n = w.getnframes()
        sr = w.getframerate()
        raw = w.readframes(n)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    return audio, sr


# ----------------------------- PRÉTRAITEMENT ---------------------------------

def normaliser(audio, rms_cible=RMS_CIBLE, pic_max=PIC_MAX):
    """Supprime la composante continue puis ramène le niveau à `rms_cible`
    sans jamais dépasser `pic_max` (pas d'écrêtage). Un signal quasi nul est
    rendu tel quel (pas d'amplification du silence)."""
    x = np.asarray(audio, dtype=np.float32)
    if x.size == 0:
        return x
    x = x - np.float32(np.mean(x))
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    pic = float(np.max(np.abs(x)))
    if rms < 1e-5 or pic < 1e-5:
        return x
    gain = min(rms_cible / rms, pic_max / pic)
    return np.clip(x * np.float32(gain), -1.0, 1.0).astype(np.float32)


def filtre_passe_haut(audio, coupure_hz=PASSE_HAUT_HZ, sr=SAMPLE_RATE):
    """Passe-haut par FFT (rampe douce sur les 20 Hz sous la coupure : pas
    d'ondulation audible). Coupe le ronflement secteur (50 Hz) et les chocs
    sur le bureau sans toucher aux fondamentales de la voix (≥ 85 Hz)."""
    x = np.asarray(audio, dtype=np.float32)
    if x.size < 2:
        return x
    spec = np.fft.rfft(x.astype(np.float64))
    freqs = np.fft.rfftfreq(x.size, d=1.0 / sr)
    bas = max(0.0, coupure_hz - 20.0)
    gain = np.clip((freqs - bas) / max(coupure_hz - bas, 1e-6), 0.0, 1.0)
    return np.fft.irfft(spec * gain, n=x.size).astype(np.float32)


def reechantillonner(audio, sr_source, sr_cible=SAMPLE_RATE):
    """Rééchantillonne par FFT (filtre idéal, pas de repliement). Whisper
    travaille en 16 kHz : envoyer plus n'apporte rien et gonfle le fichier."""
    x = np.asarray(audio, dtype=np.float32)
    if sr_source == sr_cible or x.size < 2:
        return x
    n_cible = int(round(x.size * sr_cible / float(sr_source)))
    spec = np.fft.rfft(x.astype(np.float64))
    n_bins = n_cible // 2 + 1
    if n_bins <= spec.size:
        spec = spec[:n_bins]                       # sous-échantillonnage : on coupe le haut
    else:
        spec = np.concatenate([spec, np.zeros(n_bins - spec.size, dtype=spec.dtype)])
    y = np.fft.irfft(spec, n=n_cible) * (n_cible / float(x.size))
    return y.astype(np.float32)


def taille_wav_octets(n_samples):
    """Taille d'un WAV PCM 16 bits mono de `n_samples` échantillons."""
    return OCTETS_ENTETE_WAV + 2 * int(n_samples)


def bloc_max_s(sr=SAMPLE_RATE, limite_octets=LIMITE_OCTETS_API, bloc_s=BLOC_S,
               marge_s=MARGE_COUPE_S):
    """Durée nominale maximale d'un bloc pour que, même déplacé de `marge_s`
    sur le silence, le WAV envoyé reste sous `limite_octets` × MARGE_OCTETS."""
    duree_limite = (limite_octets * MARGE_OCTETS - OCTETS_ENTETE_WAV) / (2.0 * sr)
    return max(1.0, min(float(bloc_s), duree_limite - marge_s))


def plancher_energie(audio, sr=SAMPLE_RATE, fenetre_ms=SILENCE_FENETRE_MS,
                     percentile=SILENCE_PERCENTILE):
    """RMS du bruit de fond : `percentile` bas du RMS par fenêtre de
    `fenetre_ms`, plafonné (fichier sans aucune pause)."""
    x = np.asarray(audio, dtype=np.float32)
    n = max(1, int(sr * fenetre_ms / 1000))
    m = x.size // n
    if m < 10:
        return 0.0
    frames = x[:m * n].reshape(m, n).astype(np.float64)
    r = np.sqrt(np.mean(frames ** 2, axis=1))
    return float(min(np.percentile(r, percentile), SILENCE_PLANCHER_MAX))


def segment_silencieux(audio, debut_s, fin_s, plancher, sr=SAMPLE_RATE,
                       facteur=SILENCE_FACTEUR):
    """Vrai si l'audio de [debut_s, fin_s) est au niveau du bruit de fond :
    un texte transcrit là est inventé."""
    x = np.asarray(audio, dtype=np.float32)
    d, f = max(0, int(debut_s * sr)), min(x.size, int(fin_s * sr))
    if f <= d:
        return True
    seg = x[d:f].astype(np.float64)
    r = float(np.sqrt(np.mean(seg ** 2)))
    return r < max(plancher * facteur, SILENCE_SEUIL_MIN)


def _fenetre(n):
    return np.hanning(n + 1)[:-1].astype(np.float32)   # Hann périodique


def _stft(x, n_fft, hop):
    win = _fenetre(n_fft)
    n_frames = 1 + (x.size - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    return np.fft.rfft(x[idx] * win, axis=1), win, n_frames


def _istft(spec, win, hop, n_out):
    frames = np.fft.irfft(spec, axis=1).astype(np.float32) * win
    n_fft = win.size
    out = np.zeros(n_out, dtype=np.float32)
    wsum = np.zeros(n_out, dtype=np.float32)
    w2 = win * win
    for i in range(frames.shape[0]):
        d = i * hop
        out[d:d + n_fft] += frames[i]
        wsum[d:d + n_fft] += w2
    return out / np.maximum(wsum, 1e-6)


def _gate_spectral(x, n_fft, hop, plancher, percentile):
    """Réduction de bruit sur UN bloc : profil = percentile bas de la magnitude
    par bande (le bruit stationnaire), gain = 1 - bruit/magnitude borné à
    [plancher, 1], lissé en temps et en fréquence. Léger par construction."""
    if x.size < 2 * n_fft:
        return x
    # Marge réfléchie de chaque côté : les bords d'une STFT (fenêtre Hann
    # quasi nulle, un seul recouvrement) sont mal reconstruits — on les
    # calcule sur la marge puis on la jette.
    xp = np.pad(x, (n_fft, n_fft), mode="reflect")
    spec, win, n_frames = _stft(xp, n_fft, hop)
    mag = np.abs(spec)
    bruit = np.percentile(mag, percentile, axis=0)
    gain = 1.0 - bruit[None, :] / np.maximum(mag, 1e-9)
    gain = np.clip(gain, plancher, 1.0)
    # Lissage 3 frames × 3 bandes : évite le « musical noise » du gating brut.
    if n_frames >= 3:
        gain = (np.roll(gain, 1, axis=0) + gain + np.roll(gain, -1, axis=0)) / 3.0
    gain = (np.roll(gain, 1, axis=1) + gain + np.roll(gain, -1, axis=1)) / 3.0
    n_out = (n_frames - 1) * hop + n_fft
    y = _istft(spec * gain, win, hop, n_out)
    y = y[n_fft:n_fft + x.size]
    if y.size < x.size:      # queue non couverte par une frame entière
        y = np.concatenate([y, x[y.size:]])
    return y


def reduire_bruit(audio, sr=SAMPLE_RATE, n_fft=DENOISE_N_FFT, hop=DENOISE_HOP,
                  plancher=DENOISE_PLANCHER, percentile=DENOISE_PERCENTILE,
                  bloc_s=DENOISE_BLOC_S):
    """Réduction de bruit spectrale légère, par tranches de `bloc_s` (le profil
    de bruit suit l'ambiance) avec un contexte de recouvrement de part et
    d'autre pour ne pas créer de discontinuité aux jonctions."""
    x = np.asarray(audio, dtype=np.float32)
    n = x.size
    if n < 2 * n_fft:
        return x
    taille = int(bloc_s * sr)
    ctx = n_fft * 4
    out = np.empty(n, dtype=np.float32)
    debut = 0
    while debut < n:
        fin = min(n, debut + taille)
        d0, f0 = max(0, debut - ctx), min(n, fin + ctx)
        y = _gate_spectral(x[d0:f0], n_fft, hop, plancher, percentile)
        out[debut:fin] = y[debut - d0:debut - d0 + (fin - debut)]
        debut = fin
    return out


def pretraiter(audio, sr=SAMPLE_RATE, denoise=DENOISE_ACTIF):
    """Chaîne complète : passe-haut → réduction de bruit (optionnelle) →
    normalisation. La normalisation vient en dernier pour que le niveau final
    soit celui attendu par Whisper."""
    x = np.asarray(audio, dtype=np.float32)
    if x.size == 0:
        return x
    x = filtre_passe_haut(x, sr=sr)
    if denoise:
        x = reduire_bruit(x, sr=sr)
    return normaliser(x)


# ----------------------------- DÉCOUPAGE EN BLOCS ----------------------------

def point_de_coupe(audio, cible_s, sr=SAMPLE_RATE, marge_s=MARGE_COUPE_S,
                   fenetre_ms=FENETRE_COUPE_MS):
    """Instant (s) le plus silencieux dans [cible - marge, cible + marge] :
    centre de la fenêtre de `fenetre_ms` d'énergie minimale (RMS glissant).
    Sans marge exploitable, renvoie `cible_s`."""
    x = np.asarray(audio, dtype=np.float32)
    d = max(0, int((cible_s - marge_s) * sr))
    f = min(x.size, int((cible_s + marge_s) * sr))
    n = max(1, int(fenetre_ms * sr / 1000))
    if f - d <= n:
        return float(cible_s)
    e2 = x[d:f].astype(np.float64) ** 2
    cumul = np.concatenate([[0.0], np.cumsum(e2)])
    energies = cumul[n:] - cumul[:-n]          # énergie de chaque fenêtre
    i = int(np.argmin(energies))
    return (d + i + n / 2.0) / sr


def decouper_blocs(audio, sr=SAMPLE_RATE, bloc_s=BLOC_S, marge_s=MARGE_COUPE_S):
    """Renvoie la liste des blocs contigus (debut_s, fin_s) couvrant tout
    l'audio, chaque frontière étant déplacée sur le silence le plus proche
    (point_de_coupe). Un audio de moins d'un bloc → une seule entrée (un seul
    appel API)."""
    x = np.asarray(audio, dtype=np.float32)
    duree = x.size / float(sr)
    if duree <= bloc_s + marge_s:
        return [(0.0, duree)]
    blocs = []
    debut = 0.0
    while duree - debut > bloc_s + marge_s:
        fin = point_de_coupe(x, debut + bloc_s, sr, marge_s)
        blocs.append((debut, fin))
        debut = fin
    blocs.append((debut, duree))
    return blocs


def segments_absolus(segments, offset_s):
    """Segments Whisper (temps relatifs au bloc) → dicts en temps absolu."""
    out = []
    for s in segments:
        deb = float(s.get("start", 0.0)) + offset_s
        fin = float(s.get("end", deb)) + offset_s
        out.append({
            "debut": deb, "fin": fin,
            "texte": (s.get("text") or "").strip(),
            "no_speech_prob": s.get("no_speech_prob"),
        })
    return out


def _segments_en_dicts(response):
    """Segments verbose_json → liste de dicts (objets ou dicts selon le client)."""
    segs = getattr(response, "segments", None)
    if segs is None and isinstance(response, dict):
        segs = response.get("segments")
    out = []
    for s in segs or []:
        if isinstance(s, dict):
            out.append(s)
        else:
            out.append({
                "start": getattr(s, "start", 0.0),
                "end": getattr(s, "end", 0.0),
                "text": getattr(s, "text", ""),
                "no_speech_prob": getattr(s, "no_speech_prob", None),
            })
    return out


def _tronquer_prompt(prompt, max_octets):
    enc = prompt.encode("utf-8")
    if len(enc) > max_octets:
        return enc[:max_octets].decode("utf-8", "ignore")
    return prompt


# ----------------------------- RETRANSCRIPTION -------------------------------

def transcrire_audio_complet(audio, client, prompt="", langue="fr",
                             prompt_max_octets=880, sr=SAMPLE_RATE,
                             bloc_s=BLOC_S, marge_s=MARGE_COUPE_S,
                             timeout_s=TIMEOUT_APPEL_S, journal=None,
                             annule=None, limite_octets=LIMITE_OCTETS_API):
    """Transcrit l'audio complet via Whisper (Groq, API compatible OpenAI).

    L'audio est ramené à 16 kHz mono s'il ne l'est pas déjà. Un appel par
    bloc (un seul si l'audio tient dans un bloc ; sinon blocs contigus coupés
    au silence, la fin du bloc précédent servant de contexte au suivant). La
    durée de bloc est bornée par la limite de TAILLE de l'API (bloc_max_s) et
    la taille envoyée est journalisée. Chaque bloc a droit à UNE nouvelle
    tentative ; un bloc en échec définitif LÈVE — c'est à l'appelant de
    conserver le temps réel (fail-safe). `annule` est un callable optionnel :
    s'il renvoie True entre deux blocs, on abandonne.

    Renvoie une liste de segments {debut, fin, texte, no_speech_prob} en
    secondes absolues depuis le début de l'enregistrement, triés."""
    x = np.asarray(audio, dtype=np.float32)
    if sr != SAMPLE_RATE:
        if journal:
            journal("retranscription: rééchantillonnage %d Hz → %d Hz" % (sr, SAMPLE_RATE))
        x = reechantillonner(x, sr, SAMPLE_RATE)
        sr = SAMPLE_RATE
    bloc_s = min(bloc_s, bloc_max_s(sr, limite_octets, bloc_s, marge_s))
    blocs = decouper_blocs(x, sr, bloc_s, marge_s)
    resultats = []
    contexte = ""
    for i, (d, f) in enumerate(blocs):
        if annule is not None and annule():
            raise RuntimeError("retranscription annulée")
        morceau = x[int(d * sr):int(f * sr)]
        if morceau.size == 0:
            continue
        octets = taille_wav_octets(morceau.size)
        if octets > limite_octets:
            raise RuntimeError("bloc %d : %.1f Mo > limite API %.1f Mo"
                               % (i, octets / 1048576.0, limite_octets / 1048576.0))
        if journal:
            journal("retranscription: bloc %d/%d [%.1f-%.1f s] envoi %.2f Mo (%d Hz)"
                    % (i + 1, len(blocs), d, f, octets / 1048576.0, sr))
        p = prompt
        if contexte:
            p = contexte + " " + prompt
        p = _tronquer_prompt(p, prompt_max_octets)

        derniere = None
        for tentative in range(2):
            try:
                buf = audio_to_wav_buffer(morceau, sr, name="bloc_%02d.wav" % i)
                response = client.audio.transcriptions.create(
                    model=_MODELE,
                    file=buf,
                    language=langue,
                    prompt=p,
                    temperature=0,
                    response_format="verbose_json",
                    timeout=timeout_s,
                )
                derniere = None
                break
            except Exception as exc:       # réseau, quota, timeout…
                derniere = exc
                if journal:
                    journal("retranscription: bloc %d tentative %d échouée (%s)"
                            % (i, tentative + 1, type(exc).__name__))
        if derniere is not None:
            raise derniere

        gardes = segments_absolus(_segments_en_dicts(response), d)
        resultats.extend(gardes)
        texte_bloc = " ".join(g["texte"] for g in gardes if g["texte"])
        contexte = texte_bloc[-CONTEXTE_CHARS:] if texte_bloc else ""
        if journal:
            journal("retranscription: bloc %d/%d → %d segments"
                    % (i + 1, len(blocs), len(gardes)))

    resultats.sort(key=lambda s: s["debut"])
    return resultats
