"""
Test réel Groq : vérifie que "Doliprane" est bien transcrit avec le nouveau prompt.
Usage : py tests/test_prompt_doliprane.py
Requiert GROQ_KEY.py présent (clé API Groq).
"""
import sys, os, io, wave, struct, math

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from transcription_consultation import WHISPER_INITIAL_PROMPT, GROQ_PROMPT_MAX_BYTES

# ── 1. Vérification taille prompt ───────────────────────────────────────────
nbytes = len(WHISPER_INITIAL_PROMPT.encode("utf-8"))
print(f"Prompt : {nbytes} octets  (limite {GROQ_PROMPT_MAX_BYTES})")
assert nbytes <= GROQ_PROMPT_MAX_BYTES, f"PROMPT TROP LONG : {nbytes} > {GROQ_PROMPT_MAX_BYTES}"
print("✓ Taille OK\n")

# ── 2. Génération d'un WAV synthétique avec silence (fallback si pas de vrai audio)
def make_silence_wav(duration_s=1.0, sr=16000):
    buf = io.BytesIO()
    n = int(sr * duration_s)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack(f"<{n}h", *([0] * n)))
    return buf.getvalue()

# ── 3. Test Groq réel sur fichier audio fourni en argument ──────────────────
try:
    from GROQ_KEY import GROQ_API_KEY
    from groq import Groq
except ImportError as e:
    print(f"Import manquant : {e}")
    print("→ Seule la vérification de taille a été effectuée.")
    sys.exit(0)

client = Groq(api_key=GROQ_API_KEY)

audio_path = sys.argv[1] if (__name__ == "__main__" and len(sys.argv) > 1
                             and os.path.isfile(sys.argv[1])) else None

if audio_path and os.path.exists(audio_path):
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()
    filename = os.path.basename(audio_path)
else:
    print("Aucun fichier audio fourni — test avec silence (transcription vide attendue).")
    audio_bytes = make_silence_wav()
    filename = "silence.wav"

print(f"Envoi à Groq whisper-large-v3 (prompt={nbytes}o)…")
resp = client.audio.transcriptions.create(
    model="whisper-large-v3",
    file=(filename, io.BytesIO(audio_bytes)),
    language="fr",
    prompt=WHISPER_INITIAL_PROMPT,
    response_format="text",
)
result = resp.strip() if isinstance(resp, str) else resp.text.strip()
print(f"Résultat : «{result}»")

if audio_path:
    ok = "Doliprane" in result
    print("✓ 'Doliprane' correctement transcrit" if ok else "✗ 'Doliprane' absent de la transcription")
    sys.exit(0 if ok else 1)
