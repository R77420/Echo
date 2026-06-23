"""Tests du module audio.py (RMS, conversion WAV, filtre de silence, VAD).

Aucun périphérique réel n'est utilisé : on teste la logique pure.
Lancer : pytest tests/test_audio.py -v
"""

import os
import sys
import struct

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio  # noqa: E402


def _sinus(freq=440, duree_s=0.5, sr=audio.SAMPLE_RATE, amp=0.5):
    t = np.linspace(0, duree_s, int(sr * duree_s), endpoint=False)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# --------------------------------------------------------------------------- #
#  RMS
# --------------------------------------------------------------------------- #

def test_rms_silence():
    """Signal nul → RMS = 0, donc sous le seuil."""
    z = np.zeros(audio.SAMPLE_RATE, dtype=np.float32)
    assert audio.rms(z) == 0.0
    assert audio.rms(z) < audio.RMS_MIN


def test_rms_signal():
    """Sinusoïde d'amplitude A → RMS ≈ A/√2."""
    amp = 0.5
    r = audio.rms(_sinus(amp=amp))
    assert r == pytest.approx(amp / np.sqrt(2), rel=0.02)
    assert r > audio.RMS_MIN          # un vrai signal passe le seuil


def test_rms_vide_ou_none():
    assert audio.rms(None) == 0.0
    assert audio.rms(np.array([], dtype=np.float32)) == 0.0


# --------------------------------------------------------------------------- #
#  Filtre anti-hallucination (RMS < seuil → None)
# --------------------------------------------------------------------------- #

def test_rms_filtre_hallucination():
    """garder_si_audible : quasi-silence → None, vrai signal → l'audio."""
    silence = (np.random.randn(audio.SAMPLE_RATE).astype(np.float32)) * 0.001
    assert audio.rms(silence) < audio.RMS_MIN
    assert audio.garder_si_audible(silence) is None

    fort = _sinus(amp=0.5)
    assert audio.garder_si_audible(fort) is fort   # renvoie le même tableau


# --------------------------------------------------------------------------- #
#  Conversion WAV
# --------------------------------------------------------------------------- #

def test_conversion_wav():
    """numpy → octets WAV valides (en-tête RIFF/WAVE) et relisibles."""
    sig = _sinus(amp=0.3, duree_s=0.25)
    data = audio.audio_to_wav_bytes(sig)

    assert isinstance(data, (bytes, bytearray))
    assert data[0:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    # Taille RIFF cohérente avec la taille du fichier.
    riff_size = struct.unpack("<I", data[4:8])[0]
    assert riff_size == len(data) - 8

    # Relecture : même nombre d'échantillons, bon sample rate.
    import io
    import soundfile as sf
    relu, sr = sf.read(io.BytesIO(data))
    assert sr == audio.SAMPLE_RATE
    assert len(relu) == len(sig)


def test_conversion_wav_buffer_nomme():
    """audio_to_wav_buffer : BytesIO positionné à 0 avec .name (pour l'API Groq)."""
    buf = audio.audio_to_wav_buffer(_sinus(duree_s=0.1))
    assert buf.name == "audio.wav"
    assert buf.tell() == 0
    assert buf.read(4) == b"RIFF"


# --------------------------------------------------------------------------- #
#  Segmentation VAD
# --------------------------------------------------------------------------- #

def _frame():
    return np.zeros(audio.FRAME_SAMPLES, dtype=np.float32)


def test_vad_segment_court():
    """Un tour de parole trop court (< MIN_SPEECH_MS) est ignoré (aucun segment)."""
    seg = audio.VADSegmenter()
    f = _frame()
    # Quelques frames de "parole" volontairement insuffisantes.
    courtes = max(1, seg.min_speech_frames - seg.silence_frames_limit - 2)
    emis = []
    for _ in range(courtes):
        emis.append(seg.ajouter(True, f))
    # Silence prolongé → fin de tour, mais le buffer total reste < min_speech_frames.
    for _ in range(seg.silence_frames_limit):
        emis.append(seg.ajouter(False, f))
    assert all(s is None for s in emis)          # rien n'est émis
    assert seg.buffer == []                       # état réinitialisé


def test_vad_segment_valide_emis():
    """Un tour de parole suffisamment long produit bien un segment."""
    seg = audio.VADSegmenter()
    f = _frame()
    out = None
    # Assez de frames de parole pour dépasser le minimum.
    for _ in range(seg.min_speech_frames + 2):
        r = seg.ajouter(True, f)
        out = out or r
    # Puis un silence qui clôt le tour.
    for _ in range(seg.silence_frames_limit):
        r = seg.ajouter(False, f)
        out = out if out is not None else r
    assert out is not None
    assert isinstance(out, np.ndarray)
    assert len(out) >= seg.min_speech_frames * audio.FRAME_SAMPLES


def test_vad_flush_max_seg():
    """Sans silence, le flush forcé se déclenche à MAX_SEG_MS."""
    seg = audio.VADSegmenter()
    f = _frame()
    emis = None
    for _ in range(seg.max_frames + 1):
        r = seg.ajouter(True, f)
        if r is not None:
            emis = r
            break
    assert emis is not None
    assert len(emis) >= seg.max_frames * audio.FRAME_SAMPLES
