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


# ---- Extraction structurée (écran de validation à cases à cocher) ----------

CR_CATEGORIES = ["motif", "observations", "traitements", "suivi"]

EXTRACTION_SYSTEM = (
    "Tu extrais les éléments factuels d'une transcription de consultation "
    "médicale française, pour un écran de validation où le médecin coche "
    "chaque élément.\n"
    "Réponds UNIQUEMENT avec un objet JSON valide, aucun texte autour :\n"
    '{"motif": [...], "observations": [...], "traitements": [...], "suivi": [...]}\n'
    "RÈGLES IMPÉRATIVES :\n"
    "- Chaque élément est une puce COURTE et FACTUELLE (une idée par puce)\n"
    "- Ne transforme JAMAIS un propos vague ou familier en terme médical "
    "savant : « j'ai trop faim » reste « dit avoir très faim », PAS "
    "« hyperphagie » ; « je dors mal » reste « dort mal », PAS « insomnie »\n"
    "- Reste au plus près des mots réellement dits\n"
    "- N'invente RIEN : si une catégorie n'a aucun élément dans la "
    "transcription, mets un tableau vide []\n"
    "- motif : la ou les raisons de la consultation\n"
    "- observations : symptômes décrits, constats de l'examen\n"
    "- traitements : médicaments, posologies, prescriptions évoqués\n"
    "- suivi : recommandations, prochains rendez-vous, examens à faire"
)


def _parse_elements_json(brut):
    """Parse la réponse JSON du LLM. Renvoie le dict normalisé ou None."""
    import json as _json
    t = (brut or "").strip()
    # Retirer un éventuel bloc markdown ```json ... ```
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if not m:
        return None
    try:
        d = _json.loads(m.group(0))
    except Exception:
        # Ignorable par design : JSON malformé du LLM → l'appelant retente
        # puis retombe sur elements_vides() (testé : test_json_malforme_failsafe).
        return None
    if not isinstance(d, dict):
        return None
    out = {}
    for cat in CR_CATEGORIES:
        v = d.get(cat, [])
        if not isinstance(v, list):
            v = []
        out[cat] = [str(x).strip() for x in v if str(x).strip()]
    return out


def elements_vides():
    """Structure vide (fail-safe : le médecin remplit à la main)."""
    return {cat: [] for cat in CR_CATEGORIES}


def extraire_elements_cr(transcript, api_key):
    """Extrait les éléments structurés du compte-rendu (JSON par catégorie).
    Remplace la génération de résumé en prose : l'IA propose, le médecin coche.
    JSON malformé → un retry, puis structure vide. Jamais d'exception."""
    if not transcript or not transcript.strip() or not api_key:
        return elements_vides()
    GROQ_BASE_URL = "https://api.groq.com/openai/v1"
    try:
        import openai
        client = openai.OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
    except Exception:
        # Sans client, AUCUN compte-rendu ne sera extrait : à tracer.
        from journal_erreurs import journaliser
        journaliser("extraire_elements_cr: client Groq impossible")
        return elements_vides()
    messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM},
        {"role": "user",
         "content": "Transcription de la consultation :\n\n" + transcript
                    + "\n\nExtrait les éléments au format JSON imposé."},
    ]
    for tentative in range(2):          # 1 essai + 1 retry si JSON malformé
        try:
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0,
                max_tokens=700,
                response_format={"type": "json_object"},
            )
            d = _parse_elements_json(response.choices[0].message.content)
            if d is not None:
                return d
        except Exception as exc:
            logging.warning("extraire_elements_cr (essai %d): %s", tentative + 1, exc)
    return elements_vides()


def elements_vers_resume(elements):
    """Convertit les éléments VALIDÉS en texte résumé au format standard
    (RESUME_TITRES + puces) — réutilisé tel quel par storage.ecrire_docx."""
    libelles = dict(zip(CR_CATEGORIES, RESUME_TITRES))
    lignes = [ENTETE_RESUME, ""]
    for cat in CR_CATEGORIES:
        lignes.append(libelles[cat])
        items = (elements or {}).get(cat) or []
        if items:
            lignes.extend("- " + i for i in items)
        else:
            lignes.append("Non précisé")
        lignes.append("")
    return "\n".join(lignes).strip()
