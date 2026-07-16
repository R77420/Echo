"""
Vérifie le prompt Whisper : taille sous la limite (unitaire, hors ligne) et,
via le marqueur `groq`, que « Doliprane » ressort d'un appel réel.

Usage manuel : py tests/test_prompt_doliprane.py [chemin_audio.wav]
"""
import io
import os
import struct
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transcription_consultation import WHISPER_INITIAL_PROMPT, GROQ_PROMPT_MAX_BYTES

from conftest import groq_reel


def _taille_prompt():
    return len(WHISPER_INITIAL_PROMPT.encode("utf-8"))


# ── 1. Taille du prompt (unitaire, aucun réseau) ────────────────────────────
def test_prompt_sous_limite():
    nbytes = _taille_prompt()
    assert nbytes <= GROQ_PROMPT_MAX_BYTES, (
        f"PROMPT TROP LONG : {nbytes} > {GROQ_PROMPT_MAX_BYTES}")


def _make_silence_wav(duration_s=1.0, sr=16000):
    buf = io.BytesIO()
    n = int(sr * duration_s)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack(f"<{n}h", *([0] * n)))
    return buf.getvalue()


def _transcrire_silence():
    from GROQ_KEY import GROQ_API_KEY
    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY)
    resp = client.audio.transcriptions.create(
        model="whisper-large-v3",
        file=("silence.wav", io.BytesIO(_make_silence_wav())),
        language="fr",
        prompt=WHISPER_INITIAL_PROMPT,
        response_format="text",
    )
    return resp.strip() if isinstance(resp, str) else resp.text.strip()


# ── 2. Appel réel : le silence ne doit pas halluciner de médicament ─────────
@groq_reel
def test_prompt_silence_pas_hallucination():
    result = _transcrire_silence()
    # Un silence ne doit produire ni « Doliprane » ni bavardage médical.
    assert "Doliprane" not in result


# ── Exécution manuelle avec un vrai fichier audio ───────────────────────────
if __name__ == "__main__":
    nbytes = _taille_prompt()
    print(f"Prompt : {nbytes} octets  (limite {GROQ_PROMPT_MAX_BYTES})")
    assert nbytes <= GROQ_PROMPT_MAX_BYTES, f"PROMPT TROP LONG : {nbytes}"
    print("✓ Taille OK\n")
    try:
        from GROQ_KEY import GROQ_API_KEY  # noqa: F401
        from groq import Groq
    except ImportError as e:
        print(f"Import manquant : {e} → seule la taille a été vérifiée.")
        sys.exit(0)

    audio_path = sys.argv[1] if (len(sys.argv) > 1
                                 and os.path.isfile(sys.argv[1])) else None
    client = Groq(api_key=GROQ_API_KEY)
    if audio_path:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        filename = os.path.basename(audio_path)
    else:
        print("Aucun fichier audio fourni — test avec silence.")
        audio_bytes = _make_silence_wav()
        filename = "silence.wav"

    print(f"Envoi à Groq whisper-large-v3 (prompt={nbytes}o)…")
    resp = client.audio.transcriptions.create(
        model="whisper-large-v3",
        file=(filename, io.BytesIO(audio_bytes)),
        language="fr", prompt=WHISPER_INITIAL_PROMPT, response_format="text",
    )
    result = resp.strip() if isinstance(resp, str) else resp.text.strip()
    print(f"Résultat : «{result}»")
    if audio_path:
        ok = "Doliprane" in result
        print("✓ 'Doliprane' transcrit" if ok else "✗ 'Doliprane' absent")
        sys.exit(0 if ok else 1)
