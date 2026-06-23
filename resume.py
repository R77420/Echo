"""Résumé structuré de consultation médicale.

Moteur unique : Groq LLM (Llama 70B) — résumé en ~3 s. Si Groq est
indisponible (réseau, quota…), aucun résumé n'est généré : le compte-rendu
reste sauvegardé avec la transcription complète (pas de blocage).

Le résumé suit un format figé : Motif / Observations / Traitements / Suivi.
"""

import logging
import re

# ---- Modèle Groq ------------------------------------------------------------
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TEMPERATURE = 0.1
GROQ_MAX_TOKENS  = 450

# ---- Constantes partagées --------------------------------------------------

ENTETE_RESUME = "RÉSUMÉ (généré automatiquement — à relire et corriger)"

RESUME_TITRES = [
    "Motif :",
    "Observations / points clés :",
    "Traitements et prescriptions évoqués :",
    "Suivi et recommandations :",
]

RESUME_POLITESSE = re.compile(
    r"bonne (journée|soirée|saison|continuation|route)|"
    r"prendre soin|prenez soin|au revoir|à bient[oô]t|portez-vous bien|"
    r"bonne fin de", re.IGNORECASE)

RESUME_SYSTEM = (
    "Tu es un assistant médical. Tu rédiges le compte-rendu d'une consultation "
    "à partir de sa transcription.\n\n"
    "RÈGLES IMPÉRATIVES :\n"
    "- Utilise UNIQUEMENT les informations présentes dans la transcription. "
    "N'invente aucun diagnostic, médicament, posologie ni chiffre non énoncé.\n"
    "- Rédige de façon impersonnelle, à la troisième personne. Ne t'adresse JAMAIS "
    "au patient (n'emploie jamais « vous »).\n"
    "- Respecte les négations : un symptôme nié par le patient ne doit JAMAIS "
    "apparaître comme présent ; écris-le sous forme négative claire "
    "(ex. « Pas de toux, pas de fièvre »).\n"
    "- Chaque section est une liste de puces courtes commençant par « - », même "
    "s'il n'y a qu'un seul élément.\n"
    "- Ignore les salutations et formules de politesse (bonjour, au revoir, merci, "
    "bonne journée, prenez soin de vous, etc.) : elles n'apparaissent pas dans le "
    "compte-rendu.\n"
    "- Si, et seulement si, une section ne contient réellement aucune information "
    "dans la transcription, écris sur une seule ligne, sans puce : Non précisé. "
    "Ne déduis rien, n'extrapole aucune recommandation non formulée.\n"
    "- Aucune phrase d'introduction ni de conclusion, aucun autre titre que les "
    "quatre imposés.\n\n"
    "CONTENU DE CHAQUE SECTION :\n"
    "- Motif : la raison de la consultation.\n"
    "- Observations / points clés : symptômes et plaintes décrits par le patient "
    "ET résultats de l'examen clinique (auscultation, tension, poids, etc.).\n"
    "- Traitements et prescriptions évoqués : médicaments, examens prescrits, "
    "orientations vers un spécialiste.\n"
    "- Suivi et recommandations : prochain rendez-vous, consignes de surveillance, "
    "conduite à tenir en cas d'aggravation.\n\n"
    "FORMAT — réponds EXACTEMENT avec ces quatre titres, dans cet ordre, suivis "
    "de deux points :\n\n"
    "Motif :\n"
    "Observations / points clés :\n"
    "Traitements et prescriptions évoqués :\n"
    "Suivi et recommandations :"
)

RESUME_ONESHOT1_USER = (
    "Transcription de la consultation :\n\n"
    "[09:00:00] Médecin : Bonjour, qu'est-ce qui vous amène ?\n"
    "[09:00:05] Patient : J'ai mal à la gorge depuis deux jours, avec un peu de fièvre.\n"
    "[09:00:12] Médecin : Pas de toux, pas de gêne pour respirer ?\n"
    "[09:00:16] Patient : Non, pas de toux, je respire bien.\n"
    "[09:00:22] Médecin : La gorge est rouge, les ganglions du cou sont un peu gonflés. "
    "Température 38,2.\n"
    "[09:00:40] Médecin : C'est une angine probablement virale. Paracétamol 1 gramme "
    "si douleur ou fièvre, trois fois par jour maximum.\n"
    "[09:00:55] Médecin : Reposez-vous, buvez beaucoup. Si la fièvre dépasse trois "
    "jours, revenez consulter.\n\n"
    "Rédige le compte-rendu selon le format imposé."
)
RESUME_ONESHOT1_ASSISTANT = (
    "Motif :\n"
    "- Mal de gorge depuis deux jours avec fièvre.\n\n"
    "Observations / points clés :\n"
    "- Gorge rouge, ganglions cervicaux légèrement gonflés.\n"
    "- Température à 38,2 °C.\n"
    "- Pas de toux, pas de gêne respiratoire.\n\n"
    "Traitements et prescriptions évoqués :\n"
    "- Paracétamol 1 g si douleur ou fièvre, trois fois par jour maximum.\n\n"
    "Suivi et recommandations :\n"
    "- Repos et hydratation abondante.\n"
    "- Reconsulter si la fièvre dépasse trois jours."
)
RESUME_ONESHOT2_USER = (
    "Transcription de la consultation :\n\n"
    "[10:00:00] Médecin : Bonjour, je vous écoute.\n"
    "[10:00:04] Patient : Je voulais juste savoir si je peux prendre du paracétamol "
    "avec mon traitement habituel.\n"
    "[10:00:10] Médecin : Oui, c'est compatible, aucun problème.\n"
    "[10:00:15] Patient : Parfait, merci, c'était seulement ça.\n"
    "[10:00:18] Médecin : Très bien, bonne journée.\n\n"
    "Rédige le compte-rendu selon le format imposé."
)
RESUME_ONESHOT2_ASSISTANT = (
    "Motif :\n"
    "- Question sur la compatibilité du paracétamol avec le traitement habituel.\n\n"
    "Observations / points clés :\n"
    "Non précisé\n\n"
    "Traitements et prescriptions évoqués :\n"
    "Non précisé\n\n"
    "Suivi et recommandations :\n"
    "Non précisé"
)

# ---- Normalisation ---------------------------------------------------------

def _normaliser_resume(corps):
    for t in RESUME_TITRES:
        corps = corps.replace(t, "\n" + t + "\n")
    lignes = []
    for l in corps.splitlines():
        l = l.rstrip()
        if l.lstrip().startswith("-") and RESUME_POLITESSE.search(l):
            continue
        if l == "" and (not lignes or lignes[-1] == ""):
            continue
        lignes.append(l)
    return "\n".join(lignes).strip()


# ---- Messages pour les deux moteurs ----------------------------------------
def _messages(transcript):
    return [
        {"role": "system", "content": RESUME_SYSTEM},
        {"role": "user", "content": RESUME_ONESHOT1_USER},
        {"role": "assistant", "content": RESUME_ONESHOT1_ASSISTANT},
        {"role": "user", "content": RESUME_ONESHOT2_USER},
        {"role": "assistant", "content": RESUME_ONESHOT2_ASSISTANT},
        {"role": "user",
         "content": "Transcription de la consultation :\n\n" + transcript
                    + "\n\nRédige le compte-rendu selon le format imposé."},
    ]


# ---- Moteur Groq (primaire) ------------------------------------------------

def groq_summarize(transcript, api_key):
    """Résumé via Groq LLM. Renvoie le texte formaté ou None si échec."""
    GROQ_BASE_URL = "https://api.groq.com/openai/v1"
    try:
        import openai
        client = openai.OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=_messages(transcript),
            temperature=GROQ_TEMPERATURE,
            max_tokens=GROQ_MAX_TOKENS,
        )
        corps = _normaliser_resume(
            response.choices[0].message.content.strip())
        return ENTETE_RESUME + "\n\n" + corps
    except Exception as exc:
        logging.warning("groq_summarize: %s", exc)
        return None
