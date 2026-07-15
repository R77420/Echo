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
import re

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

SYSTEM_PROMPT_LOCUTEURS = (
    "Transcript d'une consultation médicale en cabinet. Chaque ligne a le "
    "format exact : [HH:MM:SS] Conversation : texte\n"
    "Remplace le mot « Conversation » de chaque ligne par MEDECIN ou PATIENT "
    "selon le contenu.\n"
    "Indices : le médecin questionne, examine, prescrit, explique ; le patient "
    "décrit ses symptômes, répond, pose des questions sur son traitement.\n"
    "SCISSION : si une ligne contient MANIFESTEMENT deux locuteurs (ex. une "
    "question puis une réponse : « Ça va très bien et vous ? Ça va très bien. »), "
    "scinde-la en DEUX lignes avec le MÊME horodatage, en coupant le texte au "
    "bon endroit — sans rien ajouter ni retirer, la concaténation doit rester "
    "identique.\n"
    "RÈGLES STRICTES :\n"
    "- Réponds UNIQUEMENT avec les lignes, une par ligne\n"
    "- Format de sortie EXACT : [HH:MM:SS] MEDECIN : texte  (ou PATIENT)\n"
    "- Remplace l'étiquette EN PLACE ; n'ajoute JAMAIS de flèche, de commentaire "
    "ni de suffixe (pas de « -> PATIENT »)\n"
    "- Ne modifie AUCUN mot ni horodatage : uniquement l'étiquette et, si besoin, "
    "la coupure entre deux locuteurs\n"
    "- Si tu ne peux pas trancher pour une ligne, garde l'étiquette Conversation\n"
    "- Si une réplique est manifestement hors-sujet par rapport à une "
    "consultation médicale (publicité, phrase générique sans lien avec la "
    "santé, contenu incohérent avec le reste de l'échange), remplace son "
    "étiquette par HORS-SUJET au lieu de MEDECIN/PATIENT\n"
    "Exemple. Entrée :\n"
    "[09:00:01] Conversation : j'ai mal à la gorge\n"
    "[09:00:09] Conversation : je vous examine. Ça pique quand vous avalez ?\n"
    "Sortie :\n"
    "[09:00:01] PATIENT : j'ai mal à la gorge\n"
    "[09:00:09] MEDECIN : je vous examine. Ça pique quand vous avalez ?"
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


# Mots anglais courants : une hallucination Whisper bascule souvent en anglais
# sur du bruit/silence (« You've seen the victory of… »). ≥ 2 → texte suspect.
MOTS_ANGLAIS = {
    "the", "you", "and", "of", "have", "is", "are", "with", "seen",
    "this", "that", "for", "your", "victory",
}


def contient_bascule_anglaise(texte):
    """Vrai si le texte contient ≥ 2 mots anglais courants (hallucination
    Whisper qui bascule en anglais alors qu'on transcrit du français)."""
    mots = set(re.findall(r"[a-z]+", (texte or "").lower()))
    return len(mots & MOTS_ANGLAIS) >= 2


def _mapper_locuteur(brut):
    """Mappe l'étiquette renvoyée par le LLM vers le libellé interne.
    « MÉDECIN/Médecin/medecin… » → "Medecin", « PATIENT… » → "Patient".
    « HORS-SUJET » (hallucination cohérente détectée par incohérence
    thématique) et tout libellé non reconnu → None : la ligne reste
    « Conversation », donc marquée « [?] » dans l'annexe et EXCLUE du
    résumé (est_ligne_douteuse)."""
    n = "".join(c for c in brut.lower() if c.isalpha())
    # sans accents
    n = (n.replace("é", "e").replace("è", "e").replace("ê", "e"))
    if n.startswith("medecin") or n == "docteur" or n.startswith("dr"):
        return "Medecin"
    if n.startswith("patient"):
        return "Patient"
    return None


def _sans_espaces(s):
    return re.sub(r"\s+", "", s or "")


def est_ligne_douteuse(loc, texte):
    """Vrai si la ligne ne doit PAS alimenter le résumé : non classée
    (« Conversation ») ou marquée « [?] » (passage douteux). Ces lignes
    restent dans le transcript annexe mais jamais dans le compte-rendu —
    une information médicale non fiable ne doit jamais être résumée."""
    return loc == "Conversation" or (texte or "").lstrip().startswith("[?]")


# Lexique français courant (mots > 4 lettres). Sert à repérer un charabia :
# une ligne dont AUCUN mot long n'est reconnu est probablement une hallucination
# Whisper (« préimbant l'animationnel »). Volontairement restreint aux mots très
# fréquents + vocabulaire de consultation — on ne l'utilise QUE sur les lignes
# résiduelles « Conversation » (déjà non attribuées) pour ne jamais rejeter un
# terme médical d'une réplique attribuée.
_LEXIQUE_FR = {
    # liaisons / pronoms / déterminants longs
    "votre", "notre", "leurs", "elles", "nous", "vous", "cette", "cela",
    "comme", "aussi", "alors", "quand", "parce", "pour", "avec", "sans",
    "dans", "chez", "mais", "donc", "puis", "très", "trop", "plus", "moins",
    "bien", "encore", "toujours", "jamais", "aujourd", "hier", "demain",
    "peut", "faut", "veux", "veut", "dois", "doit", "pouvez", "devez",
    "quel", "quelle", "quels", "quelles", "lequel", "laquelle", "autre",
    "autres", "chaque", "plusieurs", "certains", "certaines", "même",
    "mêmes", "tout", "toute", "toutes", "quelqu", "personne", "rien",
    # verbes fréquents (formes courantes)
    "avez", "avons", "êtes", "sont", "était", "étaient", "avait", "avaient",
    "aller", "allez", "allons", "faire", "faites", "faisons", "prendre",
    "prenez", "prends", "donner", "donnez", "donne", "voir", "vois", "voit",
    "voyez", "savoir", "sais", "sait", "savez", "penser", "pense", "pensez",
    "trouve", "trouver", "trouvez", "sentir", "sens", "sent", "sentez",
    "ressens", "ressent", "revoir", "revenir", "revenez", "reviens",
    "rester", "reste", "restez", "continuer", "continue", "continuez",
    "arrêter", "arrête", "arrêtez", "commencer", "commence", "commencez",
    "regarder", "regarde", "regardez", "respirez", "respirer", "respire",
    "avaler", "avale", "avalez", "examine", "examiner", "examinez",
    "prescris", "prescrit", "prescrire", "expliquer", "explique",
    "expliquez", "parler", "parle", "parlez", "dire", "dites", "disons",
    "vouloir", "pouvoir", "devoir", "falloir", "mettre", "mettez", "mets",
    "passer", "passe", "passez", "passage", "classer", "classe", "porter",
    "porte", "arriver", "arrive", "arrivez", "changer", "change", "montrer",
    "montre", "montrez", "essayer", "essaye", "essayez", "aider", "aide",
    "aidez", "appeler", "appelle", "appelez", "attendre", "attendez",
    "attend", "entendre", "entends", "entend", "comprendre", "comprends",
    "comprend", "boire", "buvez", "manger", "mange", "mangez", "dormir",
    "dors", "dort", "dormez", "marcher", "marche", "marchez", "tenir",
    "tiens", "tient", "tenez", "vivre", "vivez", "devenir", "devient",
    # salutations / politesse
    "bonjour", "bonsoir", "revoir", "merci", "madame", "monsieur",
    "mademoiselle", "docteur", "excusez", "voilà", "accord", "comment",
    "pourquoi", "sûr", "évidemment", "effectivement", "certainement",
    "peut", "être", "vraiment", "surtout", "plutôt", "assez", "environ",
    # corps / symptômes / consultation
    "douleur", "douleurs", "gorge", "tête", "ventre", "poitrine",
    "jambe", "jambes", "bras", "bouche", "oreille", "oreilles", "estomac",
    "coeur", "poumon", "poumons", "muscle", "muscles", "articulation",
    "fièvre", "toux", "fatigue", "nausée", "nausées", "vertige", "vertiges",
    "migraine", "rhume", "angine", "grippe", "allergie", "tension",
    "essoufflement", "vomissement", "vomissements", "diarrhée", "constipation",
    "insomnie", "stress", "anxiété", "dépression", "infection", "inflammation",
    "matin", "soir", "midi", "nuit", "jour", "jours", "semaine", "semaines",
    "mois", "année", "années", "heure", "heures", "minute", "minutes",
    "gramme", "grammes", "milligramme", "comprimé", "comprimés", "gélule",
    "gélules", "sirop", "goutte", "gouttes", "dose", "doses",
    "ordonnance", "prescription", "traitement", "traitements", "médicament",
    "médicaments", "antibiotique", "antibiotiques", "vaccin", "analyse",
    "analyses", "examen", "examens", "radio", "scanner", "prise", "sang",
    "patient", "patiente", "médecin", "consultation", "rendez", "hôpital",
    "clinique", "cabinet", "urgences", "spécialiste", "généraliste",
    # temps / quantités / adverbes fréquents
    "pendant", "depuis", "avant", "après", "beaucoup", "petit", "petite",
    "grande", "grand", "mieux", "quelque", "quelques", "chose", "choses",
    "besoin", "problème", "problèmes", "question", "questions", "réponse",
    "gauche", "droite", "dernier", "dernière", "prochain", "prochaine",
    "normal", "normale", "possible", "difficile", "facile", "important",
    "importante", "sérieux", "grave", "léger", "légère", "fort", "forte",
    "moment", "moments", "endroit", "côté", "niveau", "genre", "façon",
    "manière", "raison", "exemple", "situation", "travail", "maison",
    "famille", "enfant", "enfants", "personnes", "gens", "monde",
}


def _est_charabia(texte):
    """Vrai si le texte est un charabia PROBABLE : au moins 3 mots de plus de
    4 lettres et AUCUN reconnu dans le lexique français courant. Volontairement
    strict (≥ 3 mots, 0 reconnu) — un seul mot français plausible ou moins de
    3 mots longs suffit à conserver la ligne, pour ne pas supprimer du vrai
    français à cause d'un lexique forcément incomplet."""
    mots = re.findall(r"[a-zàâäéèêëïîôöùûüç]+", (texte or "").lower())
    longs = [m for m in mots if len(m) > 4]
    if len(longs) < 3:
        return False
    connus = sum(1 for m in longs if m in _LEXIQUE_FR)
    return connus == 0


def _nettoyer_conversation_residuelle(entries):
    """Règle (b) : après attribution, une ligne restée « Conversation » (que le
    LLM n'a pas su trancher) est suspecte. Supprimée si bascule anglaise OU
    charabia (aucun mot français reconnu). Sinon → conservée mais préfixée
    « [?] » pour signaler au médecin un passage douteux."""
    final = []
    for h, loc, t in entries:
        if loc == "Conversation":
            if contient_bascule_anglaise(t) or _est_charabia(t):
                continue                       # charabia → supprimé
            if not t.lstrip().startswith("[?]"):
                t = "[?] " + t                 # douteux → marqué
        final.append((h, loc, t))
    return final


def attribuer_locuteurs(entries):
    """Mode cabinet : attribue Médecin/Patient à chaque tour par analyse du
    contenu via Groq Llama 3.3 70B, et scinde les tours où deux locuteurs sont
    manifestement fusionnés.

    `entries` : liste de (horodatage, locuteur, texte) — étiquetées
    « Conversation » à l'entrée. Renvoie la même structure avec le locuteur
    remplacé par "Medecin"/"Patient" (ou "Conversation" marqué « [?] » si
    indécidable). Le nombre de lignes peut AUGMENTER (scission) mais jamais
    diminuer, et la concaténation des textes reste identique (aucun mot perdu
    ni ajouté).

    Fail-safe : erreur API, lignes en moins, format inattendu ou concaténation
    altérée → entries d'origine inchangées (étiquette « Conversation »)."""
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
                {"role": "system", "content": SYSTEM_PROMPT_LOCUTEURS},
                {"role": "user", "content": "\n".join(lignes)},
            ],
            temperature=0,
            max_tokens=4096,
        )
        sortie = (resp.choices[0].message.content or "").strip().splitlines()
        sortie = [l for l in sortie if l.strip()]
        # Le LLM peut scinder → plus de lignes. Jamais moins (= perte de contenu).
        if len(sortie) < len(entries):
            return entries
        parsed = []
        for ligne in sortie:
            m = re.match(r"^\s*\[([^\]]*)\]\s*(.+?)\s*:\s*(.*)$", ligne)
            if not m:
                return entries          # format inattendu → fail-safe
            parsed.append((m.group(1), m.group(2), m.group(3)))
        # Garde-fou anti-modification : la concaténation des textes (hors espaces)
        # doit être identique — sinon le LLM a altéré le contenu → fail-safe.
        if _sans_espaces("".join(t for _, _, t in parsed)) != \
           _sans_espaces("".join(t for _, _, t in entries)):
            return entries
        resultat = []
        for h, label_brut, texte in parsed:
            nouveau = _mapper_locuteur(label_brut)
            # Repli : certains modèles suffixent (« … -> PATIENT ») au lieu de
            # remplacer l'étiquette en place.
            if nouveau is None:
                m2 = re.search(r"(?:->|=>)\s*(m[ée]decin|patient)\s*$",
                               texte, re.IGNORECASE)
                if m2:
                    nouveau = _mapper_locuteur(m2.group(1))
            resultat.append((h, nouveau or "Conversation", texte))
        return _nettoyer_conversation_residuelle(resultat)
    except Exception as exc:
        logging.debug("attribuer_locuteurs: %s", exc)
        return entries


# ─────────────────────────── Détection du nom du patient ────────────────────

SYSTEM_PROMPT_NOM = (
    "Transcript d'une consultation médicale. Identifie le NOM DE FAMILLE du "
    "patient s'il est explicitement prononcé (souvent dans les salutations : "
    "« Bonjour madame X », « Monsieur Y, asseyez-vous »).\n"
    "Réponds UNIQUEMENT en JSON :\n"
    '{"nom": "DUBOIS", "prenom": "Marie", "confiance": "haute"}\n'
    "- prenom : uniquement s'il est aussi prononcé, sinon \"\"\n"
    "- confiance : \"haute\" si le nom est clairement adressé au patient, "
    "\"faible\" si ambigu (peut être un tiers, un confrère, un membre de la "
    "famille, un médecin cité)\n"
    '- Si AUCUN nom de patient n\'est prononcé : {"nom": "", "prenom": "", '
    '"confiance": "faible"}\n'
    "- Ne JAMAIS inventer un nom. Dans le doute : confiance faible."
)


def detecter_nom_patient(entries):
    """Extrait le nom du patient mentionné dans la conversation (salutations).

    Renvoie {"nom": "DUBOIS", "prenom": "Marie", "confiance": "haute|faible"}
    ou None (erreur API / JSON invalide / aucune entrée).

    RÈGLE ABSOLUE : c'est une SUGGESTION affichée au médecin, jamais un
    remplissage automatique silencieux — une erreur de nom dans un dossier
    patient est grave. Le médecin valide toujours."""
    import json as _json
    if not entries:
        return None
    client = _client(TIMEOUT_GLOBAL)
    if client is None:
        return None
    # Le nom est presque toujours en début de consultation : 10 premières
    # lignes suffisent (et limitent le coût).
    lignes = ["[%s] %s : %s" % (h, loc, t) for h, loc, t in entries[:10]]
    try:
        resp = client.chat.completions.create(
            model=CORRECTION_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_NOM},
                {"role": "user", "content": "\n".join(lignes)},
            ],
            temperature=0,
            max_tokens=120,
            response_format={"type": "json_object"},
        )
        brut = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", brut, re.DOTALL)
        if not m:
            return None
        d = _json.loads(m.group(0))
        if not isinstance(d, dict):
            return None
        nom = str(d.get("nom") or "").strip()
        return {
            "nom": nom.upper(),
            "prenom": str(d.get("prenom") or "").strip(),
            "confiance": "haute" if d.get("confiance") == "haute" and nom else "faible",
        }
    except Exception as exc:
        logging.debug("detecter_nom_patient: %s", exc)
        return None
