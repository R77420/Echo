"""
Transcription temps reel d'une consultation medicale (Windows).
L'audio est transcrit via une infrastructure securisee (Groq), n'est pas
conserve apres transcription et ne sert jamais a entrainer de modeles.
Les comptes-rendus et donnees patient sont stockes en local sur le poste.

Capte simultanement :
  - le microphone du medecin
  - le son des haut-parleurs (= la voix du patient) via WASAPI loopback

Decoupe la parole aux silences (webrtcvad), transcrit chaque segment avec
faster-whisper, et affiche le texte dans une fenetre overlay toujours au premier
plan, a poser a cote de la fenetre Doctolib.

Rien ne quitte la machine : aucun appel reseau, aucun enregistrement sur disque.

------------------------------------------------------------------------------
DEPENDANCES
    pip install faster-whisper soundcard numpy webrtcvad-wheels
    (webrtcvad-wheels = meme API `import webrtcvad`, mais pre-compile pour
     Windows, donc pas besoin de Visual C++ Build Tools.)

SELECTION DU PERIPHERIQUE
    Par defaut on prend le loopback de la sortie Windows par defaut. Sur une
    machine avec routage virtuel (ex. SteelSeries Sonar), le defaut peut etre
    muet pour les medias. Au lancement, une LISTE DEROULANTE des sorties permet
    de choisir manuellement laquelle transcrire ; elle pre-selectionne
    PREFERRED_OUTPUT s'il correspond a une sortie disponible.
      - PREFERRED_OUTPUT : nom (fragment) pre-coche dans la liste. Sur cette
        machine = "Logitech PRO X" (sortie physique reelle derriere Sonar).
      - LOOPBACK_NAME : si renseigne, court-circuite la liste (mode sans GUI).
------------------------------------------------------------------------------
"""

# ── Chronomètre de démarrage ──────────────────────────────────────────────
# Mesure chaque grande étape (imports lourds, init Api, licence, fenêtre) et
# écrit %APPDATA%\Echo\demarrage.log. Coût quasi nul, laissé en permanence.
import time as _time_boot
_BOOT_T0 = _time_boot.perf_counter()
_BOOT_PREC = _BOOT_T0

def _chrono(etape):
    global _BOOT_PREC
    t = _time_boot.perf_counter()
    try:
        import os as _os
        d = _os.path.join(_os.environ.get("APPDATA", ""), "Echo")
        _os.makedirs(d, exist_ok=True)
        with open(_os.path.join(d, "demarrage.log"), "a", encoding="utf-8") as f:
            f.write("+%7.3fs (Δ %6.3fs)  %s\n" % (t - _BOOT_T0, t - _BOOT_PREC, etape))
    except Exception:
        pass
    _BOOT_PREC = t

_chrono("=== démarrage (python lancé) ===")

import ctypes
import datetime
import json
import logging
import logging.handlers
import os
import queue
import re
import subprocess
import sys
import traceback
import tempfile
import threading
import time
import tkinter as tk
import unicodedata
import urllib.request
import urllib.error
import socket
import uuid
import webbrowser
from tkinter import filedialog, messagebox, ttk
_chrono("imports stdlib + tkinter")

import numpy as np
_chrono("import numpy")
# faster_whisper N'EST PAS importé ici : 3,2 s à l'import (ctranslate2 tire
# transformers) pour un module qui ne sert qu'au fallback HORS-LIGNE.
# Import différé dans _charger_modele_local() / selftest() — mesuré : c'était
# 77 % du temps de démarrage. (Le bundle PyInstaller le garde : collect_all
# dans Echo.spec, indépendant du moment de l'import.)

import storage  # persistance : historique JSON + génération .docx (fonctions pures)
import demarrage  # lancement au démarrage de Windows (clé de registre HKCU)
_chrono("import storage + demarrage")
# Couche audio (constantes, découverte périphériques, segmentation VAD, helpers).
from audio import (
    SAMPLE_RATE, CHANNELS, FRAME_MS, FRAME_SAMPLES, VAD_LEVEL,
    SILENCE_MS, SILENCE_MS_CABINET, MIN_SPEECH_MS, MIN_SPEECH_MS_CABINET,
    RMS_MIN, RMS_MIN_LOOPBACK, RMS_MIN_MIC, RMS_MIN_CABINET,
    CABINET_CALIB_MS, CABINET_RMS_FACTOR, CABINET_RMS_FLOOR, CABINET_RMS_CEIL,
    MAX_SEG_MS,
    lister_sorties, lister_micros, nom_sortie_defaut, nom_micro_defaut,
    resoudre_loopback, loopback_defaut, resoudre_micro, micro_defaut,
    loopback_par_nom, micro_par_nom,
    rms, garder_si_audible, audio_to_wav_buffer, VADSegmenter,
)
_chrono("import audio (soundcard WASAPI + webrtcvad)")

# ----------------------------- DEBUG LOGGING ----------------------------------
# Activé uniquement si %APPDATA%\Echo\debug_mode existe (flag fichier).
# Écrit dans %APPDATA%\Echo\capture_debug.log — jamais dans l'overlay.

def _setup_debug_logger():
    _flag = os.path.join(os.environ.get("APPDATA", ""), "Echo", "debug_mode")
    if not os.path.exists(_flag):
        return logging.getLogger("echo.capture")  # logger inactif (NullHandler)
    _log_path = os.path.join(os.environ.get("APPDATA", ""), "Echo", "capture_debug.log")
    _logger = logging.getLogger("echo.capture")
    _logger.setLevel(logging.DEBUG)
    if not _logger.handlers:
        _h = logging.handlers.RotatingFileHandler(
            _log_path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        _h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        _logger.addHandler(_h)
        _logger.propagate = False
    return _logger

_caplog = _setup_debug_logger()

# ----------------------------- PARAMETRES ------------------------------------

APP_VERSION = "2.3.0"   # version courante (mise à jour auto au démarrage)
GITHUB_REPO = "R77420/Echo"

# Clé API Groq — importée depuis GROQ_KEY.py (gitignored, embarqué au build).
# Gérée par l'éditeur ; jamais affichée dans l'UI ni écrite dans les logs.
from journal_erreurs import journaliser, installer_hooks

try:
    from GROQ_KEY import GROQ_API_KEY
except Exception:
    # Sans clé, AUCUNE transcription cloud ne marchera : panne majeure,
    # toujours tracée (l'app continue pour laisser consulter l'historique).
    journaliser("GROQ_KEY.py introuvable/illisible — transcription cloud morte")
    GROQ_API_KEY = ""

MODEL_SIZE   = "large-v3-turbo"  # bench: RTF 0.95 sur CPU, meilleur que small sur vocab médical
DEVICE       = "cpu"       # "cuda" si carte NVIDIA disponible
COMPUTE_TYPE = "int8"      # "int8" sur CPU, "float16" sur GPU
LANGUAGE     = "fr"

# Noms des modèles (plus embarqués dans l'exe — téléchargés dans %APPDATA%\Echo\models\)
# Un seul modèle pour tout le monde : large-v3-turbo (beam=1 → rapide même sur CPU faible).
MODELE_WHISPER_DIR  = "faster-whisper-large-v3-turbo"
MODELE_EMBARQUE     = MODELE_WHISPER_DIR   # compatibilité ancienne constante

# Selection du loopback a transcrire (voix patient).
#   PREFERRED_OUTPUT : pre-coche dans la liste deroulante (None = sortie defaut).
#   LOOPBACK_NAME    : force sans afficher la liste (None = afficher la liste).
PREFERRED_OUTPUT = "Logitech PRO X"   # machine de dev avec Sonar -> sortie physique
LOOPBACK_NAME    = None
MIC_NAME         = None    # force le micro sans GUI (None = afficher la liste)

# Constantes audio (SAMPLE_RATE, FRAME_MS, VAD_LEVEL, SILENCE_MS, MIN_SPEECH_MS,
# RMS_MIN, MAX_SEG_MS, FRAME_SAMPLES, CHANNELS) importées depuis audio.py.

stop_event = threading.Event()
segment_queue = queue.Queue()
display_queue = queue.Queue()

# ---- Gains de volume (thread-safe via GIL + lock en écriture) ----
_GAIN_PATIENT = 1.0
_GAIN_MIC     = 1.0
_gain_lock    = threading.Lock()

def _get_gain(label):
    return _GAIN_PATIENT if label == "Patient" else _GAIN_MIC


# ---- État VAD temps réel (qui parle maintenant) ----
# Lecture/écriture de booléens simples : atomique sous GIL, pas de lock requis.
_speaking_now = {"medecin": False, "patient": False, "conversation": False}

# Pendant la consultation de DÉMONSTRATION : collecte des RMS des segments
# micro pour rendre un verdict micro en fin de démo (remplace le test 5 s —
# on ne fait pas relire une phrase au médecin). Activé par Api.set_demo_mode.
_demo_capture = {"actif": False, "rms": []}


def _set_speaking(label, is_speech):
    if label == "Patient":
        cle = "patient"
    elif label == "Conversation":
        cle = "conversation"
    else:
        cle = "medecin"
    _speaking_now[cle] = bool(is_speech)


# ----------------------------- CONFIG PERSISTANTE ----------------------------
# Principe : ne JAMAIS planter. Toute erreur de config est avalee -> defauts.

def dossier_config():
    """Dossier de configuration : %APPDATA%\\Echo (cree au besoin)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Echo")


def chemin_config():
    return os.path.join(dossier_config(), "config.json")


def charger_config():
    """Lit la config. Absente / illisible / corrompue -> {} (jamais d'erreur)."""
    try:
        with open(chemin_config(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}          # premier lancement : pas encore de config, normal
    except Exception:
        # Config corrompue : tous les réglages semblent « perdus » — tracer.
        journaliser("charger_config: config.json illisible")
        return {}


def sauver_config(cfg):
    """Ecrit la config en best-effort. N'echoue jamais bruyamment."""
    try:
        os.makedirs(dossier_config(), exist_ok=True)
        with open(chemin_config(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        # Réglages silencieusement perdus sinon (disque plein, droits…).
        journaliser("sauver_config: écriture config.json impossible")


def chemin_consultations():
    return os.path.join(dossier_config(), "consultations.json")


def chemin_patients():
    return os.path.join(dossier_config(), "patients.json")


# L'historique (consultations.json) et la génération .docx sont gérés par le
# module storage.py (fonctions pures + tests). Voir storage.{charger,ajouter,
# supprimer}_consultation, maj_consultation_resume, ecrire_docx, ecrire_txt_secours.


# ----------------------------- BARRE DE TITRE (Windows 11) -------------------
# Personnalise la couleur de la titlebar pour s'accorder au thème (DWM).
# Pur ctypes : aucune dépendance (FindWindowW au lieu de win32gui). Silencieux
# si l'API n'existe pas (Windows 10) ou si la fenêtre n'est pas trouvée.

# Couleurs de titlebar par thème : (fond, texte).
_TITLEBAR_COLORS = {
    "light": ("#FFFFFF", "#14302A"),
    "dark":  ("#0A1714", "#F4F7F5"),
}
_TITRE_FENETRE = "Écho"


def _hex_to_colorref(hex_color):
    """#RRGGBB → COLORREF (0x00BBGGRR), entier."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


def _trouver_fenetre(window_handle, titre):
    """HWND fourni, sinon recherche par titre via user32 (ctypes pur).
    restype = c_void_p pour ne pas tronquer le handle sur Windows 64 bits."""
    if window_handle:
        return window_handle
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype  = ctypes.c_void_p
        user32.FindWindowW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        return user32.FindWindowW(None, titre)
    except Exception:
        return None


def _dwm_set_attr(hwnd, attribut, valeur_uint):
    """DwmSetWindowAttribute(hwnd, attribut, &valeur, 4). Renvoie True si OK."""
    dwm = ctypes.windll.dwmapi
    dwm.DwmSetWindowAttribute.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    val = ctypes.c_uint(valeur_uint)
    res = dwm.DwmSetWindowAttribute(
        ctypes.c_void_p(hwnd), ctypes.c_uint(attribut),
        ctypes.byref(val), ctypes.sizeof(val))
    return res == 0


def set_titlebar_color(hex_color, window_handle=None, titre=_TITRE_FENETRE):
    """Applique une couleur personnalisée à la titlebar Windows 11
    (DWMWA_CAPTION_COLOR = 35). Ne plante jamais."""
    try:
        hwnd = _trouver_fenetre(window_handle, titre)
        if not hwnd:
            return
        _dwm_set_attr(hwnd, 35, _hex_to_colorref(hex_color))  # DWMWA_CAPTION_COLOR
    except Exception:
        pass


def set_titlebar_text_color(hex_color, window_handle=None, titre=_TITRE_FENETRE):
    """Couleur du texte de la titlebar (DWMWA_TEXT_COLOR = 36), pour le
    contraste (texte foncé sur barre claire, clair sur barre sombre)."""
    try:
        hwnd = _trouver_fenetre(window_handle, titre)
        if not hwnd:
            return
        _dwm_set_attr(hwnd, 36, _hex_to_colorref(hex_color))  # DWMWA_TEXT_COLOR
    except Exception:
        pass


def appliquer_titlebar_theme(theme, window_handle=None, titre=_TITRE_FENETRE):
    """Accorde la titlebar (fond + texte) au thème Écho ('light'/'dark')."""
    fond, texte = _TITLEBAR_COLORS.get(
        "dark" if theme == "dark" else "light", _TITLEBAR_COLORS["light"])
    set_titlebar_color(fond, window_handle, titre)
    set_titlebar_text_color(texte, window_handle, titre)


def message_simple(titre, message, genre="info"):
    """Affiche un message court (FR simple) sans dependre d'une fenetre existante."""
    try:
        r = tk.Tk()
        r.withdraw()
        r.attributes("-topmost", True)
        (messagebox.showerror if genre == "error" else messagebox.showinfo)(
            titre, message, parent=r)
        r.destroy()
    except Exception:
        pass


# Découverte/sélection des périphériques : déléguée à audio.py
# (lister_sorties, lister_micros, resoudre_loopback, resoudre_micro, etc.).


def choisir_peripheriques():
    """Fenetre de reglages -> (loopback, micro_ou_None, dossier_sauvegarde) ou None.

    - Reglages persistants : pre-selection depuis config.json, ecriture au clic
      "Demarrer".
    - Robustesse (jamais de crash) :
        * aucune sortie audio  -> message clair + None (abandon propre) ;
        * aucun micro          -> on continue sans micro (patient seul) ;
        * peripherique memorise absent -> repli silencieux sur le defaut.
    - Mode dev : LOOPBACK_NAME / MIC_NAME court-circuitent la liste.
    """
    cfg = charger_config()
    dossier_sauv = cfg.get("dossier_sauvegarde")

    # Mode dev : constantes forcees (court-circuite la GUI si la sortie resout).
    if LOOPBACK_NAME or MIC_NAME:
        lb = resoudre_loopback(LOOPBACK_NAME) if LOOPBACK_NAME else loopback_defaut()
        mc = resoudre_micro(MIC_NAME) if MIC_NAME else None
        if lb is not None:
            return lb, mc, dossier_sauv

    sorties = lister_sorties()
    if not sorties:
        message_simple(
            "Aucune sortie audio",
            "Aucune sortie audio n'a été détectée sur cet ordinateur.\n\n"
            "Branchez un casque ou des haut-parleurs, puis relancez Écho.",
            genre="error")
        return None

    noms_sorties = [str(s.name) for s in sorties]
    noms_micros = [str(m.name) for m in lister_micros()]

    # Pre-selection sortie : config -> PREFERRED_OUTPUT -> defaut systeme.
    presel_sortie = None
    if cfg.get("sortie") in noms_sorties:
        presel_sortie = cfg["sortie"]
    elif PREFERRED_OUTPUT:
        for n in noms_sorties:
            if PREFERRED_OUTPUT.lower() in n.lower():
                presel_sortie = n
                break
    if not presel_sortie:
        d = nom_sortie_defaut()
        presel_sortie = d if d in noms_sorties else noms_sorties[0]

    # Pre-selection micro : config -> defaut systeme -> premier dispo.
    presel_micro = ""
    if noms_micros:
        if cfg.get("micro") in noms_micros:
            presel_micro = cfg["micro"]
        else:
            d = nom_micro_defaut()
            presel_micro = d if d in noms_micros else noms_micros[0]

    choix = {"sortie": presel_sortie, "micro": presel_micro, "valide": False}

    dlg = tk.Tk()
    dlg.title("Écho — réglages")
    dlg.configure(bg="#0d1117")
    dlg.attributes("-topmost", True)
    dlg.geometry("660x320+60+60")

    # --- Sortie (patient), libelle non-technique ---
    tk.Label(dlg, text="Ce que vous entendez (patient)",
             bg="#0d1117", fg="#79c0ff", font=("Segoe UI", 12, "bold")
             ).pack(anchor="w", padx=22, pady=(24, 4))
    var_sortie = tk.StringVar(value=presel_sortie)
    ttk.Combobox(dlg, textvariable=var_sortie, values=noms_sorties,
                 state="readonly", width=72).pack(anchor="w", padx=22)

    # --- Micro (medecin), libelle non-technique ---
    tk.Label(dlg, text="Votre micro (médecin)",
             bg="#0d1117", fg="#7ee787", font=("Segoe UI", 12, "bold")
             ).pack(anchor="w", padx=22, pady=(18, 4))
    var_micro = tk.StringVar(value=presel_micro if noms_micros else "(aucun micro détecté)")
    combo_micro = ttk.Combobox(
        dlg, textvariable=var_micro,
        values=noms_micros if noms_micros else ["(aucun micro détecté)"],
        state="readonly", width=72)
    combo_micro.pack(anchor="w", padx=22)
    if not noms_micros:
        combo_micro.configure(state="disabled")
        tk.Label(dlg, text="Aucun micro branché : seule la voix du patient sera transcrite.",
                 bg="#0d1117", fg="#d29922", font=("Segoe UI", 9)
                 ).pack(anchor="w", padx=22, pady=(4, 0))

    def valider():
        choix["sortie"] = var_sortie.get()
        choix["micro"] = var_micro.get() if noms_micros else ""
        choix["valide"] = True
        dlg.destroy()

    tk.Button(dlg, text="Démarrer", command=valider,
              font=("Segoe UI", 11, "bold")).pack(pady=20)
    dlg.bind("<Return>", lambda _e: valider())
    dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)   # croix = ne pas demarrer
    dlg.mainloop()

    if not choix["valide"]:
        return None

    # Resolution avec repli sur le defaut si le peripherique a disparu.
    loopback = resoudre_loopback(choix["sortie"]) or loopback_defaut()
    if loopback is None:
        message_simple(
            "Sortie audio indisponible",
            "La sortie audio choisie n'est plus disponible.\n\nRelancez Écho.",
            genre="error")
        return None
    micro = resoudre_micro(choix["micro"]) if choix["micro"] else None

    # Persistance (best-effort) des choix.
    cfg["sortie"] = choix["sortie"]
    cfg["micro"] = choix["micro"]
    sauver_config(cfg)

    return loopback, micro, dossier_sauv


# ----------------------------- CAPTURE + VAD ---------------------------------

def _calibrer_seuil_cabinet(rec, seuil_defaut):
    """Mesure le bruit ambiant sur ~CABINET_CALIB_MS et renvoie un seuil RMS
    adaptatif = bruit_ambiant × facteur, borné [plancher, plafond].
    Le médecin ne parle pas encore : on capte le silence de la pièce."""
    n_frames = max(1, CABINET_CALIB_MS // FRAME_MS)
    energies = []
    for _ in range(n_frames):
        if stop_event.is_set():
            break
        try:
            data = rec.record(numframes=FRAME_SAMPLES)
        except Exception:
            break
        mono = data[:, 0] if data.ndim > 1 else data
        energies.append(rms(mono))
    if not energies:
        return seuil_defaut
    ambiant = float(np.median(energies))   # médiane : robuste aux pics parasites
    seuil = ambiant * CABINET_RMS_FACTOR
    seuil = max(CABINET_RMS_FLOOR, min(CABINET_RMS_CEIL, seuil))
    _caplog.debug("[CONVERSATION] bruit ambiant médian=%.4f → seuil=%.4f", ambiant, seuil)
    return seuil


def capturer(source_factory, label):
    """Capture d'une source + segmentation par silence (webrtcvad).

    Robuste : tout probleme (peripherique absent, debranche en cours, erreur
    quelconque) coupe proprement CE flux avec un message simple, sans tuer
    l'autre flux ni l'application.
    """
    is_loopback = (label == "Patient")
    if label == "Patient":
        libelle, rms_seuil = "patient", RMS_MIN_LOOPBACK
    elif label == "Conversation":
        # Mode cabinet : micro seul, deux locuteurs à distance variable →
        # seuil plus permissif que le mic télé (médecin près du casque).
        libelle, rms_seuil = "conversation", RMS_MIN_CABINET
    else:
        libelle, rms_seuil = "médecin", RMS_MIN_MIC

    try:
        mic = source_factory()
    except Exception:
        mic = None
    if mic is None:
        display_queue.put(("AVIS",
            "La capture (%s) n'a pas pu démarrer. L'application reste utilisable." % libelle))
        return

    # Cabinet : silence de fin de tour plus court → sépare mieux deux locuteurs
    # qui enchaînent vite ; durée min plus longue → élimine les micro-bruits.
    is_cabinet = (label == "Conversation")
    silence_ms    = SILENCE_MS_CABINET if is_cabinet else SILENCE_MS
    min_speech_ms = MIN_SPEECH_MS_CABINET if is_cabinet else MIN_SPEECH_MS
    segmenteur = VADSegmenter(silence_ms=silence_ms, min_speech_ms=min_speech_ms)
    _caplog.debug("[%s] thread démarré — seuil RMS=%.4f silence=%dms min_speech=%dms",
                  label.upper(), rms_seuil, silence_ms, min_speech_ms)

    try:
        with mic.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=FRAME_SAMPLES) as rec:
            # Cabinet : calibrage adaptatif du seuil RMS sur le bruit ambiant des
            # ~2 premières secondes (le médecin ne parle pas encore). S'adapte au
            # cabinet calme comme bruyant.
            if is_cabinet:
                rms_seuil = _calibrer_seuil_cabinet(rec, rms_seuil)
                _caplog.debug("[CONVERSATION] seuil RMS adaptatif=%.4f", rms_seuil)
            while not stop_event.is_set():
                try:
                    data = rec.record(numframes=FRAME_SAMPLES)
                except Exception:
                    display_queue.put(("AVIS",
                        "La capture (%s) s'est interrompue (périphérique débranché ?). "
                        "L'autre source continue normalement." % libelle))
                    return
                mono = data[:, 0] if data.ndim > 1 else data
                gain = _get_gain(label)
                if gain != 1.0:
                    mono = np.clip(mono * gain, -1.0, 1.0, out=mono.copy())

                frame_rms = rms(mono)
                segment, is_speech = segmenteur.push(mono)
                _caplog.debug("[%s VAD] is_speech=%s rms=%.4f", label.upper(), is_speech, frame_rms)
                _set_speaking(label, is_speech)
                if segment is not None:
                    seg_rms = rms(segment)
                    # Démo : mémoriser le niveau des segments micro (pas le
                    # loopback) pour le verdict micro de fin de découverte.
                    if _demo_capture["actif"] and label != "Patient":
                        _demo_capture["rms"].append(float(seg_rms))
                    audible = garder_si_audible(segment, seuil=rms_seuil)
                    _caplog.debug(
                        "[%s] segment dur=%.2fs rms=%.4f keep=%s",
                        label.upper(),
                        len(segment) / SAMPLE_RATE,
                        seg_rms,
                        "OUI" if audible is not None else "NON (RMS trop bas)",
                    )
                    if audible is not None:
                        segment_queue.put((label, segment))
    except Exception:
        journaliser("capturer(%s): démarrage de la capture impossible" % label)
        display_queue.put(("AVIS",
            "Impossible de démarrer la capture (%s). Vérifiez le périphérique audio." % libelle))
    finally:
        _set_speaking(label, False)


# ----------------------------- MODELE / RESSOURCES ---------------------------

def ressource(rel):
    """Chemin d'une ressource, que l'appli soit en dev ou gelee (PyInstaller).
    En mode frozen, les --add-data sont extraits dans sys._MEIPASS."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def models_dir():
    """Dossier runtime des modeles telecharges : APPDATA/Echo/models/"""
    return os.path.join(dossier_config(), "models")


def whisper_ok():
    """Vérifie que le modèle Whisper est prêt (dossier + model.bin > 1 Go)."""
    d = os.path.join(models_dir(), MODELE_WHISPER_DIR)
    f = os.path.join(d, "model.bin")
    try:
        return os.path.isdir(d) and os.path.isfile(f) and os.path.getsize(f) > 1_000_000_000
    except OSError:
        return False


def chemin_modele():
    """Résolution du chemin Whisper :
      - frozen + modele dans APPDATA : chemin APPDATA (installeur leger)
      - dev : dossier local du projet (pas de rupture du workflow dev)
      - fallback : MODEL_SIZE (telechargement HF)
    """
    # 1. APPDATA (runtime, frozen ou dev après téléchargement)
    if whisper_ok():
        return os.path.join(models_dir(), MODELE_WHISPER_DIR)
    # 2. Dev : dossier local dans le projet
    if not getattr(sys, "frozen", False):
        local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             MODELE_WHISPER_DIR)
        if os.path.isdir(local) and os.path.isfile(os.path.join(local, "model.bin")):
            return local
    # 3. Fallback : nom MODEL_SIZE → faster-whisper télécharge via HF
    return MODEL_SIZE


# ----------------------------- RÉSUMÉ (Groq LLM) ------------------------------
# Moteur unique : Groq LLM (~3 s, Llama 70B). Si indisponible → pas de résumé,
# le compte-rendu reste sauvegardé avec la transcription complète.

_chrono("corps du module (jusqu'à resume/correction)")
from resume import (groq_summarize, ENTETE_RESUME,
                    extraire_elements_cr, elements_vers_resume, elements_vides)
import correction
_chrono("import resume + correction")


# ----------------------------- PARAMÈTRES WHISPER ----------------------------

# Lexique médical condensé (~700 chars) pour guider Whisper. Tenu sous ~730
# caractères afin que, combiné au contexte dynamique (~150), le prompt reste
# sous la limite Groq de 896 octets UTF-8. Priorité : médicaments les plus
# prescrits en France (sans doublons marque/générique), symptômes et examens
# courants, termes de consultation. (Pas de médicaments rares ni de spécialités.)
WHISPER_INITIAL_PROMPT = (
    # Phrase d'ancrage prioritaire — agit comme dictionnaire pour Whisper.
    # Répéter les noms en tête du prompt maximise leur reconnaissance.
    "Consultation médicale française. Termes exacts : "
    "Doliprane, Amoxicilline, Ibuprofène, Oméprazole, "
    "Cortancyl, Ventoline, Levothyrox, Metformine, Bisoprolol, Ramipril. "
    # Paires marque/générique : le contexte entre parenthèses ancre le mot.
    "Médicaments : Doliprane (paracétamol), Amoxicilline (Clamoxyl), "
    "Ibuprofène (Advil, Nurofen), Oméprazole (Mopral), Metformine (Glucophage), "
    "Tramadol, Cortancyl, Ventoline, Levothyrox, bisoprolol, ramipril, "
    "atorvastatine, Zolpidem, sertraline. "
    # Examens courants
    "Examens : NFS, glycémie, HbA1c, TSH, CRP, créatinine, "
    "ECG, échographie, scanner, IRM, ECBU, SpO2, tension artérielle. "
    # Termes de consultation
    "Termes : ordonnance, renouvellement, arrêt de travail, "
    "antécédents, allergie, posologie, mutuelle. "
    # Symptômes
    "Symptômes : fièvre, toux, céphalées, dyspnée, nausées, vertiges, "
    "fatigue, palpitations."
)

# Limite de prompt imposée par l'API Groq (whisper-large-v3) : 896 « caractères »
# comptés en octets UTF-8. On vise une marge sous cette limite.
GROQ_PROMPT_MAX_BYTES = 880

# Dictionnaire de corrections post-transcription.
# Clés : erreurs connues du modèle small en français médical (insensibles à la casse).
# Valeurs : orthographe correcte.
# Facile à étendre : ajouter une ligne "erreur": "correction".
_CORRECTIONS_RAW = {
    "dolifren":       "Doliprane",
    "dolipren":       "Doliprane",
    "doliprant":      "Doliprane",
    "ibuprofène":     "ibuprofène",   # normalise les variantes sans accent
    "ibuprofen":      "ibuprofène",
    "amoxiciline":    "amoxicilline",
    "la fière":       "la fièvre",
    "une fière":      "une fièvre",
    "de fière":       "de fièvre",
    "ventoline":      "Ventoline",
    "levothyroxine":  "lévothyroxine",
    "metformin":      "metformine",
    "oeudeme":        "œdème",
    "oedème":         "œdème",
    "dynspnée":       "dyspnée",
    "dyspné":         "dyspnée",
    "palitations":    "palpitations",
    "palpation":      "palpitations",  # ambiguïté fréquente à l'oral
    "tension":        "tension",       # placeholder — ne remplace pas, sert de doc
}

# On compile une table { minuscules: correction } pour la recherche rapide.
_CORRECTIONS = {k.lower(): v for k, v in _CORRECTIONS_RAW.items()
                if k.lower() != v.lower()}  # ignore les no-ops


def corriger_transcription(texte):
    """Applique les corrections post-transcription (orthographe médicale).
    Remplacement mot-à-mot insensible à la casse, respecte la ponctuation."""
    if not texte or not _CORRECTIONS:
        return texte
    # Tokenise en préservant les séparateurs, remplace mot par mot.
    tokens = re.split(r'(\W+)', texte)
    result = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        tl = t.lower()
        # Essaie d'abord les bi-grammes (ex. "la fièvre").
        if i + 2 < len(tokens):
            bigram = (t + tokens[i + 1] + tokens[i + 2]).lower()
            if bigram in _CORRECTIONS:
                result.append(_CORRECTIONS[bigram])
                i += 3
                continue
        if tl in _CORRECTIONS:
            result.append(_CORRECTIONS[tl])
        else:
            result.append(t)
        i += 1
    return "".join(result)


# ----------------------------- TRANSCRIPTION ---------------------------------

# Contexte dynamique : dernières transcriptions, injectées dans le prompt Groq
# pour améliorer la cohérence (noms, médicaments répétés…). Thread-safe.
_dernier_contexte = ""
_contexte_lock    = threading.Lock()


def _maj_contexte(texte):
    """Ajoute `texte` au contexte glissant (300 derniers caractères)."""
    global _dernier_contexte
    with _contexte_lock:
        _dernier_contexte = (_dernier_contexte + " " + texte).strip()[-300:]


def _reset_contexte():
    global _dernier_contexte
    with _contexte_lock:
        _dernier_contexte = ""


# RMS / filtre de silence : audio.rms() et audio.garder_si_audible() (importés).


# Hallucinations connues de Whisper — patterns découverts en test terrain.
# Règle : zéro faux positif, donc on n'ajoute que ce qui est impossible
# dans une vraie consultation médicale.
PATTERNS_HALLUCINATION_SOUS_TITRAGE = [
    # Sous-titrage générique
    "sous-titre", "sous-titrage", "sous titré", "amara.org",
    "réalisé par la communauté", "transcription par",
    # Transitions de remplissage Whisper découvertes en test
    "et d'autres", "et maladies", "et douleurs",
    "et lesquels", "et plus de choses",
    "et moi.", "et la vie.",
]

# Verbes conjugués courants : leur absence dans une longue énumération
# (≥ 3 virgules) trahit un charabia sans structure de phrase (hallucination
# française en liste de mots). Formes accentuées ET non accentuées.
_VERBES_COURANTS = {
    "est", "sont", "a", "ont", "ai", "as", "avez", "avons",
    "suis", "es", "êtes", "etes", "va", "vais", "allez", "vont",
    "fait", "fais", "faites", "prend", "prends", "prenez",
    "peut", "peux", "pouvez", "faut", "dois", "doit", "devez",
    "ressens", "ressent", "avale", "pique", "examine", "vois", "voit",
    "prescris", "prescrit", "était", "etait", "avait", "veux", "veut",
}


def est_hallucination_generique(texte):
    """Vrai si le texte est une hallucination Whisper connue.

    Filtres :
    1. Pattern exact (liste PATTERNS_HALLUCINATION_SOUS_TITRAGE)
    2. Segment court (≤ 4 mots) commençant par « et » → transition de remplissage
    3. Segment d'un seul mot ou ponctuation seule → bruit
    4. Mot unique répété 3+ fois → boucle Whisper
    5. Bascule anglaise (≥ 2 mots anglais courants) → hallucination sur bruit/silence
    """
    t = (texte or "").strip().lower()
    if not t:
        return True
    # Filtre 1 : patterns connus
    if any(p in t for p in PATTERNS_HALLUCINATION_SOUS_TITRAGE):
        return True
    mots = t.split()
    # Filtre 2 : ≤ 4 mots et commence par "et " → hallucination de transition
    if len(mots) <= 4 and t.startswith("et "):
        return True
    # Filtre 3 : un seul mot (ou ponctuation seule) → bruit
    if len(mots) <= 1:
        return True
    # Filtre 4 : mot unique répété 3+ fois → boucle Whisper
    if len(set(mots)) == 1 and len(mots) >= 3:
        return True
    # Filtre 5 : bascule en anglais (Whisper hallucine en anglais sur du bruit)
    if correction.contient_bascule_anglaise(t):
        return True
    # Filtre 6 : énumération sans verbe (≥ 3 virgules et aucun verbe conjugué
    # courant) → charabia français probable. Les vraies énumérations médicales
    # ont un verbe ou moins de virgules ; en cas de doute on garde (le filet
    # « [?] » de l'attribution attrape le reste).
    if t.count(",") >= 3:
        mots_alpha = set(re.findall(r"[a-zàâäéèêëïîôöùûüç]+", t))
        if not (mots_alpha & _VERBES_COURANTS):
            return True
    return False


def _init_cloud_client():
    """Initialise le client Groq (API compatible OpenAI, modèle whisper
    large-v3-turbo). Clé gérée par l'éditeur (GROQ_API_KEY). Renvoie le client
    ou None si la clé ou la librairie est indisponible."""
    if not GROQ_API_KEY:
        return None
    try:
        import openai
        return openai.OpenAI(api_key=GROQ_API_KEY,
                             base_url="https://api.groq.com/openai/v1")
    except Exception:
        # Client cloud indisponible = plus de transcription : tracer.
        journaliser("_init_cloud_client: création du client Groq impossible")
        return None


def modele_local_present():
    """Vrai si le modèle Whisper local (filet hors-ligne) est disponible
    (APPDATA après téléchargement, ou dossier local en dev)."""
    if whisper_ok():
        return True
    if not getattr(sys, "frozen", False):
        local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             MODELE_WHISPER_DIR)
        if os.path.isdir(local) and os.path.isfile(os.path.join(local, "model.bin")):
            return True
    return False


def _charger_modele_local():
    """Charge le modèle Whisper local. Renvoie le modèle ou None.
    À n'appeler que si modele_local_present() est vrai (évite tout
    téléchargement HF implicite)."""
    try:
        from faster_whisper import WhisperModel   # import différé (3,2 s)
        return WhisperModel(chemin_modele(), device=DEVICE, compute_type=COMPUTE_TYPE)
    except Exception:
        # Filet hors-ligne mort alors que le modèle est censé être présent.
        journaliser("_charger_modele_local: chargement Whisper local impossible")
        return None


# Filtre de confiance Whisper : au-delà de ce no_speech_prob, le segment est
# du bruit/silence halluciné. Parole réelle mesurée ≈ 0.002 (marge ×250) ;
# les hallucinations « cohérentes » captées sur un blanc se situent souvent
# en zone 0.4-0.6 → seuil resserré à 0.5 après test terrain. Chaque segment
# accepté/rejeté journalise son no_speech_prob pour affinage.
NO_SPEECH_MAX = 0.5

# ----------------------------- TEST MICROPHONE -------------------------------
# Phrase de référence lue par le médecin à ~1 m du micro (distance patient).
PHRASE_TEST_MICRO = ("Bonjour, je viens vous voir car j'ai mal à la gorge "
                     "depuis trois jours.")
# Mots-clés attendus pour juger la complétude de la transcription.
_MOTS_TEST_MICRO = {"bonjour", "viens", "voir", "mal", "gorge",
                    "depuis", "trois", "jours"}
# Seuils RMS (cohérents avec la capture : mic télé strict / cabinet permissif).
MIC_TEST_RMS_OK    = 0.015   # au-dessus : adapté au cabinet
MIC_TEST_RMS_FAIBLE = 0.006  # entre les deux : capte mais faiblement


def _qualite_transcription_test(texte):
    """Ratio [0,1] de mots-clés attendus retrouvés dans la transcription."""
    t = unicodedata.normalize("NFKD", (texte or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    mots = set(re.findall(r"[a-z]+", t))
    if not mots:
        return 0.0
    return len(mots & _MOTS_TEST_MICRO) / len(_MOTS_TEST_MICRO)


def verdict_micro(rms, texte):
    """Trois verdicts à partir du RMS moyen et de la transcription obtenue :
      'insuffisant' : RMS < 0.006 OU aucune transcription
      'adapte'      : RMS > 0.015 ET transcription correcte
      'faible'      : entre les deux (capte mais faiblement / partiel)."""
    q = _qualite_transcription_test(texte)
    if rms < MIC_TEST_RMS_FAIBLE or q == 0.0:
        return "insuffisant"
    if rms > MIC_TEST_RMS_OK and q >= 0.6:
        return "adapte"
    return "faible"


def _transcrire_local(model, audio):
    """Transcrit un segment avec le modèle local (beam=1).
    Renvoie (texte, no_speech_prob|None)."""
    segments, _ = model.transcribe(
        audio, language=LANGUAGE, vad_filter=True,
        beam_size=1,
        condition_on_previous_text=True,
        initial_prompt=WHISPER_INITIAL_PROMPT,
    )
    segments = list(segments)
    texte = "".join(s.text for s in segments)
    # faster-whisper expose no_speech_prob par segment.
    probs = [getattr(s, "no_speech_prob", None) for s in segments]
    probs = [p for p in probs if p is not None]
    nsp = max(probs) if probs else None
    return texte, nsp


def _transcrire_cloud(client, audio):
    """Transcrit un segment via l'API Groq (whisper-large-v3). Lève si échec.
    Le prompt combine le lexique médical et le contexte récent (cohérence).
    Renvoie (texte, no_speech_prob|None) — verbose_json expose le score."""
    buf = audio_to_wav_buffer(audio)   # numpy → BytesIO WAV nommé (audio.py)

    with _contexte_lock:
        ctx = _dernier_contexte
    prompt_complet = WHISPER_INITIAL_PROMPT
    if ctx:
        prompt_complet = ctx[-150:] + " " + WHISPER_INITIAL_PROMPT
    # Groq compte la limite en octets UTF-8 (accents = 2 octets) : on tronque
    # sur cette base, en conservant le contexte récent (placé en tête).
    enc = prompt_complet.encode("utf-8")
    if len(enc) > GROQ_PROMPT_MAX_BYTES:
        prompt_complet = enc[:GROQ_PROMPT_MAX_BYTES].decode("utf-8", "ignore")

    response = client.audio.transcriptions.create(
        model="whisper-large-v3",
        file=buf,
        language=LANGUAGE,
        prompt=prompt_complet,
        temperature=0,   # déterministe → moins d'hallucinations, aucun coût en vitesse
        response_format="verbose_json",   # expose no_speech_prob par segment
    )
    texte = getattr(response, "text", "") or ""
    # Segments : liste d'objets ou de dicts selon la version du client.
    nsp = None
    segs = getattr(response, "segments", None) or []
    probs = []
    for s in segs:
        p = s.get("no_speech_prob") if isinstance(s, dict) else getattr(s, "no_speech_prob", None)
        if p is not None:
            probs.append(float(p))
    if probs:
        nsp = max(probs)   # le pire segment décide (bruit ponctuel)
    return texte, nsp


# File des segments à corriger par le LLM (id, texte, contexte).
# Le worker tourne en parallèle : le texte brut est affiché immédiatement,
# la version corrigée remplace le tour à l'écran quand elle arrive (~1 s).
_correction_queue = queue.Queue()
_seg_id_lock = threading.Lock()
_seg_id_next = 0


def _nouveau_seg_id():
    global _seg_id_next
    with _seg_id_lock:
        _seg_id_next += 1
        return _seg_id_next


def _correction_worker():
    """Consomme _correction_queue et pousse les corrections vers l'affichage.
    S'arrête avec stop_event (fin de consultation)."""
    while not stop_event.is_set():
        try:
            seg_id, texte, ctx = _correction_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        corrige = correction.corriger_segment(texte, ctx)
        if corrige and corrige != texte:
            display_queue.put(("CORRECTION", corrige, seg_id))


def transcrire():
    """Worker unique : consomme les segments et les transcrit.

    Groq (whisper-large-v3-turbo) est le moteur PRINCIPAL. En cas d'erreur
    (connexion, quota…), repli automatique sur le modèle local turbo :
      - si le modèle local est présent → message sobre + transcription locale ;
      - s'il est absent → on signale à l'overlay qu'un téléchargement de secours
        est requis (à la demande uniquement, 1,6 Go).
    Le modèle local n'est chargé que lorsqu'il devient nécessaire (paresseux)."""
    _reset_contexte()          # nouveau contexte glissant pour cette consultation
    # Purger les corrections en attente d'une consultation précédente.
    while True:
        try:
            _correction_queue.get_nowait()
        except queue.Empty:
            break
    threading.Thread(target=_correction_worker, daemon=True).start()
    cloud_client    = _init_cloud_client()
    cloud_on        = cloud_client is not None
    local_model     = None     # chargé paresseusement au 1er repli
    fallback_warned = False     # avertir « repli local » une seule fois
    backup_signaled = False     # signaler « téléchargement requis » une seule fois

    if cloud_on:
        display_queue.put(("INFO", "Transcription prête (Groq)."))
    else:
        # Pas de clé : on s'appuie directement sur le modèle local s'il existe.
        if modele_local_present():
            local_model = _charger_modele_local()
        if local_model is not None:
            display_queue.put(("INFO", "Pret. La transcription demarre (local)."))
        else:
            display_queue.put(("BESOIN_SECOURS",
                "Téléchargement du modèle de secours requis (1,6 Go). Continuer ?"))
            backup_signaled = True

    while not stop_event.is_set():
        try:
            label, audio = segment_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        # Toute exception du traitement d'UN segment est journalisée (traceback
        # complet) et n'interrompt jamais le worker : sinon la transcription
        # s'arrêterait silencieusement pour toute la consultation.
        try:
            # Filtre RMS déjà appliqué dans capturer() avec le seuil adapté à la
            # source (loopback patient, mic médecin télé, mic cabinet adaptatif).
            texte = None
            no_speech = None
            if cloud_on:
                try:
                    texte, no_speech = _transcrire_cloud(cloud_client, audio)
                except Exception:
                    # Repli local : charger le modèle s'il est dispo, sinon
                    # proposer le téléchargement de secours.
                    if local_model is None and modele_local_present():
                        local_model = _charger_modele_local()
                    if local_model is not None:
                        if not fallback_warned:
                            display_queue.put(("AVIS",
                                "Connexion indisponible — transcription locale (qualité réduite)"))
                            fallback_warned = True
                    elif not backup_signaled:
                        display_queue.put(("BESOIN_SECOURS",
                            "Téléchargement du modèle de secours requis (1,6 Go). Continuer ?"))
                        backup_signaled = True

            if texte is None and local_model is not None:
                texte, no_speech = _transcrire_local(local_model, audio)

            # Filtre de confiance : score élevé de non-parole → bruit/silence
            # halluciné, on rejette avant tout affichage.
            if no_speech is not None and no_speech > NO_SPEECH_MAX:
                _caplog.debug("[%s] rejeté : no_speech_prob=%.3f > %.2f (texte=%r)",
                              label, no_speech, NO_SPEECH_MAX, (texte or "")[:60])
                continue

            texte = corriger_transcription((texte or "").strip())
            # Filtre anti-hallucination de sous-titrage : ni affiché, ni
            # sauvegardé, ni injecté dans le contexte du prochain segment.
            if texte and not est_hallucination_generique(texte):
                _caplog.debug("[%s] accepté no_speech=%s texte=%r",
                              label,
                              ("%.3f" % no_speech) if no_speech is not None else "n/a",
                              texte[:60])
                with _contexte_lock:
                    ctx_correction = _dernier_contexte
                _maj_contexte(texte)   # alimente le prompt du prochain segment
                # Affichage immédiat du texte brut ; la correction LLM arrive
                # ensuite via _correction_worker et remplace le tour à l'écran.
                seg_id = _nouveau_seg_id()
                display_queue.put((label, texte, seg_id))
                _correction_queue.put((seg_id, texte, ctx_correction))
        except Exception:
            _caplog.error("transcrire: segment %s ignoré (exception)\n%s",
                          label, traceback.format_exc())


# ----------------------------- EXPORT DOCUMENT (delegue a storage.py) -------

# La generation .docx/.txt et les constantes associees vivent dans storage.py.
# Alias conserve pour construire le transcript du resume (libelle accentue).
LOCUTEUR_FICHIER = storage.LOCUTEUR_FICHIER

# ----------------------------- INTERFACE -------------------------------------


def nettoyer_nom_fichier(s):
    """Rend `s` utilisable dans un nom de fichier Windows : sans accents ni
    caracteres interdits. (Les accents restent intacts dans l'en-tete du fichier.)"""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r'[<>:"/\\|?*]', "", s)          # caracteres interdits Windows
    s = re.sub(r"[\x00-\x1f]", "", s)            # caracteres de controle
    s = re.sub(r"\s+", "-", s.strip())
    return s or "consultation"


class Overlay:
    COULEURS = {
        "Medecin": "#7ee787",
        "Patient": "#79c0ff",
        "INFO":    "#8b949e",
        "AVIS":    "#d29922",
        "ERREUR":  "#ff7b72",
    }

    def __init__(self, root, dossier_sauvegarde=None, banniere=None):
        self.root = root
        # Transcript complet accumule au fil de l'eau : (horodatage, locuteur, texte).
        self.entries = []
        # Dossier de sauvegarde memorise (ouverture par defaut de la boite fichier).
        self.dossier_sauvegarde = dossier_sauvegarde

        root.title("Écho — transcription consultation")
        root.geometry("520x640+40+40")
        root.attributes("-topmost", True)
        root.configure(bg="#0d1117")

        # Bandeau doux (ex : aucun micro detecte), affiche en haut si fourni.
        if banniere:
            tk.Label(root, text=banniere, bg="#3a2d10", fg="#f0c674",
                     font=("Segoe UI", 10), wraplength=496, justify="left",
                     padx=12, pady=8).pack(side="top", fill="x")

        # Barre du bas avec le bouton Quitter.
        barre = tk.Frame(root, bg="#161b22")
        barre.pack(side="bottom", fill="x")
        tk.Button(barre, text="Quitter", command=self.on_quit,
                  font=("Segoe UI", 10, "bold")).pack(side="right", padx=10, pady=8)

        self.text = tk.Text(
            root, bg="#0d1117", fg="#e6edf3", font=("Segoe UI", 13),
            wrap="word", padx=14, pady=14, borderwidth=0, spacing1=4, spacing3=6,
        )
        self.text.pack(side="top", fill="both", expand=True)
        for nom, couleur in self.COULEURS.items():
            self.text.tag_configure(nom, foreground=couleur)
        self.text.configure(state="disabled")

        # La croix de fermeture passe par le meme chemin que le bouton Quitter.
        root.protocol("WM_DELETE_WINDOW", self.on_quit)

        self.root.after(100, self._vider_file)

    def _vider_file(self):
        while True:
            try:
                item = display_queue.get_nowait()
            except queue.Empty:
                break
            label, texte = item[0], item[1]
            if label == "CORRECTION":
                continue   # UI tk legacy : pas de remplacement in-place
            self.text.configure(state="normal")
            if label in ("INFO", "AVIS", "ERREUR"):
                self.text.insert("end", "- " + texte + "\n", label)
            else:
                # Entree de transcription : on l'affiche ET on l'accumule.
                horodatage = datetime.datetime.now().strftime("%H:%M:%S")
                self.entries.append((horodatage, label, texte))
                self.text.insert("end", label + " : ", label)
                self.text.insert("end", texte + "\n")
            self.text.see("end")
            self.text.configure(state="disabled")
        if not stop_event.is_set():
            self.root.after(100, self._vider_file)

    # ------------------------- Fermeture / enregistrement --------------------

    def on_quit(self):
        """Bouton Quitter ET croix de fermeture. Tout sur le thread principal."""
        rep = messagebox.askyesnocancel(
            "Quitter",
            "Enregistrer la transcription de cette consultation ?",
            parent=self.root,
        )
        if rep is None:
            return                      # Annuler -> on revient a l'appli
        if rep:                         # Oui -> formulaire patient, resume, fichier
            infos = self._formulaire_patient()
            if infos is None:
                return                  # formulaire annule -> on revient a l'appli

            resume = None
            if self.entries:            # rien a resumer si aucune parole captee
                resume = self._generer_resume_avec_progres(self._verbatim_texte())
                if resume is not None:
                    resume = self._relire_resume(resume)
                    if resume is None:
                        return          # relecture annulee -> on revient a l'appli
                else:
                    # Echec modele/generation : pas d'erreur technique, on continue.
                    message_simple(
                        "Résumé indisponible",
                        "Le résumé automatique n'a pas pu être généré.\n\n"
                        "La transcription mot à mot sera enregistrée normalement.")

            # Fenêtre documents annexes (après relecture, avant sauvegarde).
            annexes = self._choisir_annexes()

            if not self._enregistrer(infos, resume, annexes):
                return                  # boite de fichier annulee -> on revient a l'appli
        # Non, ou enregistrement effectue -> fermeture effective.
        self._fermer()

    def _fermer(self):
        stop_event.set()
        self.root.destroy()

    def _verbatim_texte(self):
        """Transcription mot a mot accumulee, au format horodate (pour le modele
        et pour le fichier)."""
        return "\n".join(
            "[%s] %s : %s" % (h, LOCUTEUR_FICHIER.get(loc, loc), t)
            for h, loc, t in self.entries)

    def _generer_resume_avec_progres(self, transcript):
        """Genere le resume via Groq (rapide). Si indisponible, aucun resume
        (None) — la transcription reste sauvegardee. Progression dans un thread."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Résumé")
        dlg.configure(bg="#0d1117")
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)

        lbl = tk.Label(dlg, text="Rédaction du résumé...",
                       bg="#0d1117", fg="#e6edf3", font=("Segoe UI", 12))
        lbl.pack(padx=36, pady=(26, 6))
        tk.Label(dlg, text="Génération via Groq (quelques secondes).",
                 bg="#0d1117", fg="#8b949e", font=("Segoe UI", 9)).pack(padx=36, pady=(0, 26))

        etat = {"resume": None, "done": False}

        def worker():
            try:
                etat["resume"] = groq_summarize(transcript, GROQ_API_KEY)
            except Exception:
                journaliser("resume tk worker: groq_summarize a levé")
                etat["resume"] = None
            etat["done"] = True

        threading.Thread(target=worker, daemon=True).start()

        def tick():
            if etat["done"]:
                try:
                    dlg.destroy()
                except Exception:
                    pass
                return
            dlg.after(200, tick)

        dlg.after(200, tick)
        self.root.wait_window(dlg)
        return etat["resume"]

    def _relire_resume(self, resume):
        """Brouillon editable : le medecin relit/corrige avant enregistrement.
        Renvoie le texte valide, ou None si annulation.

        Structure (de haut en bas, empile avant la zone texte) :
          1. Bandeau avertissement  — fixe en haut  (pack side=TOP)
          2. Frame boutons          — fixe en bas   (pack side=BOTTOM avant le texte)
          3. Zone texte + scrollbar — remplit le reste (pack side=TOP, expand=True)
        Le pack BOTTOM avant TOP garantit que la barre de boutons ne est jamais
        chassee hors ecran quand le texte est grand.
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("Résumé — à relire et corriger")
        dlg.configure(bg="#0d1117")
        dlg.attributes("-topmost", True)
        dlg.geometry("660x560")
        dlg.minsize(520, 480)           # boutons toujours visibles a l'ouverture
        dlg.grab_set()

        # 1 — Bandeau fixe en haut.
        tk.Label(dlg,
                 text="Relisez et corrigez le résumé avant l'enregistrement. "
                      "La transcription mot à mot sera conservée dans le fichier.",
                 bg="#3a2d10", fg="#f0c674", font=("Segoe UI", 10),
                 wraplength=630, justify="left").pack(
                     side="top", fill="x", padx=0, pady=0, ipadx=12, ipady=8)

        res = {"valide": None}

        def valider():
            res["valide"] = txt.get("1.0", "end").strip()
            dlg.destroy()

        def annuler():
            dlg.destroy()               # res["valide"] reste None

        # 2 — Barre de boutons fixe en bas (packer AVANT la zone texte).
        barre = tk.Frame(dlg, bg="#161b22")
        barre.pack(side="bottom", fill="x")
        tk.Button(
            barre, text="Valider et enregistrer", command=valider,
            font=("Segoe UI", 11, "bold"), width=22,
        ).pack(side="right", padx=14, pady=10)
        tk.Button(
            barre, text="Annuler", command=annuler,
            font=("Segoe UI", 11), width=18,
        ).pack(side="right", padx=(0, 6), pady=10)

        # 3 — Zone texte + scrollbar (prend tout l'espace restant).
        frame_txt = tk.Frame(dlg, bg="#0d1117")
        frame_txt.pack(side="top", fill="both", expand=True)

        sb = tk.Scrollbar(frame_txt)
        sb.pack(side="right", fill="y")

        txt = tk.Text(frame_txt, bg="#0d1117", fg="#e6edf3",
                      font=("Segoe UI", 11), wrap="word",
                      padx=12, pady=12, insertbackground="#e6edf3",
                      yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.config(command=txt.yview)
        txt.insert("1.0", resume)

        dlg.protocol("WM_DELETE_WINDOW", annuler)
        self.root.wait_window(dlg)
        return res["valide"]

    def _formulaire_patient(self):
        """Toplevel modal de saisie des infos patient. Renvoie un dict ou None
        si l'utilisateur annule. Nom/Prenom/Date de naissance obligatoires."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Informations patient")
        dlg.configure(bg="#0d1117")
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)
        dlg.grab_set()                  # modal

        resultat = {"data": None}
        champs = {}

        def champ(libelle):
            tk.Label(dlg, text=libelle, bg="#0d1117", fg="#e6edf3",
                     font=("Segoe UI", 10)).pack(anchor="w", padx=18, pady=(10, 0))
            e = tk.Entry(dlg, width=44, font=("Segoe UI", 11))
            e.pack(padx=18, pady=(0, 2))
            return e

        champs["nom"]       = champ("Nom *")
        champs["prenom"]    = champ("Prénom *")
        champs["naissance"] = champ("Date de naissance (JJ/MM/AAAA) *")
        champs["motif"]     = champ("Motif de consultation (facultatif)")

        err = tk.Label(dlg, text="", bg="#0d1117", fg="#ff7b72", font=("Segoe UI", 9))
        err.pack(padx=18, pady=(6, 0))

        def valider():
            nom       = champs["nom"].get().strip()
            prenom    = champs["prenom"].get().strip()
            naissance = champs["naissance"].get().strip()
            motif     = champs["motif"].get().strip()
            if not nom:
                err.config(text="Le champ Nom est obligatoire.")
                champs["nom"].focus_set()
                return
            if not prenom:
                err.config(text="Le champ Prénom est obligatoire.")
                champs["prenom"].focus_set()
                return
            if not naissance:
                err.config(text="La date de naissance est obligatoire.")
                champs["naissance"].focus_set()
                return
            resultat["data"] = {"nom": nom, "prenom": prenom,
                                "naissance": naissance, "motif": motif}
            dlg.destroy()

        def annuler():
            dlg.destroy()               # resultat reste None

        btns = tk.Frame(dlg, bg="#0d1117")
        btns.pack(pady=14)
        tk.Button(btns, text="Valider", command=valider,
                  font=("Segoe UI", 10, "bold")).pack(side="left", padx=6)
        tk.Button(btns, text="Annuler", command=annuler).pack(side="left", padx=6)

        dlg.protocol("WM_DELETE_WINDOW", annuler)
        champs["nom"].focus_set()
        self.root.wait_window(dlg)
        return resultat["data"]

    def _choisir_annexes(self):
        """Fenêtre 'Documents annexes' (après relecture résumé, avant sauvegarde).
        Renvoie une liste de {"label": str, "fichier": str|None} (jamais None).
        Fermeture sans choix = liste vide (équivalent 'Passer')."""
        DOCS = [
            ("ordonnance",    "Ordonnance"),
            ("arret",         "Arrêt de travail"),
            ("autre",         "Autre document"),
        ]
        resultat = {"annexes": [], "valide": False}

        dlg = tk.Toplevel(self.root)
        dlg.title("Documents créés lors de cette consultation")
        dlg.configure(bg="#0d1117")
        dlg.attributes("-topmost", True)
        dlg.geometry("620x480")
        dlg.minsize(540, 380)
        dlg.grab_set()

        # --- En-tête ---
        tk.Label(dlg, text="Documents créés lors de cette consultation",
                 bg="#0d1117", fg="#e6edf3",
                 font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=22, pady=(22, 2))
        tk.Label(dlg,
                 text="Sélectionnez les documents à inclure dans le compte-rendu.\n"
                      "Vous pourrez joindre un fichier (photo ou scan) pour chacun.",
                 bg="#0d1117", fg="#8b949e", font=("Segoe UI", 10),
                 justify="left").pack(anchor="w", padx=22, pady=(0, 14))

        corps = tk.Frame(dlg, bg="#0d1117")
        corps.pack(fill="both", expand=True, padx=22)

        # État par document : {clé: {"var": BooleanVar, "fichier": str|None,
        #                            "lbl_fichier": Label, "autre_var": StringVar}}
        etat = {}

        for cle, libelle in DOCS:
            var = tk.BooleanVar(value=False)
            autre_var = tk.StringVar(value="")
            etat[cle] = {"var": var, "fichier": None,
                         "lbl_fichier": None, "autre_var": autre_var}

            # Conteneur pour la ligne complète du document.
            frame_doc = tk.Frame(corps, bg="#0d1117")
            frame_doc.pack(fill="x", pady=(0, 10))

            # Case à cocher + libellé.
            cb = tk.Checkbutton(
                frame_doc, text=libelle, variable=var,
                bg="#0d1117", fg="#e6edf3", selectcolor="#0d1117",
                activebackground="#0d1117", activeforeground="#e6edf3",
                font=("Segoe UI", 12, "bold"), anchor="w",
            )
            cb.pack(anchor="w")

            # Champ texte "Autre document".
            if cle == "autre":
                frame_autre = tk.Frame(frame_doc, bg="#0d1117")
                frame_autre.pack(anchor="w", padx=26, pady=(2, 0))
                tk.Label(frame_autre, text="Précisez le type :",
                         bg="#0d1117", fg="#8b949e",
                         font=("Segoe UI", 10)).pack(side="left")
                tk.Entry(frame_autre, textvariable=autre_var, width=28,
                         font=("Segoe UI", 10)).pack(side="left", padx=(6, 0))

            # Sous-frame bouton + nom fichier (visible quand coché).
            frame_fichier = tk.Frame(frame_doc, bg="#0d1117")
            frame_fichier.pack(anchor="w", padx=26, pady=(4, 0))

            lbl_fich = tk.Label(frame_fichier, text="Aucun fichier joint",
                                bg="#0d1117", fg="#8b949e",
                                font=("Segoe UI", 9), anchor="w")
            etat[cle]["lbl_fichier"] = lbl_fich

            def _joindre(c=cle, lf=lbl_fich):
                p = filedialog.askopenfilename(
                    parent=dlg,
                    title="Choisir un fichier",
                    filetypes=[
                        ("Images et PDF", "*.jpg *.jpeg *.png *.pdf"),
                        ("Images", "*.jpg *.jpeg *.png"),
                        ("PDF", "*.pdf"),
                        ("Tous les fichiers", "*.*"),
                    ],
                )
                if p:
                    etat[c]["fichier"] = p
                    nom_court = os.path.basename(p)
                    if len(nom_court) > 40:
                        nom_court = nom_court[:37] + "..."
                    lf.config(text=nom_court, fg="#7ee787")

            tk.Button(
                frame_fichier,
                text="\U0001f4ce Joindre un fichier (image ou PDF)",
                command=_joindre,
                font=("Segoe UI", 10), pady=4, padx=8,
            ).pack(side="left")
            lbl_fich.pack(side="left", padx=(10, 0))

        # --- Boutons bas ---
        def continuer():
            docs = []
            for cle, libelle in DOCS:
                if etat[cle]["var"].get():
                    if cle == "autre":
                        lab = etat[cle]["autre_var"].get().strip() or "Autre document"
                    else:
                        lab = libelle
                    docs.append({"label": lab, "fichier": etat[cle]["fichier"]})
            resultat["annexes"] = docs
            resultat["valide"] = True
            dlg.destroy()

        def passer():
            resultat["annexes"] = []
            resultat["valide"] = True
            dlg.destroy()

        barre = tk.Frame(dlg, bg="#161b22")
        barre.pack(side="bottom", fill="x")
        tk.Button(barre, text="Continuer", command=continuer,
                  font=("Segoe UI", 11, "bold"), width=16,
                  ).pack(side="right", padx=14, pady=10)
        tk.Button(barre, text="Passer — aucun document", command=passer,
                  font=("Segoe UI", 11), width=24,
                  ).pack(side="right", padx=(0, 6), pady=10)

        dlg.protocol("WM_DELETE_WINDOW", passer)   # croix = Passer
        self.root.wait_window(dlg)
        return resultat["annexes"]

    def _enregistrer(self, infos, resume=None, annexes=None):
        """Produit un fichier Word .docx (avec fallback .txt si python-docx echoue).
        Structure : titre + tableau patient + resume edite + transcription integrale
        + documents annexes. Renvoie True si ecrit, False si la boite est annulee."""
        now = datetime.datetime.now()
        date_str  = now.strftime("%d/%m/%Y")
        heure_str = now.strftime("%Hh%M")
        nom    = infos["nom"]
        prenom = infos["prenom"]

        nom_defaut = "%s_%s_%s_%s.docx" % (
            nettoyer_nom_fichier(nom.upper()),
            nettoyer_nom_fichier(prenom),
            now.strftime("%Y-%m-%d"),
            now.strftime("%Hh%M"),
        )
        initialdir = self.dossier_sauvegarde \
            if (self.dossier_sauvegarde and os.path.isdir(self.dossier_sauvegarde)) else None

        chemin = filedialog.asksaveasfilename(
            parent=self.root,
            title="Enregistrer le compte-rendu",
            defaultextension=".docx",
            initialfile=nom_defaut,
            initialdir=initialdir,
            filetypes=[("Document Word", "*.docx"), ("Tous les fichiers", "*.*")],
        )
        if not chemin:
            return False

        # Tente l'enregistrement docx ; repli txt si python-docx indisponible.
        try:
            storage.ecrire_docx(chemin, infos, now, resume, self.entries, annexes=annexes)
        except Exception:
            # Fallback .txt (ne jamais perdre la transcription) — mais la
            # cause de l'échec docx doit rester visible pour l'éditeur.
            journaliser("_enregistrer: ecrire_docx a échoué, repli .txt")
            chemin_txt = re.sub(r"\.docx$", ".txt", chemin, flags=re.IGNORECASE)
            if not chemin_txt.lower().endswith(".txt"):
                chemin_txt += ".txt"
            try:
                storage.ecrire_txt_secours(chemin_txt, infos, now, resume, self.entries)
                message_simple(
                    "Enregistrement Word impossible",
                    "Le document Word n'a pas pu être créé.\n\n"
                    "La transcription a été sauvegardée en texte brut :\n"
                    + chemin_txt)
            except Exception:
                # Échec TOTAL de sauvegarde (docx ET txt) : le pire cas.
                journaliser("_enregistrer: échec docx PUIS txt — transcription non sauvée")
                message_simple(
                    "Enregistrement impossible",
                    "Le fichier n'a pas pu être enregistré à cet endroit.\n\n"
                    "Choisissez un autre dossier (par exemple le Bureau).",
                    genre="error")
                return False

        try:
            dossier = os.path.dirname(chemin)
            self.dossier_sauvegarde = dossier
            cfg = charger_config()
            cfg["dossier_sauvegarde"] = dossier
            sauver_config(cfg)
        except Exception:
            pass   # mémorisation du dernier dossier : confort, jamais bloquant
        return True


# ----------------------------- MAIN (tkinter legacy) -------------------------

def _main_tk():
    """Point d'entree tkinter (conserve pour --tk et tests en dev)."""
    choix = choisir_peripheriques()
    if choix is None:
        return
    loopback, micro, dossier = choix

    banniere = None
    if micro is None:
        banniere = ("Aucun micro détecté — seule la voix du patient sera transcrite. "
                    "Branchez un micro puis relancez pour transcrire aussi le médecin.")
        display_queue.put(("AVIS", banniere))
    else:
        display_queue.put(("INFO", "Micro (médecin) : " + micro.name))
    display_queue.put(("INFO", "Sortie (patient) : " + loopback.name))

    threads = [
        threading.Thread(target=transcrire, daemon=True),
        threading.Thread(target=capturer, args=(lambda: loopback, "Patient"), daemon=True),
    ]
    if micro is not None:
        threads.append(
            threading.Thread(target=capturer, args=(lambda: micro, "Medecin"), daemon=True))
    for t in threads:
        t.start()

    root = tk.Tk()
    Overlay(root, dossier_sauvegarde=dossier, banniere=banniere)
    try:
        root.mainloop()
    finally:
        stop_event.set()
        time.sleep(0.3)


# ----------------------------- GESTIONNAIRE DE TÉLÉCHARGEMENT ----------------

# État partagé entre le thread de téléchargement et l'Api (JSON-sérialisable).
# ---- Logger fichier pour diagnostic des téléchargements ---------------
import logging as _logging
import os as _os
_log_path = _os.path.join(_os.environ.get('APPDATA', _os.path.expanduser('~')), 'Echo', 'download.log')
_os.makedirs(_os.path.dirname(_log_path), exist_ok=True)
_logging.basicConfig(
    filename=_log_path, level=_logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True)

_download_state = {
    "whisper": {"downloaded": 0, "total": 1_700_000_000, "speed": 0.0,
                "done": False, "error": None},
    # Mise à jour de l'application (EchoSetup.exe téléchargé dans %TEMP%).
    "update":  {"downloaded": 0, "total": 195_000_000, "speed": 0.0,
                "done": False, "error": None},
    "running": False,
}
_download_lock = threading.Lock()

# ----------------------------- MISE À JOUR AUTOMATIQUE ------------------------

_update_info  = None                 # résultat caché de la vérification
_update_lock  = threading.Lock()
_update_file  = None                 # chemin du EchoSetup.exe téléchargé


def _version_tuple(v):
    """Convertit '1.2.0' → (1, 2, 0). Lève si format inattendu."""
    return tuple(int(x) for x in str(v).strip().split("."))


def _log_update(msg):
    """Journal de la mise à jour auto : %APPDATA%\\Echo\\update.log (toujours
    actif, une ligne par vérification — l'échec silencieux d'avant a caché
    pendant des mois que l'auto-update ne marchait pas)."""
    try:
        dossier = os.path.join(os.environ.get("APPDATA", ""), "Echo")
        os.makedirs(dossier, exist_ok=True)
        with open(os.path.join(dossier, "update.log"), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), msg))
    except Exception:
        pass


def verifier_mise_a_jour():
    """Interroge la dernière release GitHub (stdlib uniquement : l'ancienne
    version importait `requests`, non embarqué dans le build → ImportError
    avalée → l'auto-update n'a JAMAIS fonctionné, silencieusement).
    Retourne {disponible: bool, version?, url?} ; tout échec est journalisé."""
    url_api = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
    try:
        req = urllib.request.Request(url_api, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Echo-app/" + APP_VERSION,   # exigé par l'API GitHub
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        derniere = str(data.get("tag_name", "")).lstrip("v")   # 'v2.2.1' → '2.2.1'
        if not derniere:
            _log_update("réponse sans tag_name (rate limit ?) : %s"
                        % str(data)[:120])
            return {"disponible": False}
        if _version_tuple(derniere) > _version_tuple(APP_VERSION):
            url = next(
                (a["browser_download_url"] for a in data.get("assets", [])
                 if a["name"].endswith(".exe")), None)
            if url is None:
                _log_update("v%s disponible mais aucun .exe attaché" % derniere)
                return {"disponible": False}
            _log_update("v%s disponible (locale v%s)" % (derniere, APP_VERSION))
            return {"disponible": True, "version": derniere, "url": url}
        _log_update("à jour (locale v%s, GitHub v%s)" % (APP_VERSION, derniere))
        return {"disponible": False}
    except urllib.error.HTTPError as e:
        _log_update("échec HTTP %d sur %s" % (e.code, url_api))
    except Exception as e:
        _log_update("échec vérification : %s: %s" % (type(e).__name__, e))
    return {"disponible": False}


def _warm_update_check():
    """Pré-charge la vérification de mise à jour (thread daemon au démarrage)."""
    global _update_info
    info = verifier_mise_a_jour()
    with _update_lock:
        _update_info = info


def _dl_update(url):
    """Télécharge EchoSetup.exe dans %TEMP% avec progression (best-effort)."""
    global _update_file
    dest = os.path.join(tempfile.gettempdir(), "EchoSetup.exe")
    with _download_lock:
        _download_state["update"].update(
            {"downloaded": 0, "speed": 0.0, "done": False, "error": None})
    try:
        # stdlib uniquement (même piège que verifier_mise_a_jour : `requests`
        # n'est pas embarqué dans le build).
        req = urllib.request.Request(url, headers={
            "User-Agent": "Echo-app/" + APP_VERSION})
        with urllib.request.urlopen(req, timeout=30) as r:
            total = int(r.headers.get("content-length") or 0) or \
                _download_state["update"]["total"]
            with _download_lock:
                _download_state["update"]["total"] = total
            dl = 0
            t0 = time.time()
            with open(dest, "wb") as f:
                while True:
                    if stop_event.is_set():
                        break
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    dl += len(chunk)
                    dt = time.time() - t0
                    with _download_lock:
                        _download_state["update"]["downloaded"] = dl
                        _download_state["update"]["speed"] = \
                            round(dl / dt / 1_000_000, 1) if dt > 0 else 0.0
        _update_file = dest
        _log_update("EchoSetup.exe téléchargé (%d octets)" % dl)
        with _download_lock:
            _download_state["update"]["done"]       = True
            _download_state["update"]["downloaded"] = _download_state["update"]["total"]
            _download_state["update"]["speed"]      = 0.0
    except Exception as exc:
        journaliser("_dl_update: téléchargement EchoSetup.exe interrompu")
        with _download_lock:
            _download_state["update"]["error"] = (
                "Téléchargement de la mise à jour interrompu. "
                "Vérifiez votre connexion. (" + str(exc)[:80] + ")")


def _taille_dossier(chemin):
    """Taille totale en octets de tous les fichiers d'un dossier."""
    total = 0
    try:
        for root, _, files in os.walk(chemin):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _progres_whisper_loop(dest_dir, stop_dl):
    """Thread de polling pour la progression Whisper."""
    prev_dl   = 0
    prev_time = time.time()
    while not stop_dl.is_set():
        dl = _taille_dossier(dest_dir)
        now = time.time()
        dt  = now - prev_time
        if dt > 0:
            speed = (dl - prev_dl) / dt  # octets/s
        else:
            speed = 0.0
        prev_dl   = dl
        prev_time = now
        with _download_lock:
            s = _download_state["whisper"]
            if not s["done"] and not s["error"]:
                s["downloaded"] = dl
                s["speed"]      = round(speed / 1_000_000, 1)  # Mo/s
        time.sleep(0.5)


def _dl_whisper():
    """Télécharge le modèle Whisper depuis HuggingFace (avec reprise)."""
    import logging
    try:
        logging.debug("_dl_whisper: début")
        from huggingface_hub import snapshot_download
        dest = os.path.join(models_dir(), MODELE_WHISPER_DIR)
        logging.debug("_dl_whisper: dest = %s", dest)
        os.makedirs(dest, exist_ok=True)
        stop_dl = threading.Event()
        t_prog  = threading.Thread(target=_progres_whisper_loop,
                                   args=(dest, stop_dl), daemon=True)
        t_prog.start()
        logging.debug("_dl_whisper: snapshot_download...")
        snapshot_download(
            repo_id="deepdml/faster-whisper-large-v3-turbo-ct2",
            local_dir=dest,
            resume_download=True,
            ignore_patterns=["*.msgpack", "*.h5", "flax_model*",
                             "tf_model*", "*.ot", "README*"],
        )
        logging.debug("_dl_whisper: terminé avec succès")
        with _download_lock:
            _download_state["whisper"]["done"]       = True
            _download_state["whisper"]["downloaded"] = _download_state["whisper"]["total"]
            _download_state["whisper"]["speed"]      = 0.0
    except Exception as exc:
        logging.exception("_dl_whisper: exception")
        with _download_lock:
            _download_state["whisper"]["error"] = (
                "Téléchargement Whisper interrompu. "
                "Vérifiez votre connexion internet et réessayez. "
                "(" + str(exc)[:80] + ")")
    finally:
        stop_dl.set()


def _run_downloads(models_to_dl):
    """Télécharge séquentiellement les modèles demandés."""
    import logging
    logging.debug("_run_downloads démarré, modèles: %s", models_to_dl)
    try:
        with _download_lock:
            _download_state["running"] = True
        logging.debug("running=True")
        if "whisper" in models_to_dl:
            _dl_whisper()
        logging.debug("_run_downloads terminé avec succès")
    except Exception:
        logging.exception("_run_downloads: exception")
    finally:
        logging.debug("_run_downloads finally: running=False")
        with _download_lock:
            _download_state["running"] = False


# ----------------------------- BACKEND LICENCE/AUTH --------------------------

_ECHO_API = "https://muxoyiitqdnehuvbwcac.supabase.co/functions/v1/echo-api"
_SUPABASE_ANON = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im11eG95aWl0cWRuZWh1dmJ3Y2FjIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3ODI0MTA0NjksImV4cCI6MjA5Nzk4NjQ2OX0"
    ".4E40ECsuOlPSJIPfHdAPZc771qRa_qF6DDSKhWn76TA"
)


# Dernière erreur d'appel API, lisible par les appelants pour un message
# précis (l'ancien « Erreur réseau » fourre-tout masquait la vraie cause).
_derniere_erreur_api = {"type": None, "detail": ""}


def _appel_api(endpoint, payload, timeout=10):
    """POST JSON vers l'API Écho (Edge Function Supabase, en-tête
    Authorization = clé anon obligatoire).
    Renvoie le dict JSON de la réponse, ou None en cas d'échec — auquel
    cas _derniere_erreur_api distingue :
      'dns'     : le domaine ne résout pas (projet Supabase absent/en pause)
      'reseau'  : pas de connexion / timeout
      'http'    : le serveur a RÉPONDU une erreur (code + corps JSON si présent)
      'reponse' : réponse illisible (JSON invalide)."""
    global _derniere_erreur_api
    _derniere_erreur_api = {"type": None, "detail": ""}
    url = _ECHO_API + "/" + endpoint.lstrip("/")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _SUPABASE_ANON,
            "apikey": _SUPABASE_ANON,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _normaliser_reponse(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        # Le serveur a répondu : ce N'EST PAS une erreur réseau. On tente
        # de rendre le JSON (l'API renvoie souvent {ok:false,error:...}
        # avec un 4xx) pour que l'appelant affiche le vrai message.
        corps = ""
        try:
            corps = e.read().decode("utf-8", "replace")
            d = json.loads(corps)
            if isinstance(d, dict):
                d.setdefault("ok", False)
                d = _normaliser_reponse(d)
                d.setdefault("error", "Le serveur a répondu une erreur (HTTP %d)." % e.code)
                _derniere_erreur_api = {"type": "http", "detail": "HTTP %d" % e.code}
                return d
        except Exception:
            pass
        _derniere_erreur_api = {"type": "http",
                                "detail": "HTTP %d %s" % (e.code, corps[:120])}
        return None
    except urllib.error.URLError as e:
        raison = str(getattr(e, "reason", e))
        if "getaddrinfo" in raison or "11001" in raison or "Name or service" in raison:
            _derniere_erreur_api = {"type": "dns", "detail": raison}
        else:
            _derniere_erreur_api = {"type": "reseau", "detail": raison}
        return None
    except (socket.timeout, TimeoutError) as e:
        _derniere_erreur_api = {"type": "reseau", "detail": "timeout"}
        return None
    except ValueError as e:               # JSON invalide
        _derniere_erreur_api = {"type": "reponse", "detail": str(e)}
        return None
    except Exception as e:
        _derniere_erreur_api = {"type": "reseau", "detail": str(e)}
        return None


def _normaliser_reponse(d):
    """Uniformise une réponse API : l'Edge Function répond avec la clé
    FRANÇAISE `erreur` — le client lisait `error` et affichait donc toujours
    son message générique (« Erreur réseau » / « Inscription échouée »),
    la cause des jours de fausse chasse au réseau. On expose les deux,
    encodage réparé."""
    if not isinstance(d, dict):
        return d
    if "error" not in d and isinstance(d.get("erreur"), str):
        d["error"] = d["erreur"]
    for cle in ("error", "erreur"):
        if isinstance(d.get(cle), str):
            d[cle] = _reparer_utf8(d[cle])
    return d


def _reparer_utf8(texte):
    """Répare le mojibake « Email dÃ©jÃ  utilisÃ© » : l'Edge Function encode
    parfois l'UTF-8 deux fois. Si le texte contient des séquences typiques
    (Ã©, Ã¨…), on le ré-interprète latin-1 → UTF-8 ; sinon inchangé."""
    if not isinstance(texte, str) or "Ã" not in texte:
        return texte
    try:
        repare = texte.encode("latin-1").decode("utf-8")
        # Ne garder la réparation que si elle a vraiment réduit le mojibake.
        return repare if repare.count("Ã") < texte.count("Ã") else texte
    except (UnicodeEncodeError, UnicodeDecodeError):
        return texte


def _message_erreur_api():
    """Message utilisateur précis selon la dernière erreur d'appel API."""
    t = _derniere_erreur_api.get("type")
    if t == "dns":
        return ("Le serveur Écho est injoignable (adresse introuvable). "
                "Vérifiez votre connexion internet ; si elle fonctionne, "
                "le service est peut-être temporairement indisponible.")
    if t == "reseau":
        return "Pas de connexion internet, ou serveur trop lent à répondre. Réessayez."
    if t == "http":
        return ("Le serveur a répondu une erreur (%s). Réessayez dans un instant."
                % _derniere_erreur_api.get("detail", ""))
    if t == "reponse":
        return "Réponse du serveur illisible. Réessayez."
    return "Erreur réseau. Vérifiez votre connexion."


def _verifier_licence(cle_licence):
    """Vérifie la validité de la licence auprès du backend.
    Retourne dict avec clés valide, licence_active, en_essai, jours_restants.
    En cas d'erreur réseau : accès autorisé par défaut (fail-open)."""
    result = _appel_api("verifier-licence", {"cle_licence": cle_licence})
    if result is None:
        # Pas de réseau → on laisse passer (fail-open, pas de blocage offline)
        return {"valide": True, "licence_active": True}
    return result


# Mémoïsation du statut licence : au démarrage, get_app_state est appelé par
# la fenêtre principale ET l'overlay (+ rappels) → 3 appels réseau bloquants
# de ~0,5 s chacun mesurés. Un seul appel suffit largement par minute.
_licence_cache = {"t": 0.0, "statut": None}
_LICENCE_TTL = 60.0          # secondes de validité du cache mémoire
_licence_refresh_lock = threading.Lock()
_licence_refresh_actif = False


def _verifier_licence_cachee(cle):
    """Statut licence AVEC cache mémoire (60 s) : au plus un appel réseau."""
    global _licence_cache
    if (_licence_cache["statut"] is not None
            and time.monotonic() - _licence_cache["t"] < _LICENCE_TTL):
        return _licence_cache["statut"]
    statut = _verifier_licence(cle)
    _licence_cache = {"t": time.monotonic(), "statut": statut}
    return statut


# ----------------------------- API PYWEBVIEW ---------------------------------

class Api:
    """Pont JS ↔ Python exposé via js_api= de pywebview.
    Toutes les méthodes sont appelées depuis un thread worker de pywebview
    (thread-safe requis). Elles doivent être synchrones et retourner des
    types JSON-sérialisables (dict, list, str, int, float, bool, None)."""

    def __init__(self):
        self._window      = None  # fenêtre générique (compatibilité Phase 1)
        self._main_win    = None  # fenêtre principale
        self._overlay_win = None  # overlay transcript
        self._lock        = threading.Lock()
        self._entries     = []    # (timestamp, label, texte)
        self._seg_index   = {}    # seg_id → index dans _entries (corrections LLM)
        self._infos       = None
        self._resume      = None
        self._annexes     = []
        self._dossier_sauvegarde = None
        self._started     = False
        self._save_done   = False
        self._start_time  = None
        self._resume_status = "idle"
        self._resume_text   = None
        self._webview_mod   = None  # module webview, disponible après start
        self._mode          = "tele"  # "tele" ou "cabinet" (présentiel)
        self._tray          = None    # icône barre système (EchoTray)
        self._fermeture_reelle = False  # True quand « Quitter Écho » est demandé

    # ---- Polling -------------------------------------------------------

    def _drain_display(self):
        """Vide la display_queue → met à jour self._entries et renvoie les
        nouvelles entrées pour le JS. Partagé par get_updates() (polling) et
        arreter_capture() (drainage final au clic Terminer)."""
        items = []
        while True:
            try:
                item = display_queue.get_nowait()
            except queue.Empty:
                break
            label, texte = item[0], item[1]
            seg_id = item[2] if len(item) > 2 else None
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            if label in ("Medecin", "Patient", "Conversation"):
                with self._lock:
                    if seg_id is not None:
                        self._seg_index[seg_id] = len(self._entries)
                    self._entries.append((ts, label, texte))
            elif label == "CORRECTION":
                # Correction LLM différée : mettre à jour l'entrée stockée
                # (docx + résumé) et transmettre au JS pour le DOM.
                with self._lock:
                    idx = self._seg_index.get(seg_id)
                    if idx is not None and idx < len(self._entries):
                        h, loc, _ = self._entries[idx]
                        self._entries[idx] = (h, loc, texte)
            items.append({"type": label, "texte": texte,
                          "timestamp": ts, "seg_id": seg_id})
        return items

    def get_updates(self):
        """Vide la display_queue et retourne les nouvelles entrées.
        Appelé toutes les ~200 ms depuis le JS.
        Retourne [{type, texte, timestamp}, ...]."""
        return self._drain_display()

    def arreter_capture(self):
        """Arrêt IMMÉDIAT de la capture au clic « Terminer » : aucun segment
        audio n'est produit ni transcrit après ce point (évite les
        hallucinations sur le bruit ambiant pendant que le médecin remplit le
        formulaire). Ne ferme PAS l'overlay ni ne restaure les fenêtres
        (c'est le rôle de end_consultation).

        Séquence : figer la capture → attendre les threads → intégrer les
        derniers segments déjà transcrits → purger l'audio « en vol » non
        transcrit. Retourne les dernières entrées pour un rendu final."""
        stop_event.set()
        # Attendre l'arrêt des threads de capture/transcription (segments en vol).
        for t in getattr(self, "_threads", []):
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
        # Purger l'audio brut non encore transcrit ET la file de correction :
        # ces segments post-clic ne doivent JAMAIS produire de texte.
        for q in (segment_queue, _correction_queue):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self._started = False
        # Intégrer les segments déjà transcrits avant l'arrêt (transcript figé).
        return self._drain_display()

    def get_speaking_status(self):
        """État VAD temps réel pour l'indicateur « qui parle ».
        Appelé toutes les ~120 ms depuis le JS."""
        return {
            "medecin":      _speaking_now.get("medecin", False),
            "patient":      _speaking_now.get("patient", False),
            "conversation": _speaking_now.get("conversation", False),
            "active":       not stop_event.is_set(),
        }

    # ---- Périphériques -------------------------------------------------

    def get_devices(self):
        """Retourne les périphériques disponibles avec pré-sélections.
        Retourne {outputs, mics, selected_output, selected_mic}."""
        cfg     = charger_config()
        outputs = [str(s.name) for s in lister_sorties()]
        mics    = [str(m.name) for m in lister_micros()]

        # Pré-sélection sortie : config > PREFERRED_OUTPUT > défaut système.
        presel_out = cfg.get("sortie") if cfg.get("sortie") in outputs else None
        if not presel_out and PREFERRED_OUTPUT:
            for n in outputs:
                if PREFERRED_OUTPUT.lower() in n.lower():
                    presel_out = n
                    break
        if not presel_out:
            d = nom_sortie_defaut()
            presel_out = d if d in outputs else (outputs[0] if outputs else "")

        # Pré-sélection micro.
        presel_mic = cfg.get("micro") if cfg.get("micro") in mics else None
        if not presel_mic:
            d = nom_micro_defaut()
            presel_mic = d if d in mics else (mics[0] if mics else "")

        self._dossier_sauvegarde = cfg.get("dossier_sauvegarde")
        return {
            "outputs":         outputs,
            "mics":            mics,
            "selected_output": presel_out,
            "selected_mic":    presel_mic,
        }

    def verifier_micro(self):
        """Détection légère d'un micro disponible, AVANT de lancer la capture.
        Permet au JS de guider l'utilisateur (modale) sans démarrer les threads.
        Retourne {mic_present: bool, mics: [...]}."""
        try:
            mics = [str(m.name) for m in lister_micros()]
        except Exception:
            mics = []
        return {"mic_present": bool(mics), "mics": mics}

    # ===== PRISE EN MAIN (visite guidée + consultation de démonstration) =====

    def get_decouverte(self):
        """Flags de la prise en main + verdict micro rendu par la démo."""
        cfg = charger_config()
        return {"visite_faite": bool(cfg.get("visite_faite")),
                "demo_faite":   bool(cfg.get("demo_faite")),
                "demo_micro":   getattr(self, "_demo_micro", None)}

    def marquer_decouverte(self, flag):
        """Mémorise 'visite_faite' ou 'demo_faite' (proposer une fois,
        puis laisser tranquille — jamais de relance forcée)."""
        if flag not in ("visite_faite", "demo_faite"):
            return {"ok": False}
        cfg = charger_config()
        cfg[flag] = True
        sauver_config(cfg)
        return {"ok": True}

    def set_demo_mode(self, actif):
        """(Dés)active le mode démonstration pour la PROCHAINE consultation.
        Actif : la sauvegarde marquera demo=true et les RMS micro sont
        collectés pour le verdict micro de fin de découverte."""
        _demo_capture["actif"] = bool(actif)
        _demo_capture["rms"] = []
        if actif:
            self._demo_micro = None
        return {"ok": True}

    def get_demo_actif(self):
        """Lu par l'overlay pour afficher le script et les bulles guidées."""
        return {"actif": bool(_demo_capture["actif"])}

    def _finir_demo(self, entries):
        """Fin de la consultation de démo : verdict micro (si jamais testé —
        on ne fait pas relire une phrase, la démo remplace le test 5 s)."""
        try:
            cfg = charger_config()
            if not cfg.get("mic_test_date") and _demo_capture["rms"]:
                rms_moyen = sum(_demo_capture["rms"]) / len(_demo_capture["rms"])
                texte = " ".join(t for _, loc, t in entries if loc != "Patient")
                # Pas de phrase de référence en démo : la « qualité » est
                # simplement « du texte a été transcrit depuis le micro ».
                if rms_moyen < MIC_TEST_RMS_FAIBLE or not texte.strip():
                    verdict = "insuffisant"
                elif rms_moyen > MIC_TEST_RMS_OK:
                    verdict = "adapte"
                else:
                    verdict = "faible"
                cfg["mic_test_rms"] = round(float(rms_moyen), 5)
                cfg["mic_test_date"] = datetime.datetime.now().isoformat()
                cfg["mic_test_verdict"] = verdict
                self._demo_micro = verdict
            cfg["demo_faite"] = True
            sauver_config(cfg)
        except Exception:
            # Sans ce flag, la démo serait re-proposée : gênant, pas grave —
            # mais la cause mérite d'être visible.
            journaliser("_finir_demo: écriture des flags démo impossible")

    def suggerer_nom_patient(self):
        """Suggestion du nom entendu dans la conversation courante (démo,
        étape 3) : SUGGESTION affichée avec badge ✨, jamais un remplissage
        silencieux — le médecin garde le dernier mot."""
        with self._lock:
            entries = list(self._entries)
        try:
            return correction.detecter_nom_patient(entries)
        except Exception:
            # Suggestion de confort : son absence est tolérée, pas son silence.
            journaliser("suggerer_nom_patient: détection impossible")
            return None

    def supprimer_demo(self):
        """Supprime la consultation de démonstration (entrée + fichier).
        Le « patient » DUBOIS disparaît avec elle (jamais compté ailleurs)."""
        data = storage.charger_consultations(chemin_consultations())
        demo = next((c for c in data if isinstance(c, dict)
                     and c.get("demo") is True), None)
        if demo is None:
            return {"ok": False, "error": "Aucune démonstration trouvée."}
        storage.supprimer_consultation_avec_fichier(
            chemin_consultations(), demo.get("id"))
        return {"ok": True}

    # ===== TEST MICROPHONE (onboarding + Paramètres) =========================

    def demarrer_test_micro(self, mic_name=""):
        """Enregistre 5 s du micro, mesure le RMS, transcrit via Groq, en déduit
        un verdict (adapte/faible/insuffisant). Non bloquant : le JS poll
        get_test_micro_status() pour l'onde en direct puis le résultat."""
        if getattr(self, "_mic_test_running", False):
            return {"ok": True}
        micro = resoudre_micro(mic_name) if mic_name else micro_defaut()
        if micro is None:
            return {"ok": False, "erreur": "no_mic"}
        self._mic_test_running = True
        self._mic_test = {"status": "running", "level": 0.0}
        threading.Thread(target=self._worker_test_micro,
                         args=(micro,), daemon=True).start()
        return {"ok": True}

    def _worker_test_micro(self, micro):
        """Capture 5 s → RMS moyen → transcription Groq → verdict + config."""
        frames = []
        try:
            with micro.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                blocksize=FRAME_SAMPLES) as rec:
                n = int(5.0 * SAMPLE_RATE / FRAME_SAMPLES)
                for _ in range(n):
                    data = rec.record(numframes=FRAME_SAMPLES)
                    mono = data[:, 0] if data.ndim > 1 else data
                    frames.append(mono.copy())
                    # Niveau instantané (0..1) pour l'onde animée côté JS.
                    self._mic_test["level"] = float(rms(mono))
        except Exception:
            _caplog.error("test_micro: %s", traceback.format_exc())
            self._mic_test = {"status": "error"}
            self._mic_test_running = False
            return

        audio_full = (np.concatenate(frames) if frames
                      else np.zeros(1, dtype=np.float32))
        rms_moyen = float(rms(audio_full))
        texte, no_speech = "", None
        client = _init_cloud_client()
        if client is not None:
            try:
                texte, no_speech = _transcrire_cloud(client, audio_full)
            except Exception:
                texte = ""
        texte = (texte or "").strip()
        niveau = verdict_micro(rms_moyen, texte)

        # Mémoriser pour ne pas redemander à chaque lancement.
        try:
            cfg = charger_config()
            cfg["mic_test_rms"] = round(rms_moyen, 5)
            cfg["mic_test_date"] = datetime.datetime.now().isoformat()
            cfg["mic_test_verdict"] = niveau
            sauver_config(cfg)
        except Exception:
            pass

        self._mic_test = {
            "status": "done", "level": 0.0,
            "rms": round(rms_moyen, 5),
            "texte": texte,
            "no_speech": no_speech,
            "verdict": niveau,
        }
        self._mic_test_running = False

    def get_test_micro_status(self):
        """État courant du test micro (poll JS : level pendant, résultat après)."""
        return getattr(self, "_mic_test", {"status": "idle"})

    def mic_test_fait(self):
        """Le test micro a-t-il déjà été effectué ? (proposition au 1er cabinet)."""
        cfg = charger_config()
        return {"fait": bool(cfg.get("mic_test_date")),
                "verdict": cfg.get("mic_test_verdict", "")}

    # ---- Démarrage -----------------------------------------------------

    def start(self, mic_name, output_name, mode="tele"):
        """Résout les périphériques, sauvegarde la config et démarre les threads.

        mode="tele"    : loopback (patient) + micro (médecin), étiquetage par canal.
        mode="cabinet" : micro seul, tout étiqueté « Conversation » (attribution
                         des locuteurs par LLM en fin de consultation).
        Retourne {ok, loopback?, mic?, warning?, mode} ou {ok:false, error}."""
        if self._started:
            return {"ok": False, "error": "Déjà démarré."}

        # ---- Mode présentiel : micro seul ----
        if mode == "cabinet":
            micro = resoudre_micro(mic_name) if mic_name else micro_defaut()
            if micro is None:
                # Micro indispensable en présentiel → erreur bloquante mais
                # machine-lisible (le JS affiche une modale « Réessayer »).
                return {"ok": False, "erreur": "no_mic",
                        "error": ("Écho a besoin d'un microphone pour capter la "
                                  "consultation. Branchez un micro ou un casque, "
                                  "puis réessayez.")}
            cfg = charger_config()
            cfg["micro"] = mic_name or ""
            cfg["mode_consultation"] = "cabinet"
            sauver_config(cfg)

            threads = [
                threading.Thread(target=transcrire, daemon=True),
                threading.Thread(target=capturer,
                                 args=(lambda: micro, "Conversation"), daemon=True),
            ]
            for t in threads:
                t.start()
            self._threads = threads
            self._started = True
            return {"ok": True, "mic": micro.name, "mode": "cabinet"}

        # ---- Mode téléconsultation : loopback + micro (inchangé) ----
        loopback = resoudre_loopback(output_name) or loopback_defaut()
        if loopback is None:
            return {"ok": False,
                    "error": ("Aucune sortie audio disponible. "
                              "Branchez un casque ou des haut-parleurs, "
                              "puis relancez Écho.")}

        micro = resoudre_micro(mic_name) if mic_name else None

        # Persistance config.
        cfg = charger_config()
        cfg["sortie"] = output_name
        cfg["micro"]  = mic_name or ""
        cfg["mode_consultation"] = "tele"
        sauver_config(cfg)

        threads = [
            threading.Thread(target=transcrire, daemon=True),
            threading.Thread(target=capturer,
                             args=(lambda: loopback, "Patient"), daemon=True),
        ]
        if micro is not None:
            threads.append(
                threading.Thread(target=capturer,
                                 args=(lambda: micro, "Medecin"), daemon=True))
        for t in threads:
            t.start()

        self._threads = threads   # mémorisés pour attendre leur fin au restart
        self._started = True
        result = {"ok": True, "loopback": loopback.name, "mode": "tele"}
        if micro is None:
            # Mode dégradé assumé : patient seul. Flag machine-lisible + message.
            result["mic_absent"] = True
            result["warning"] = (
                "Aucun micro détecté — seule la voix du patient sera transcrite. "
                "Branchez un micro puis relancez pour transcrire aussi le médecin.")
        else:
            result["mic"] = micro.name
        return result

    # ---- Fermeture (Phase 1 : simple ; Phase 2 : flux de sauvegarde) ---

    def quit_app(self):
        """Phase 1 : arrête les threads et ferme la fenêtre."""
        stop_event.set()
        if self._window:
            self._window.destroy()
        return {"ok": True}

    # ---- Phase 2+ : stubs exposés dès maintenant -----------------------

    def save_patient(self, nom, prenom, ddn, motif):
        """Enregistre les infos patient (Phase 2 : déclenche le flux résumé)."""
        self._infos = {"nom": nom, "prenom": prenom, "naissance": ddn, "motif": motif}
        return {"ok": True}

    def get_resume(self):
        """Retourne le résumé généré (Phase 2). Stub Phase 1."""
        return {"texte": self._resume or ""}

    def validate_resume(self, texte_corrige):
        """Valide le résumé édité et déclenche la sauvegarde du .docx (Phase 2)."""
        self._resume = texte_corrige
        return {"ok": True}

    def set_annexes(self, annexes):
        """Enregistre la liste des documents annexes (Phase 2)."""
        self._annexes = annexes or []
        return {"ok": True}

    def pick_file(self):
        """Ouvre un sélecteur de fichier natif via pywebview (Phase 2)."""
        if not self._window:
            return {"path": None}
        try:
            import webview as _wv
            paths = self._window.create_file_dialog(
                _wv.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("Images et PDF (*.jpg;*.jpeg;*.png;*.pdf)",
                            "Tous les fichiers (*.*)"),
            )
            return {"path": paths[0] if paths else None}
        except Exception:
            return {"path": None}

    def cancel_save(self):
        """Annule le flux de sauvegarde côté overlay (JS annule le modal)."""
        return {"ok": True}

    def navigate(self, target):
        """Charge une nouvelle page dans une fenêtre (usage interne)."""
        win = self._overlay_win or self._main_win or self._window
        if not win:
            return {"ok": False}
        ui_dir = ressource("ui")
        if not target.endswith(".html"):
            target += ".html"
        win.load_url(os.path.join(ui_dir, target))
        return {"ok": True}

    # ===== ÉTAT GLOBAL / ONBOARDING ==========================================

    def _rafraichir_licence_fond(self, cle):
        """Vérifie la licence en ARRIÈRE-PLAN (un seul thread à la fois),
        persiste le statut en config (dernier connu pour le prochain
        démarrage) et notifie l'UI si la validité a changé."""
        global _licence_refresh_actif
        with _licence_refresh_lock:
            if _licence_refresh_actif:
                return
            _licence_refresh_actif = True

        def worker():
            global _licence_refresh_actif
            try:
                _t = time.perf_counter()
                statut = _verifier_licence_cachee(cle)
                _chrono("licence rafraîchie en arrière-plan (%.3fs)"
                        % (time.perf_counter() - _t))
                cfg = charger_config()
                avant = cfg.get("licence_valide_cache", True)
                cfg["licence_valide_cache"] = bool(statut.get("valide"))
                cfg["en_essai_cache"] = bool(statut.get("en_essai"))
                if statut.get("en_essai"):
                    cfg["jours_restants"] = statut.get("jours_restants", 0)
                sauver_config(cfg)
                # Le statut a changé pendant que l'UI tournait sur l'ancien :
                # prévenir la fenêtre principale (bascule expiré/valide).
                if bool(statut.get("valide")) != bool(avant):
                    win = getattr(self, "_main_win", None)
                    if win is not None:
                        _safe_js(win, "typeof onLicenceMaj==='function' && onLicenceMaj(%s)"
                                 % ("true" if statut.get("valide") else "false"))
            except Exception:
                journaliser("_rafraichir_licence_fond: échec du rafraîchissement")
            finally:
                with _licence_refresh_lock:
                    _licence_refresh_actif = False

        threading.Thread(target=worker, daemon=True).start()

    def get_app_state(self):
        """Retourne l'état de l'appli : licence, onboarding, nom médecin, etc."""
        cfg = charger_config()
        cle = cfg.get("cle_licence", "")

        licence_ok      = False
        licence_expired = False
        en_essai        = False
        jours_restants  = 0

        if cle:
            # JAMAIS d'appel réseau bloquant ici (le pont JS gelait ~0,5 s
            # par fenêtre au démarrage). Cache mémoire s'il est frais, sinon
            # dernier statut connu en config (fail-open : une licence valide
            # en cache laisse travailler même si le backend tarde), et un
            # rafraîchissement part en arrière-plan — l'UI est notifiée si
            # le statut change.
            if (_licence_cache["statut"] is not None
                    and time.monotonic() - _licence_cache["t"] < _LICENCE_TTL):
                statut = _licence_cache["statut"]
                _chrono("get_app_state: licence depuis le cache mémoire")
            else:
                statut = {"valide": cfg.get("licence_valide_cache", True),
                          "en_essai": cfg.get("en_essai_cache", False),
                          "jours_restants": cfg.get("jours_restants", 0)}
                self._rafraichir_licence_fond(cle)
                _chrono("get_app_state: licence depuis la config (refresh en fond)")
            if statut.get("valide"):
                licence_ok = True
                if statut.get("en_essai"):
                    en_essai = True
                    jours_restants = statut.get("jours_restants", 0)
            else:
                licence_expired = True

        return {
            "licence_ok":        licence_ok,
            "licence_expired":   licence_expired,
            "en_essai":          en_essai,
            "jours_restants":    jours_restants,
            # Flag DÉDIÉ posé par complete_onboarding. doctor_name ne peut
            # plus servir de marqueur : l'inscription le stocke désormais
            # immédiatement (régression : la visite guidée ne se lançait
            # plus, le compte neuf paraissait déjà onboardé).
            # Rétro-compat : les installs d'avant le flag ont "micro" en
            # config (posé uniquement par complete_onboarding).
            "onboarding_done":   bool(cfg.get("onboarding_fait")) or "micro" in cfg,
            "doctor_name":       cfg.get("doctor_name", ""),
            "save_folder":       cfg.get("dossier_sauvegarde", ""),
            "gain_patient":      cfg.get("gain_patient", 1.0),
            "gain_mic":          cfg.get("gain_mic", 1.0),
            "theme":             cfg.get("theme") or "light",
            "version":           APP_VERSION,
            "mode_consultation": cfg.get("mode_consultation", ""),
            "devices_configured": bool(cfg.get("micro") or cfg.get("sortie")),
            "specialty":         cfg.get("specialty", ""),
        }

    def complete_onboarding(self, doctor_name, mic_name, output_name):
        """Valide l'onboarding et sauvegarde la config complète.
        Le nom n'est plus demandé pendant l'onboarding : celui de
        l'INSCRIPTION fait foi (doctor_name vide = le conserver)."""
        cfg = charger_config()
        if doctor_name.strip():
            cfg["doctor_name"] = doctor_name.strip()
        if not cfg.get("doctor_name"):
            return {"ok": False, "error": "Le nom du médecin est introuvable."}
        cfg["micro"]  = mic_name
        cfg["sortie"] = output_name
        cfg["onboarding_fait"] = True
        sauver_config(cfg)
        return {"ok": True}

    # ===== AUTH / LICENCE ====================================================

    # ---- Vérification d'email (inscription 2 étapes + mot de passe oublié) --

    def demander_code(self, email, type_code):
        """Demande l'envoi d'un code à 6 chiffres ('inscription' ou 'reset').
        Remonte le VRAI message serveur (email_pris, anti-abus…)."""
        res = _appel_api("demander-code", {"email": email, "type": type_code})
        if not res:
            return {"ok": False, "error": _message_erreur_api()}
        return res

    def verifier_code(self, email, code, type_code):
        """Vérifie le code reçu par email → {ok, jeton_verification} ou le
        message serveur exact (Code incorrect / expiré / trop de tentatives)."""
        res = _appel_api("verifier-code",
                         {"email": email, "code": code, "type": type_code})
        if not res:
            return {"ok": False, "error": _message_erreur_api()}
        return res

    def reinitialiser_mot_de_passe(self, email, jeton, nouveau_mdp):
        """Change le mot de passe après vérification email (jeton 'reset')."""
        res = _appel_api("reinitialiser-mot-de-passe", {
            "email": email, "jeton_verification": jeton,
            "nouveau_mot_de_passe": nouveau_mdp,
        })
        if not res:
            return {"ok": False, "error": _message_erreur_api()}
        return res

    def auth_inscription(self, nom, email, password, specialty="",
                         jeton_verification=""):
        """Inscrit un nouveau médecin (email préalablement vérifié par jeton).
        Stocke cle_licence et infos dans config."""
        res = _appel_api("inscription", {
            "nom": nom, "email": email,
            "mot_de_passe": password, "specialite": specialty,
            "jeton_verification": jeton_verification,
        })
        if not res:
            return {"ok": False, "error": _message_erreur_api()}
        if not res.get("ok"):
            erreur = res.get("error") or "Inscription refusée par le serveur."
            # Email déjà pris : message honnête + bascule vers la connexion
            # (le flag email_pris est lu par le JS).
            e_norm = erreur.lower()
            if "existe" in e_norm or ("déjà" in e_norm and
                                      ("utilis" in e_norm or "pris" in e_norm)):
                return {"ok": False, "email_pris": True,
                        "error": "Un compte existe déjà avec cet email. "
                                 "Connectez-vous plutôt."}
            return {"ok": False, "error": erreur}
        cfg = charger_config()
        cfg["cle_licence"]   = res.get("cle_licence", "")
        cfg["medecin_id"]    = res.get("medecin_id", "")
        cfg["doctor_name"]   = res.get("nom", nom)
        cfg["email"]         = email
        cfg["specialty"]     = specialty
        cfg["jours_restants"] = res.get("jours_restants", 0)
        sauver_config(cfg)
        return {
            "ok": True,
            "en_essai": res.get("en_essai", False),
            "jours_restants": res.get("jours_restants", 0),
        }

    def auth_connexion(self, email, password):
        """Connecte un médecin existant. Stocke cle_licence et infos dans config."""
        res = _appel_api("connexion", {"email": email, "mot_de_passe": password})
        if not res:
            return {"ok": False, "error": _message_erreur_api()}
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error", "Identifiants incorrects.")}
        cfg = charger_config()
        cfg["cle_licence"]   = res.get("cle_licence", "")
        cfg["medecin_id"]    = res.get("medecin_id", "")
        cfg["doctor_name"]   = res.get("nom", "")
        cfg["email"]         = email
        cfg["jours_restants"] = res.get("jours_restants", 0)
        sauver_config(cfg)
        expired = not res.get("valide", True)
        return {
            "ok": True,
            "expired":  expired,
            "en_essai": res.get("en_essai", False),
            "jours_restants": res.get("jours_restants", 0),
        }

    def ouvrir_lien_paiement(self, type_paiement="abonnement"):
        """Crée un lien de paiement Stripe via le backend et l'ouvre dans le navigateur."""
        cfg = charger_config()
        medecin_id = cfg.get("medecin_id", "")
        res = _appel_api("creer-paiement", {
            "medecin_id": medecin_id,
            "type": type_paiement,
        })
        url = res.get("url") if res else None
        if url:
            webbrowser.open(url)
            return {"ok": True}
        return {"ok": False, "error": "Impossible d'obtenir le lien de paiement."}

    def ouvrir_mailto(self, adresse):
        """Ouvre le client mail avec l'adresse de support."""
        webbrowser.open("mailto:" + adresse)
        return {"ok": True}

    def get_profile_info(self):
        """Retourne email et spécialité pour le panneau profil."""
        cfg = charger_config()
        return {
            "email":    cfg.get("email", ""),
            "specialty": cfg.get("specialty", ""),
        }

    def deconnecter(self):
        """Efface les infos de session (licence, médecin) sans toucher devices/theme."""
        cfg = charger_config()
        for key in ("cle_licence", "medecin_id", "doctor_name", "jours_restants", "email", "specialty"):
            cfg.pop(key, None)
        sauver_config(cfg)
        return {"ok": True}

    # ===== SETTINGS ==========================================================

    def get_settings(self):
        """Config + périphériques (pour la page Paramètres)."""
        cfg  = charger_config()
        devs = self.get_devices()
        return {
            "doctor_name":   cfg.get("doctor_name", ""),
            "specialty":     cfg.get("specialty", ""),
            "save_folder":   cfg.get("dossier_sauvegarde", ""),
            "gain_patient":  cfg.get("gain_patient", 1.0),
            "gain_mic":      cfg.get("gain_mic", 1.0),
            "theme":         cfg.get("theme") or "light",
            **devs,
        }

    def save_settings(self, data):
        """Sauvegarde les paramètres modifiés."""
        cfg = charger_config()
        for k in ("doctor_name", "save_folder"):
            if k in data and data[k] is not None:
                key = "dossier_sauvegarde" if k == "save_folder" else k
                cfg[key] = data[k]
        if "gain_patient" in data:
            v = max(0.0, min(1.5, float(data["gain_patient"])))
            cfg["gain_patient"] = v
            self.set_volume_patient(v)
        if "gain_mic" in data:
            v = max(0.0, min(1.5, float(data["gain_mic"])))
            cfg["gain_mic"] = v
            self.set_volume_mic(v)
        if "sortie" in data: cfg["sortie"] = data["sortie"]
        if "micro"  in data: cfg["micro"]  = data["micro"]
        if "theme"  in data:
            cfg["theme"] = "dark" if data["theme"] == "dark" else "light"
            appliquer_titlebar_theme(cfg["theme"])   # accorde la titlebar
        # Spécialité : persistée localement ET propagée au backend (best-effort).
        specialite_modifiee = ("specialty" in data
                               and (data["specialty"] or "") != cfg.get("specialty", ""))
        if "specialty" in data:
            cfg["specialty"] = data["specialty"] or ""
        sauver_config(cfg)
        if specialite_modifiee:
            self._maj_specialite_backend(cfg.get("medecin_id", ""), cfg["specialty"])
        return {"ok": True}

    def _maj_specialite_backend(self, medecin_id, specialite):
        """Propage la spécialité vers Supabase (best-effort, jamais bloquant :
        la config locale reste la source de vérité pour l'affichage)."""
        if not medecin_id:
            return
        try:
            _appel_api("maj-specialite",
                       {"medecin_id": medecin_id, "specialite": specialite})
        except Exception:
            pass

    def maj_specialite(self, specialite):
        """Met à jour la spécialité (config locale + backend). Exposée au JS."""
        cfg = charger_config()
        cfg["specialty"] = specialite or ""
        sauver_config(cfg)
        self._maj_specialite_backend(cfg.get("medecin_id", ""), cfg["specialty"])
        return {"ok": True}

    def apply_theme(self, theme):
        """Accorde la barre de titre Windows au thème courant. Appelé par le JS
        juste après avoir changé data-theme sur <html>. Ne plante jamais."""
        appliquer_titlebar_theme("dark" if theme == "dark" else "light")
        return {"ok": True}

    # ===== DÉMARRAGE WINDOWS (barre système) =================================

    def get_startup_state(self):
        """État RÉEL de la clé de registre Run (pas une valeur config)."""
        return {"enabled": demarrage.demarrage_actif()}

    def set_startup(self, enabled):
        """Active/désactive le lancement au démarrage de Windows.
        Fail-safe : échec registre → {ok:false, error} sans planter."""
        res = demarrage.activer_demarrage() if enabled else demarrage.desactiver_demarrage()
        return res

    def ouvrir_fenetre(self):
        """Restaure et affiche la fenêtre principale (depuis le tray)."""
        try:
            if self._main_win:
                self._main_win.show()
                self._main_win.restore()
        except Exception:
            pass
        return {"ok": True}

    def quitter_app(self):
        """Vraie sortie de l'application (menu « Quitter Écho » du tray)."""
        self._fermeture_reelle = True
        if getattr(self, "_tray", None):
            self._tray.arreter()
        self._close_all()
        threading.Thread(
            target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
        return {"ok": True}

    def backup_model_status(self):
        """État du modèle local de secours (présent + progression éventuelle)."""
        with _download_lock:
            import copy
            return {"present": modele_local_present(),
                    "whisper": copy.deepcopy(_download_state["whisper"]),
                    "running": _download_state["running"]}

    def start_backup_download(self):
        """Télécharge le modèle Whisper de secours À LA DEMANDE (1,6 Go).
        Retourne {ok, ready} : ready=True si déjà présent."""
        if modele_local_present():
            return {"ok": True, "ready": True}
        with _download_lock:
            if _download_state["running"]:
                return {"ok": True, "ready": False}
            _download_state["whisper"]["error"]      = None
            _download_state["whisper"]["done"]       = False
            _download_state["whisper"]["downloaded"] = 0
        threading.Thread(target=_run_downloads, args=(["whisper"],), daemon=True).start()
        return {"ok": True, "ready": False}

    def pick_folder(self):
        """Sélecteur de dossier natif (depuis n'importe quelle fenêtre)."""
        win = self._main_win or self._overlay_win or self._window
        if not win:
            return {"path": None}
        try:
            import webview as _wv
            paths = win.create_file_dialog(_wv.FOLDER_DIALOG)
            return {"path": paths[0] if paths else None}
        except Exception:
            return {"path": None}

    # ===== VOLUME ============================================================

    def set_volume_patient(self, ratio):
        global _GAIN_PATIENT
        with _gain_lock:
            _GAIN_PATIENT = max(0.0, min(1.5, float(ratio)))
        return {"ok": True}

    def set_volume_mic(self, ratio):
        global _GAIN_MIC
        with _gain_lock:
            _GAIN_MIC = max(0.0, min(1.5, float(ratio)))
        return {"ok": True}

    # ===== DÉMARRAGE / FIN DE CONSULTATION ===================================

    def begin_consultation(self, mode="tele", mic_name="", output_name=""):
        """Lance la consultation depuis la fenêtre principale.

        `mode` : "cabinet" (micro seul) ou "tele" (loopback + micro).
        Les périphériques sont lus depuis la config si non fournis (le flux
        normal ne passe plus par le sélecteur — cf. modale de choix du mode)."""
        cfg = charger_config()
        if not mic_name:
            mic_name = cfg.get("micro", "")
        if not output_name:
            output_name = cfg.get("sortie", "")
        self._mode = "cabinet" if mode == "cabinet" else "tele"
        # Attendre la fin des threads de la consultation précédente avant de
        # ré-armer stop_event (sinon ils survivraient au clear et doubleraient
        # la capture/transcription).
        old_threads = getattr(self, "_threads", [])
        if any(t.is_alive() for t in old_threads):
            stop_event.set()
            for t in old_threads:
                t.join(timeout=1.5)
        # Purger TOUTES les files de la consultation précédente (audio brut,
        # affichage, corrections en attente) — aucun résidu ne doit fuir dans
        # la consultation suivante lors d'un enchaînement « Patient suivant ».
        for q in (segment_queue, display_queue, _correction_queue):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        # Réinitialise l'état pour une nouvelle consultation.
        stop_event.clear()
        _reset_contexte()          # contexte dynamique Whisper (noms, médicaments)
        with self._lock:
            self._entries.clear()
            self._seg_index.clear()
        self._save_done  = False
        self._infos      = None
        self._resume     = None
        self._annexes    = []
        self._resume_status = "idle"
        self._resume_text   = None
        self._start_time    = datetime.datetime.now()
        self._saved_id        = None
        self._saved_file_path = None
        self._saved_annexes   = []
        self._saved_is_docx   = False

        result = self.start(mic_name, output_name, self._mode)
        if result.get("ok"):
            self._maj_tray("rouge")   # consultation en cours → icône rouge
            if self._main_win:
                self._main_win.minimize()
            if self._overlay_win:
                self._overlay_win.show()
                self._overlay_win.restore()
                # Réinitialise l'UI de l'overlay (chrono, transcript, état
                # d'attente) et lui transmet le mode courant.
                try:
                    self._overlay_win.evaluate_js(
                        "startConsultationUI('%s')" % self._mode)
                except Exception:
                    pass
        return result

    def patient_suivant(self):
        """Enchaîne une nouvelle consultation dans le MÊME mode et avec les
        mêmes périphériques que la précédente, sans repasser par la modale de
        choix de mode ni le sélecteur (« Patient suivant »).

        begin_consultation réinitialise intégralement l'état (transcript,
        contexte dynamique, files, chrono, threads de capture recréés) : aucune
        donnée du patient précédent ne fuit. En cas d'échec de démarrage
        (périphérique déconnecté…), renvoie {ok:false, error} — le JS retombe
        proprement sur l'accueil, jamais de blocage."""
        mode = getattr(self, "_mode", "tele")
        return self.begin_consultation(mode)

    def end_consultation(self):
        """Ferme l'overlay et restaure la fenêtre principale."""
        self._save_done = True
        self._maj_tray("vert")   # consultation terminée → icône verte (prêt)
        if self._overlay_win:
            self._overlay_win.hide()
        if self._main_win:
            self._main_win.restore()
            self._main_win.evaluate_js("onConsultationEnded()")
        stop_event.set()
        # Autoriser une nouvelle consultation (les threads de capture et de
        # transcription s'arrêtent via stop_event ; start() en relance de neufs).
        self._started = False
        # Présélection patient jamais consommée (consultation non sauvegardée) :
        # ne pas polluer la consultation suivante.
        self._patient_presel = None
        return {"ok": True}

    def request_quit(self):
        """Déclenche le flux de fermeture (depuis le bouton Quitter ou la croix).
        Retourne {needs_save: true} si des entrées non sauvegardées existent,
        {needs_save: false} si on peut fermer directement."""
        with self._lock:
            has_entries = bool(self._entries)
        if has_entries and not getattr(self, "_save_done", False):
            return {"needs_save": True}
        self._close_all()
        return {"needs_save": False}

    def force_quit(self):
        """Fermeture sans sauvegarde (confirmée par l'utilisateur)."""
        self._close_all()
        return {"ok": True}

    def _close_all(self):
        stop_event.set()
        for w in (self._overlay_win, self._main_win, self._window):
            if w:
                try:
                    w.destroy()
                except Exception:
                    pass

    # ===== BARRE SYSTÈME (tray) : état + minimisation =======================

    def _maj_tray(self, etat=None):
        """Met à jour la couleur de l'icône tray. Si etat=None, la déduit :
        gris (licence invalide) → rouge (consultation active) → vert (prêt)."""
        tray = getattr(self, "_tray", None)
        if not tray:
            return
        if etat is None:
            if not stop_event.is_set() and self._started:
                etat = "rouge"
            else:
                try:
                    cfg = charger_config()
                    cle = cfg.get("cle_licence", "")
                    ok = _verifier_licence_cachee(cle).get("valide", False) if cle else False
                except Exception:
                    ok = True   # fail-open : ne pas griser à tort hors ligne
                etat = "vert" if ok else "gris"
        tray.set_etat(etat)

    def minimiser_dans_tray(self):
        """Ferme (X) la fenêtre principale → minimisation dans la barre système
        au lieu de quitter. Renvoie {minimise, notice} pour le handler pywebview.

        Si une consultation est EN COURS, on NE minimise PAS silencieusement :
        le JS affiche la confirmation habituelle (on ne cache pas une fenêtre
        qui enregistre)."""
        consultation_active = self._started and not stop_event.is_set()
        if consultation_active:
            return {"minimise": False, "consultation_active": True}
        try:
            if self._main_win:
                self._main_win.hide()
        except Exception:
            pass
        # Notification système « continue en arrière-plan », une seule fois.
        cfg = charger_config()
        premiere = not cfg.get("tray_notice_vue")
        if premiere:
            cfg["tray_notice_vue"] = True
            sauver_config(cfg)
            tray = getattr(self, "_tray", None)
            if tray:
                tray.notifier("Écho continue en arrière-plan — retrouvez-le "
                              "près de l'horloge.")
        return {"minimise": True, "notice": premiere}

    # ===== FLUX DE SAUVEGARDE (orchestré depuis le JS de l'overlay) ==========

    def generate_resume_async(self):
        """Lance la génération du résumé via Groq dans un thread.
        JS pollera get_resume_status(). Si Groq échoue → statut "error"
        (la transcription est déjà sauvegardée, pas de blocage)."""
        self._resume_status = "loading"
        self._resume_text   = None

        def worker():
            try:
                self._resume_status = "generating"
                with self._lock:
                    n0 = len(self._entries)
                    entries = list(self._entries)
                # Mode cabinet : attribuer Médecin/Patient par analyse LLM du
                # contenu AVANT la correction (les tours arrivent en
                # « Conversation »). L'attribution peut scinder des tours →
                # le nombre de lignes peut augmenter. Ordre : attribution →
                # correction → résumé.
                if self._mode == "cabinet":
                    entries = correction.attribuer_locuteurs(entries)
                # Passe de correction LLM globale : le résumé ET le .docx
                # sont générés depuis le transcript corrigé.
                entries = correction.corriger_transcript_complet(entries)
                with self._lock:
                    # Réécrire seulement si aucun nouveau segment n'est arrivé
                    # entre-temps (sinon on écraserait de la parole récente).
                    if entries and len(self._entries) == n0:
                        self._entries[:] = entries
                # Le résumé ne se construit QUE sur les lignes fiables (attribuées
                # Médecin/Patient). Les lignes douteuses « [?] » / « Conversation »
                # restent dans le transcript annexe mais ne doivent JAMAIS être
                # résumées : une info médicale non fiable dans le compte-rendu
                # est le pire scénario possible.
                transcript = "\n".join(
                    "[%s] %s : %s" % (h, LOCUTEUR_FICHIER.get(loc, loc), t)
                    for h, loc, t in entries
                    if not correction.est_ligne_douteuse(loc, t))
                texte = groq_summarize(transcript, GROQ_API_KEY)
                self._resume_text   = texte
                self._resume_status = "done" if texte else "error"
            except Exception:
                # Un échec du résumé n'empêche jamais la sauvegarde : le .docx
                # est déjà écrit avant cette étape (résumé optionnel).
                _caplog.error("generate_resume worker: échec\n%s",
                              traceback.format_exc())
                self._resume_status = "error"

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def get_resume_status(self):
        return {
            "status": getattr(self, "_resume_status", "idle"),
            "texte":  getattr(self, "_resume_text",   None) or "",
        }

    def open_save_dialog(self, default_filename):
        """Ouvre la boîte 'Enregistrer sous' .docx via pywebview."""
        win = self._overlay_win or self._window
        if not win:
            return {"path": None}
        try:
            import webview as _wv
            cfg     = charger_config()
            initdir = cfg.get("dossier_sauvegarde") or ""
            paths   = win.create_file_dialog(
                _wv.SAVE_DIALOG,
                save_filename=default_filename,
                directory=initdir if os.path.isdir(initdir) else "",
                file_types=("Document Word (*.docx)", "Tous les fichiers (*.*)")
            )
            return {"path": paths[0] if paths else None}
        except Exception:
            return {"path": None}

    def perform_save(self, file_path, resume_text, annexes):
        """Écrit le .docx, enregistre dans consultations.json."""
        if not self._infos:
            return {"ok": False, "error": "Informations patient manquantes."}
        now = getattr(self, "_start_time", None) or datetime.datetime.now()
        with self._lock:
            entries = list(self._entries)
        try:
            storage.ecrire_docx(file_path, self._infos, now,
                                resume_text or None, entries, annexes=annexes or [])
        except Exception as exc:
            # Fallback txt.
            txt_path = re.sub(r"\.docx$", ".txt", file_path, flags=re.IGNORECASE)
            try:
                storage.ecrire_txt_secours(txt_path, self._infos, now, resume_text, entries)
                file_path = txt_path
            except Exception:
                return {"ok": False, "error": str(exc)}

        # Mémorise le dossier.
        dossier = os.path.dirname(file_path)
        cfg = charger_config()
        cfg["dossier_sauvegarde"] = dossier
        sauver_config(cfg)

        # Ajoute au journal. Les entries sont stockées pour permettre de
        # réécrire le .docx lors d'une validation DIFFÉRÉE du compte-rendu
        # (les self._entries seront perdues à la consultation suivante).
        cid = str(uuid.uuid4())
        dur = int((datetime.datetime.now() - now).total_seconds() / 60)
        storage.ajouter_consultation(chemin_consultations(), {
            "id":           cid,
            "date":         now.isoformat(),
            "patient":      self._infos,
            "summary":      resume_text or "",
            "file_path":    file_path,
            "duration_min": dur,
            "cr_valide":    False,          # compte-rendu à valider par le médecin
            "cr_elements":  None,           # extraction IA (remplie en arrière-plan)
            "entries":      [list(e) for e in entries],
            "annexes":      annexes or [],  # pour réécrire le .docx en différé
            "demo":         bool(_demo_capture["actif"]),   # consultation de démonstration
        })
        if _demo_capture["actif"]:
            self._finir_demo(entries)

        # Mémorise pour un éventuel ajout de résumé ultérieur (flux non bloquant).
        self._saved_id        = cid
        self._saved_file_path = file_path
        self._saved_annexes   = annexes or []
        self._saved_is_docx   = file_path.lower().endswith(".docx")

        # Extraction structurée en arrière-plan (attribution cabinet incluse) :
        # l'écran de validation la récupérera via get_cr_elements().
        threading.Thread(target=self._extraction_cr_worker,
                         args=(cid,), daemon=True).start()
        return {"ok": True, "file_path": file_path}

    def _extraction_cr_worker(self, cid):
        """Pipeline post-sauvegarde : attribution locuteurs (cabinet) →
        correction globale → extraction JSON des éléments du compte-rendu →
        consultations.json. Le .docx est réécrit avec le transcript amélioré.
        Best-effort intégral : le document initial est déjà sauvegardé."""
        try:
            with self._lock:
                n0 = len(self._entries)
                entries = list(self._entries)
            if self._mode == "cabinet":
                entries = correction.attribuer_locuteurs(entries)
            entries = correction.corriger_transcript_complet(entries)
            with self._lock:
                if entries and len(self._entries) == n0:
                    self._entries[:] = entries
            # Transcript fiable uniquement (lignes [?]/Conversation exclues).
            transcript = "\n".join(
                "[%s] %s : %s" % (h, LOCUTEUR_FICHIER.get(loc, loc), t)
                for h, loc, t in entries
                if not correction.est_ligne_douteuse(loc, t))
            elements = extraire_elements_cr(transcript, GROQ_API_KEY)
            storage.maj_consultation_cr(
                chemin_consultations(), cid,
                cr_elements=elements, entries=[list(e) for e in entries])
            # Réécrire le .docx avec le transcript attribué/corrigé (sans résumé
            # pour l'instant — il viendra à la validation).
            fp = getattr(self, "_saved_file_path", None)
            if fp and getattr(self, "_saved_is_docx", False) and self._infos:
                now = getattr(self, "_start_time", None) or datetime.datetime.now()
                storage.ecrire_docx(fp, self._infos, now, None, entries,
                                    annexes=getattr(self, "_saved_annexes", []) or [])
        except Exception:
            _caplog.error("_extraction_cr_worker: %s", traceback.format_exc())
            journaliser("_extraction_cr_worker: extraction du CR échouée (cid=%s)" % cid)
            try:
                storage.maj_consultation_cr(chemin_consultations(), cid,
                                            cr_elements=elements_vides())
            except Exception:
                pass

    # ===== VALIDATION DU COMPTE-RENDU (cases à cocher) ======================

    def get_cr_elements(self, cid):
        """Éléments extraits pour l'écran de validation.
        Retourne {ready, elements, cr_valide, patient, date}."""
        for c in storage.charger_consultations(chemin_consultations()):
            if isinstance(c, dict) and c.get("id") == cid:
                return {
                    "ready":     c.get("cr_elements") is not None,
                    "elements":  c.get("cr_elements") or elements_vides(),
                    "cr_valide": bool(c.get("cr_valide")),
                    "patient":   c.get("patient") or {},
                    "date":      c.get("date", ""),
                }
        return {"ready": False, "elements": elements_vides(),
                "cr_valide": False, "patient": {}, "date": ""}

    def valider_cr(self, cid, elements):
        """Valide le compte-rendu : seuls les éléments cochés (reçus ici)
        vont dans le résumé du .docx. Réécrit le document et marque
        cr_valide=True dans l'historique."""
        record = None
        for c in storage.charger_consultations(chemin_consultations()):
            if isinstance(c, dict) and c.get("id") == cid:
                record = c
                break
        if not record:
            return {"ok": False, "error": "Consultation introuvable."}
        resume_txt = elements_vers_resume(elements or {})
        fp = record.get("file_path") or ""
        entries = [tuple(e) for e in (record.get("entries") or [])]
        if fp.lower().endswith(".docx") and record.get("patient"):
            try:
                d = datetime.datetime.fromisoformat(record.get("date", ""))
            except Exception:
                d = datetime.datetime.now()
            try:
                storage.ecrire_docx(fp, record["patient"], d, resume_txt,
                                    entries,
                                    annexes=record.get("annexes") or [])
            except Exception as exc:
                return {"ok": False,
                        "error": "Document inaccessible : " + str(exc)}
        try:
            storage.maj_consultation_cr(chemin_consultations(), cid,
                                        cr_elements=elements,
                                        cr_valide=True, summary=resume_txt)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def ignorer_cr(self, cid):
        """« Ignorer le compte-rendu » : transcript seul, plus rien à valider."""
        try:
            storage.maj_consultation_cr(chemin_consultations(), cid,
                                        cr_valide=True)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_cr_a_valider(self):
        """Nombre + id de la consultation la plus récente à valider
        (bandeau accueil et ouverture auto post-consultation)."""
        en_attente = [c for c in storage.charger_consultations(chemin_consultations())
                      if isinstance(c, dict) and c.get("cr_valide") is False]
        return {"count": len(en_attente),
                "dernier_id": en_attente[0].get("id") if en_attente else None}

    def valider_cr_groupe(self, items):
        """Validation groupée (vue « Tout voir en liste »).

        `items` : [{cid, elements, nom?, prenom?}] — les comptes-rendus que le
        médecin a VUS et confirmés en liste, avec leurs éléments cochés.

        RÈGLE : une consultation sans patient identifié (nom_a_saisir et aucun
        nom saisi en ligne) est IGNORÉE — jamais de compte-rendu au dossier
        sans nom. Si un nom est fourni, la consultation est nommée puis validée.

        Retourne {ok, valides: [cid...], ignores: [cid...]}."""
        by_id = {c.get("id"): c
                 for c in storage.charger_consultations(chemin_consultations())
                 if isinstance(c, dict)}
        valides, ignores = [], []
        for it in items or []:
            cid = it.get("cid")
            record = by_id.get(cid)
            if not record:
                continue
            if record.get("nom_a_saisir"):
                nom = (it.get("nom") or "").strip()
                if not nom:
                    ignores.append(cid)          # pas de nom → pas de validation
                    continue
                r = self.nommer_consultation(cid, nom, it.get("prenom", ""))
                if not r.get("ok"):
                    ignores.append(cid)
                    continue
            r = self.valider_cr(cid, it.get("elements") or {})
            (valides if r.get("ok") else ignores).append(cid)
        return {"ok": True, "valides": valides, "ignores": ignores}

    def finalize_with_resume(self, resume_text):
        """Réécrit le .docx déjà sauvegardé en y ajoutant le résumé en tête,
        au même emplacement, et met à jour le journal. Le document existe
        déjà : cette étape est purement optionnelle et non bloquante."""
        fp = getattr(self, "_saved_file_path", None)
        if not fp or not self._infos:
            return {"ok": False, "error": "Aucun document à compléter."}
        if not getattr(self, "_saved_is_docx", False):
            return {"ok": False,
                    "error": "Le document a été enregistré en texte ; "
                             "le résumé ne peut pas y être réinséré automatiquement."}
        now = getattr(self, "_start_time", None) or datetime.datetime.now()
        with self._lock:
            entries = list(self._entries)
        annexes = getattr(self, "_saved_annexes", []) or []
        try:
            storage.ecrire_docx(fp, self._infos, now,
                                resume_text or None, entries, annexes=annexes)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        # Met à jour le résumé dans l'historique (best-effort).
        cid = getattr(self, "_saved_id", None)
        if cid:
            try:
                storage.maj_consultation_resume(chemin_consultations(), cid,
                                               resume_text or "")
            except Exception:
                pass
        return {"ok": True}

    def finalize_cabinet_docx(self):
        """Cabinet : garantit que le .docx porte les étiquettes Médecin/Patient
        (attribution) et les termes corrigés, MÊME si le médecin décline le
        résumé. Best-effort, en arrière-plan : le .docx existe déjà (labels
        « Conversation »), cette étape l'améliore sans bloquer la fermeture.
        La sauvegarde du travail ne dépend donc jamais du succès de l'IA."""
        if self._mode != "cabinet":
            return {"ok": True}
        fp = getattr(self, "_saved_file_path", None)
        if not fp or not getattr(self, "_saved_is_docx", False) or not self._infos:
            return {"ok": True}
        now = getattr(self, "_start_time", None) or datetime.datetime.now()
        annexes = getattr(self, "_saved_annexes", []) or []
        infos = self._infos
        resume_txt = getattr(self, "_resume_text", None)
        with self._lock:
            n0 = len(self._entries)
            entries = list(self._entries)

        def worker():
            try:
                ent = entries
                # Attribution déjà faite si aucune ligne « Conversation » brute ne
                # subsiste (les résiduelles indécidables portent le marqueur « [?] »).
                besoin = any(loc == "Conversation"
                             and not (t or "").lstrip().startswith("[?]")
                             for _, loc, t in ent)
                if besoin:
                    ent = correction.attribuer_locuteurs(ent)
                    ent = correction.corriger_transcript_complet(ent)
                    with self._lock:
                        if ent and len(self._entries) == n0:
                            self._entries[:] = ent
                storage.ecrire_docx(fp, infos, now, resume_txt or None, ent,
                                    annexes=annexes)
            except Exception:
                _caplog.error("finalize_cabinet_docx: %s", traceback.format_exc())

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    # ===== SAISIE DIFFÉRÉE DU NOM (« Nommer plus tard ») =====================

    def save_sans_nom(self, annexes=None):
        """« Nommer plus tard » : sauvegarde immédiate SANS nom de patient.

        - Libellé provisoire « Consultation de 9h15 » (heure de début)
        - Fichier .docx écrit automatiquement dans le dossier de sauvegarde
          configuré (ou Documents), sans boîte de dialogue — le médecin
          enchaîne immédiatement
        - nom_a_saisir=True dans l'historique ; la détection du nom prononcé
          dans la conversation tourne en arrière-plan (ne bloque JAMAIS)."""
        debut = getattr(self, "_start_time", None) or datetime.datetime.now()
        libelle = "Consultation de %dh%02d" % (debut.hour, debut.minute)
        self._infos = {"nom": libelle, "prenom": "", "naissance": "", "motif": ""}

        # Chemin automatique : dossier configuré > Documents. Déduplication.
        cfg = charger_config()
        dossier = cfg.get("dossier_sauvegarde") or ""
        if not dossier or not os.path.isdir(dossier):
            dossier = os.path.join(os.path.expanduser("~"), "Documents")
            os.makedirs(dossier, exist_ok=True)
        base = "Consultation_%s_%02dh%02d" % (
            debut.strftime("%Y-%m-%d"), debut.hour, debut.minute)
        fp = os.path.join(dossier, base + ".docx")
        n = 2
        while os.path.exists(fp):
            fp = os.path.join(dossier, "%s_%d.docx" % (base, n))
            n += 1

        res = self.perform_save(fp, "", annexes or [])
        if not res.get("ok"):
            return res
        cid = self._saved_id
        try:
            storage.maj_consultation_cr(chemin_consultations(), cid,
                                        nom_a_saisir=True)
        except Exception:
            pass
        # Détection du nom prononcé — SUGGESTION uniquement, jamais un
        # remplissage automatique (une erreur de nom au dossier est grave).
        with self._lock:
            entries = list(self._entries)

        def worker():
            try:
                suggere = correction.detecter_nom_patient(entries)
                if suggere and suggere.get("nom"):
                    storage.maj_consultation_cr(chemin_consultations(), cid,
                                                nom_suggere=suggere)
            except Exception:
                _caplog.error("detecter_nom worker: %s", traceback.format_exc())

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "file_path": fp, "cid": cid}

    def get_consultations_a_nommer(self):
        """File des consultations sans nom, pour la saisie en série.
        [{id, libelle, date, duration_min, extrait (4 lignes), nom_suggere}]"""
        out = []
        for c in storage.charger_consultations(chemin_consultations()):
            if not (isinstance(c, dict) and c.get("nom_a_saisir")):
                continue
            entries = c.get("entries") or []
            extrait = [
                {"loc": LOCUTEUR_FICHIER.get(e[1], e[1]), "texte": e[2]}
                for e in entries[:4] if len(e) >= 3
            ]
            out.append({
                "id":           c.get("id"),
                "libelle":      (c.get("patient") or {}).get("nom", ""),
                "date":         c.get("date", ""),
                "duration_min": c.get("duration_min", 0),
                "extrait":      extrait,
                "nom_suggere":  c.get("nom_suggere") or None,
            })
        return out

    def nommer_consultation(self, cid, nom, prenom=""):
        """Nomme une consultation différée : remplace le libellé provisoire,
        retire le flag nom_a_saisir et réécrit l'en-tête du .docx (best-effort)."""
        nom = (nom or "").strip()
        if not nom:
            return {"ok": False, "error": "Le nom est obligatoire."}
        record = None
        for c in storage.charger_consultations(chemin_consultations()):
            if isinstance(c, dict) and c.get("id") == cid:
                record = c
                break
        if not record:
            return {"ok": False, "error": "Consultation introuvable."}
        patient = dict(record.get("patient") or {})
        patient["nom"] = nom
        patient["prenom"] = (prenom or "").strip()
        try:
            storage.maj_consultation_cr(chemin_consultations(), cid,
                                        patient=patient, nom_a_saisir=False)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        # Réécrire l'en-tête patient du .docx (best-effort, non bloquant).
        fp = record.get("file_path") or ""
        if fp.lower().endswith(".docx") and os.path.isfile(fp):
            try:
                d = datetime.datetime.fromisoformat(record.get("date", ""))
            except Exception:
                d = datetime.datetime.now()
            try:
                storage.ecrire_docx(
                    fp, patient, d, record.get("summary") or None,
                    [tuple(e) for e in (record.get("entries") or [])],
                    annexes=record.get("annexes") or [])
            except Exception:
                _caplog.error("nommer_consultation docx: %s",
                              traceback.format_exc())
        return {"ok": True}

    def get_derniere_consultation_flags(self):
        """Flags de la consultation la plus récente (routage post-consultation
        dans la fenêtre principale : nommage différé vs écran de validation)."""
        data = storage.charger_consultations(chemin_consultations())
        if not data or not isinstance(data[0], dict):
            return {"id": None, "a_nommer": False, "a_valider": False}
        c = data[0]
        return {
            "id":        c.get("id"),
            "a_nommer":  bool(c.get("nom_a_saisir")),
            "a_valider": c.get("cr_valide") is False,
        }

    # ===== HISTORIQUE =========================================================

    def get_consultations(self):
        data = storage.charger_consultations(chemin_consultations())
        # Descripteur de ligne calculé à la source (motif validé > extrait >
        # première réplique patient) : les vues n'ont qu'à l'afficher.
        for c in data:
            texte, typ = storage.descripteur_consultation(c)
            c["descripteur"] = texte
            c["descripteur_type"] = typ
        return data

    def get_synthese_patient(self, consultation_ids):
        """Encart de synthèse de la fiche patient (dernière consultation +
        traitements récents agrégés sur les consultations validées)."""
        data = storage.charger_consultations(chemin_consultations())
        return storage.synthese_patient(data, consultation_ids)

    def get_patients(self):
        """Patients distincts : consultations + patients créés manuellement."""
        consultations = storage.charger_consultations(chemin_consultations())
        manuels = storage.charger_patients_manuels(chemin_patients())
        return storage.extraire_patients(consultations, manuels)

    def ajouter_patient(self, nom, prenom="", ddn=""):
        """Crée un patient à la main (vue Patients). Refuse les doublons."""
        try:
            ok = storage.ajouter_patient_manuel(chemin_patients(), nom, prenom, ddn)
            if not ok:
                return {"ok": False,
                        "error": "Ce patient existe déjà (ou le nom est vide)."}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- Patient présélectionné (consultation lancée depuis une fiche) -----

    def set_patient_preselectionne(self, nom, prenom="", ddn=""):
        """Mémorise le patient pour pré-remplir le formulaire de l'overlay."""
        self._patient_presel = {"nom": nom or "", "prenom": prenom or "",
                                "ddn": ddn or ""}
        return {"ok": True}

    def get_patient_preselectionne(self):
        """Consommé une seule fois par l'overlay à l'ouverture du formulaire."""
        p = getattr(self, "_patient_presel", None)
        self._patient_presel = None
        return p or {}

    def suggerer_patients(self, query):
        """Autocomplétion du champ Nom (max 5 patients, préfixe nom/prénom)."""
        if not query or len(query.strip()) < 2:
            return []
        consultations = storage.charger_consultations(chemin_consultations())
        return storage.rechercher_patients(consultations, query)

    def get_stats(self):
        """Statistiques de l'accueil calculées depuis consultations.json :
        nb ce mois, nb cette semaine, patients distincts, durée moyenne (min)."""
        consultations = storage.charger_consultations(chemin_consultations())
        now = datetime.datetime.now()
        debut_mois = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # Lundi de la semaine courante.
        debut_sem = (now - datetime.timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)

        mois = semaine = 0
        durees = []
        for c in consultations:
            if not isinstance(c, dict):
                continue
            # La démonstration ne compte pas dans l'activité du médecin.
            if c.get("demo") is True:
                continue
            try:
                d = datetime.datetime.fromisoformat(c.get("date", ""))
            except Exception:
                d = None
            if d is not None:
                if d >= debut_mois:
                    mois += 1
                if d >= debut_sem:
                    semaine += 1
            dm = c.get("duration_min")
            if isinstance(dm, (int, float)) and dm > 0:
                durees.append(dm)

        duree_moy = round(sum(durees) / len(durees)) if durees else 0
        # Patients distincts : même regroupement que la vue Patients
        # (clé nom+prénom normalisés — cf. storage.extraire_patients).
        nb_patients = len(storage.extraire_patients(consultations))
        return {"mois": mois, "semaine": semaine,
                "patients": nb_patients, "duree_moy": duree_moy}

    def delete_consultation(self, cid):
        """Retire l'entrée d'id `cid` de consultations.json.

        Le fichier .docx sur le disque est conservé.
        """
        try:
            storage.supprimer_consultation(chemin_consultations(), cid)
            return {"ok": True}
        except (PermissionError, OSError):
            return {"ok": False,
                    "error": "Fichier temporairement inaccessible. Réessayez."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_consultation_with_file(self, cid):
        """Retire l'entrée d'id `cid` et supprime aussi le .docx s'il existe.

        Si le fichier n'existe pas/plus, l'entrée est quand même retirée
        sans erreur.
        """
        try:
            storage.supprimer_consultation_avec_fichier(chemin_consultations(), cid)
            return {"ok": True}
        except (PermissionError, OSError):
            return {"ok": False,
                    "error": "Fichier temporairement inaccessible. Réessayez."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ===== FICHIERS (OS) ======================================================

    def open_file_os(self, path):
        try:
            os.startfile(path)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_folder_os(self, path):
        try:
            subprocess.Popen(["explorer", "/select,", path])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def file_exists(self, path):
        return {"exists": bool(path and os.path.isfile(path))}

    def get_patient_infos(self):
        """Retourne les infos patient en cours (pour le nom de fichier par défaut)."""
        return self._infos or {}

    # ===== MODÈLES — VÉRIFICATION ET TÉLÉCHARGEMENT ==========================

    def check_models(self):
        """Vérifie les modèles présents. Groq étant le moteur principal pour
        la transcription ET le résumé, AUCUN modèle n'est requis au premier
        lancement (Whisper de secours téléchargé à la demande)."""
        return {"whisper_ok": whisper_ok()}

    def start_downloads(self):
        """Premier lancement : aucun téléchargement (Groq cloud pour tout).
        Le modèle Whisper de secours est récupéré à la demande si besoin."""
        import logging
        try:
            with _download_lock:
                if _download_state["running"]:
                    return {"ok": True, "msg": "already running"}
                _download_state["whisper"]["done"] = True
            logging.debug("start_downloads: aucun modèle à télécharger (Groq cloud)")
            return {"ok": True}
        except Exception as e:
            logging.exception("start_downloads: exception")
            return {"ok": False, "error": str(e)}

    def get_download_progress(self):
        """Retourne l'état complet des téléchargements (JSON-serializable)."""
        with _download_lock:
            import copy
            return copy.deepcopy(_download_state)

    def retry_download(self, model):
        """Remet error=None et relance le téléchargement d'un modèle spécifique."""
        if model != "whisper":
            return {"ok": False, "error": "modèle inconnu"}
        with _download_lock:
            if _download_state["running"]:
                return {"ok": False, "error": "Téléchargement déjà en cours."}
            _download_state[model]["error"]      = None
            _download_state[model]["done"]       = False
            _download_state[model]["downloaded"] = 0
        threading.Thread(target=_run_downloads, args=([model],), daemon=True).start()
        return {"ok": True}

    # ===== MISE À JOUR AUTOMATIQUE ===========================================

    def get_update_info(self):
        """Info de mise à jour (cache rempli par le thread de démarrage ;
        calcul synchrone si pas encore prêt). Jamais bloquant pour l'UI."""
        with _update_lock:
            if _update_info is not None:
                return _update_info
        info = verifier_mise_a_jour()
        with _update_lock:
            globals()["_update_info"] = info
        return info

    def start_update_download(self, url):
        """Télécharge l'installeur de mise à jour dans %TEMP% (progression via
        get_download_progress()['update'])."""
        if not url:
            return {"ok": False, "error": "URL de mise à jour manquante."}
        threading.Thread(target=_dl_update, args=(url,), daemon=True).start()
        return {"ok": True}

    def install_update(self):
        """Lance l'installeur en silencieux puis ferme l'app pour qu'Inno Setup
        remplace les fichiers (données %APPDATA%\\Echo préservées)."""
        path = _update_file or os.path.join(tempfile.gettempdir(), "EchoSetup.exe")
        if not os.path.isfile(path):
            return {"ok": False, "error": "Fichier d'installation introuvable."}
        try:
            subprocess.Popen([path, "/VERYSILENT", "/NORESTART", "/CLOSEAPPLICATIONS"])
        except Exception as e:
            return {"ok": False, "error": str(e)}
        threading.Thread(target=self._quit_for_update, daemon=True).start()
        return {"ok": True}

    def _quit_for_update(self):
        """Laisse le temps à l'installeur de démarrer puis ferme l'app."""
        time.sleep(0.6)
        try:
            self._close_all()
        except Exception:
            pass
        os._exit(0)


# ----------------------------- MAIN (pywebview) ------------------------------

def _main_webview():
    """Point d'entrée pywebview — deux fenêtres distinctes."""
    _chrono("main() atteint")
    import webview
    _chrono("import webview (pywebview)")

    api    = Api()
    _chrono("init Api")
    ui_dir = ressource("ui")

    # Vérification de mise à jour en arrière-plan (non bloquant, silencieux).
    threading.Thread(target=_warm_update_check, daemon=True).start()

    # Fenêtre 1 : application principale (accueil / paramètres / historique).
    main_win = webview.create_window(
        "Écho",
        os.path.join(ui_dir, "main_window.html"),
        js_api=api,
        width=870, height=660,
        min_size=(720, 520),
        background_color="#F7F8FA",
    )
    # Fenêtre 2 : overlay de transcription (toujours au premier plan).
    overlay_win = webview.create_window(
        "Écho — Consultation en cours",
        os.path.join(ui_dir, "index.html"),
        js_api=api,
        width=560, height=720,
        min_size=(480, 520),
        on_top=True,
        background_color="#080E1C",
    )

    api._main_win    = main_win
    api._overlay_win = overlay_win
    api._window      = overlay_win   # compatibilité Phase 1

    demarrage_tray = "--tray" in sys.argv   # lancé au démarrage Windows

    # Intercepter la fermeture de l'overlay via la croix Windows.
    def on_overlay_closing():
        with api._lock:
            has_entries = bool(api._entries)
        if has_entries and not api._save_done:
            overlay_win.evaluate_js("handleWindowClose()")
            return False
        return True

    overlay_win.events.closing += on_overlay_closing

    # Fermeture (X) de la fenêtre principale : minimiser dans la barre système
    # au lieu de quitter — sauf « Quitter Écho » (fermeture réelle) et sauf
    # consultation en cours (confirmation demandée, pas de masquage silencieux).
    def on_main_closing():
        if api._fermeture_reelle:
            return True   # laisser fermer
        res = api.minimiser_dans_tray()
        if res.get("consultation_active"):
            # Une consultation enregistre → confirmation habituelle, ne pas cacher.
            try:
                overlay_win.show(); overlay_win.restore()
                overlay_win.evaluate_js("handleWindowClose()")
            except Exception:
                pass
        return False   # ne jamais fermer via la croix (tray only)

    main_win.events.closing += on_main_closing
    _chrono("fenêtres créées (create_window x2)")
    # Premier rendu réel de la fenêtre principale.
    main_win.events.shown += (lambda: _chrono("fenêtre principale AFFICHÉE (premier rendu)"))

    # --- Icône de la barre système (best-effort : app OK sans icône) ---
    try:
        from tray import EchoTray
        api._tray = EchoTray(
            on_open=lambda: api.ouvrir_fenetre(),
            on_new=lambda: (api.ouvrir_fenetre(),
                            _safe_js(main_win, "openConsultationFlow && openConsultationFlow()")),
            on_quit=lambda: api.quitter_app(),
        )
        if not api._tray.demarrer():
            api._tray = None
    except Exception:
        api._tray = None
    _chrono("tray démarré (pystray + PIL)")

    def on_start():
        api._webview_mod = webview
        overlay_win.hide()   # L'overlay est caché jusqu'au début d'une consultation.
        # Lancé au démarrage Windows (--tray) : rester dans la barre système,
        # sans ouvrir la fenêtre. La vérif de licence tourne quand même (JS).
        if demarrage_tray:
            try:
                main_win.hide()
            except Exception:
                pass
        # Accorde la barre de titre au thème enregistré (best-effort, Windows 11).
        try:
            appliquer_titlebar_theme(charger_config().get("theme") or "light")
        except Exception:
            pass
        # État initial de l'icône (vert / gris selon la licence).
        threading.Thread(target=lambda: (time.sleep(1.0), api._maj_tray()),
                         daemon=True).start()

    _chrono("webview.start() appelé (boucle GUI)")
    webview.start(func=on_start, debug=("--dev" in sys.argv))
    stop_event.set()
    if api._tray:
        api._tray.arreter()
    time.sleep(0.2)


def _safe_js(win, code):
    """Exécute du JS dans une fenêtre sans jamais lever (thread tray)."""
    try:
        win.evaluate_js(code)
    except Exception:
        pass


def main():
    # Toute exception NON attrapée (main + threads) part dans
    # %APPDATA%\Echo\erreurs.log — plus aucune panne invisible.
    installer_hooks()
    if "--tk" in sys.argv:
        _main_tk()
    else:
        _main_webview()


def selftest():
    """Validation headless de l'environnement (installeur léger).
    Vérifie les imports critiques et l'état des modèles sans les télécharger.
    Écrit le résultat dans un log + code de sortie."""
    import traceback
    log = os.path.join(tempfile.gettempdir(), "echo_selftest.log")
    try:
        import av          # noqa: F401
        import ctranslate2 # noqa: F401
        import onnxruntime # noqa: F401
        from huggingface_hub import hf_hub_download  # noqa: F401 — requis pour dl
        frozen = getattr(sys, "frozen", False)
        w_ok   = whisper_ok()
        models_info = "whisper=%s" % (w_ok,)
        # Charge le modèle Whisper si disponible, sinon valide juste les imports.
        if w_ok:
            from faster_whisper import WhisperModel   # import différé
            source = chemin_modele()
            model  = WhisperModel(source, device=DEVICE, compute_type=COMPUTE_TYPE)
            segs, _ = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32),
                                       language=LANGUAGE, vad_filter=True, beam_size=1)
            list(segs)
            models_info += " whisper_loaded=True"
        msg  = ("SELFTEST OK | frozen=%s | meipass=%s | models=%s | models_dir=%s"
                % (frozen, getattr(sys, "_MEIPASS", "-"),
                   models_info, models_dir()))
        code = 0
    except Exception:
        msg  = "SELFTEST FAIL\n" + traceback.format_exc()
        code = 1
    try:
        with open(log, "w", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    print(msg)
    sys.exit(code)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
