# -*- coding: utf-8 -*-
"""
correction.py — Correction LLM du transcript médical (Groq Llama 3.3 70B).

Corrige les erreurs de transcription Whisper sur les termes médicaux
(ex : « Doliphrène » → « Doliprane ») sans jamais reformuler le propos.

Deux modes :
  - corriger_segment()             : temps réel, segment par segment (timeout court)
  - corriger_transcript_complet()  : passe globale avant génération du résumé

Fail-safe systématique : toute erreur (API, timeout, réponse aberrante)
renvoie le texte original inchangé.
"""

import logging

try:
    from GROQ_KEY import GROQ_API_KEY
except Exception:
    GROQ_API_KEY = ""

GROQ_BASE_URL   = "https://api.groq.com/openai/v1"
CORRECTION_MODEL = "llama-3.3-70b-versatile"
TIMEOUT_SEGMENT  = 3.0   # secondes — au-delà, le texte brut reste affiché
TIMEOUT_GLOBAL   = 20.0

SYSTEM_PROMPT = (
    "Tu corriges des segments de transcription de consultation "
    "médicale française. Corrige UNIQUEMENT :\n"
    "- Noms de médicaments mal transcrits (ex: Doliphrène → Doliprane, "
    "d'olivrane → Doliprane, amoxiciline → Amoxicilline)\n"
    "- Termes médicaux mal orthographiés\n"
    "- Posologies incohérentes (ex: '1 gramme 3 fois par jours' → "
    "'1 g 3 fois par jour')\n"
    "RÈGLES STRICTES :\n"
    "- Ne JAMAIS reformuler ni résumer\n"
    "- Ne JAMAIS ajouter ou supprimer d'information\n"
    "- Ne JAMAIS corriger le style oral (garder les 'euh', hésitations)\n"
    "- Si aucune erreur médicale → retourner le texte IDENTIQUE\n"
    "- Retourner UNIQUEMENT le texte corrigé, aucun commentaire"
)

SYSTEM_PROMPT_GLOBAL = SYSTEM_PROMPT + (
    "\n- Le texte est un transcript multi-lignes au format "
    "'[HH:MM:SS] Locuteur : texte'. Conserver EXACTEMENT ce format, "
    "les horodatages et le nombre de lignes ; corriger seulement les "
    "termes médicaux dans les textes."
)


def _client(timeout):
    if not GROQ_API_KEY:
        return None
    try:
        import openai
        return openai.OpenAI(api_key=GROQ_API_KEY,
                             base_url=GROQ_BASE_URL,
                             timeout=timeout)
    except Exception:
        return None


def corriger_segment(texte, contexte=""):
    """Correction rapide d'un segment en temps réel.
    Renvoie le texte corrigé, ou le texte original en cas d'échec/timeout."""
    if not texte or not texte.strip():
        return texte
    client = _client(TIMEOUT_SEGMENT)
    if client is None:
        return texte
    user = texte
    if contexte:
        user = "Contexte (segments précédents, ne pas corriger) :\n" \
               + contexte + "\n\nSegment à corriger :\n" + texte
    try:
        resp = client.chat.completions.create(
            model=CORRECTION_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            temperature=0,
            max_tokens=200,
        )
        corrige = (resp.choices[0].message.content or "").strip()
        # Garde-fous : réponse vide ou aberrante (longueur trop éloignée
        # de l'original = le modèle a reformulé/commenté) → original.
        if not corrige:
            return texte
        if len(corrige) > len(texte) * 2 + 40:
            return texte
        return corrige
    except Exception as exc:
        logging.debug("corriger_segment: %s", exc)
        return texte


def corriger_transcript_complet(entries):
    """Correction globale du transcript avant génération du résumé.

    `entries` : liste de (horodatage, locuteur, texte).
    Renvoie une liste de même structure avec les textes corrigés.
    Fail-safe : toute anomalie (erreur API, nombre de lignes différent…)
    renvoie les entries originales."""
    if not entries:
        return entries
    client = _client(TIMEOUT_GLOBAL)
    if client is None:
        return entries
    lignes = ["[%s] %s : %s" % (h, loc, t) for h, loc, t in entries]
    try:
        resp = client.chat.completions.create(
            model=CORRECTION_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_GLOBAL},
                {"role": "user", "content": "\n".join(lignes)},
            ],
            temperature=0,
            max_tokens=4096,
        )
        sortie = (resp.choices[0].message.content or "").strip().splitlines()
        sortie = [l for l in sortie if l.strip()]
        if len(sortie) != len(entries):
            return entries          # structure altérée → on ne touche à rien
        corrigees = []
        for (h, loc, t), ligne in zip(entries, sortie):
            prefixe = "[%s] %s : " % (h, loc)
            if ligne.startswith(prefixe):
                corrigees.append((h, loc, ligne[len(prefixe):]))
            else:
                # Ligne au format inattendu → garder l'originale.
                corrigees.append((h, loc, t))
        return corrigees
    except Exception as exc:
        logging.debug("corriger_transcript_complet: %s", exc)
        return entries
