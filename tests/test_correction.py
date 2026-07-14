# -*- coding: utf-8 -*-
"""
Tests du module correction.py (correction LLM du transcript médical).

Les tests marqués « Groq réel » nécessitent GROQ_KEY.py et le réseau.
Usage : .venv/Scripts/python.exe -m pytest tests/test_correction.py -v
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import correction


GROQ_DISPONIBLE = bool(correction.GROQ_API_KEY)


# ---------------------------------------------------------------- fail-safe

def test_fail_safe_texte_vide():
    assert correction.corriger_segment("") == ""
    assert correction.corriger_segment("   ") == "   "


def test_fail_safe_erreur_api(monkeypatch):
    """Erreur API simulée → le texte original est retourné tel quel."""
    def _client_casse(timeout):
        class Casse:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        raise RuntimeError("API down")
        return Casse()
    monkeypatch.setattr(correction, "_client", _client_casse)
    texte = "Je vous prescris du Doliphrène"
    assert correction.corriger_segment(texte) == texte


def test_fail_safe_transcript_erreur_api(monkeypatch):
    monkeypatch.setattr(correction, "_client", lambda t: None)
    entries = [("10:00:00", "Medecin", "du Doliphrène matin et soir")]
    assert correction.corriger_transcript_complet(entries) == entries


def test_fail_safe_transcript_vide():
    assert correction.corriger_transcript_complet([]) == []


# ------------------------------------------------------------- Groq réel

import pytest

pytestmark_reel = pytest.mark.skipif(
    not GROQ_DISPONIBLE, reason="GROQ_KEY.py absent — tests réels ignorés")


@pytest.mark.skipif(not GROQ_DISPONIBLE, reason="clé Groq absente")
def test_correction_medicament():
    """'Doliphrène' doit être corrigé en 'Doliprane' (appel Groq réel)."""
    t0 = time.time()
    res = correction.corriger_segment("Je vous prescris du Doliphrène")
    latence = time.time() - t0
    print(f"\n  latence : {latence:.2f}s — résultat : «{res}»")
    assert "Doliprane" in res
    assert latence < 5.0


@pytest.mark.skipif(not GROQ_DISPONIBLE, reason="clé Groq absente")
def test_pas_de_correction_inutile():
    """Texte médicalement correct → retourné identique (ou quasi)."""
    texte = "Je vous prescris du Doliprane 1 g 3 fois par jour"
    res = correction.corriger_segment(texte)
    print(f"\n  résultat : «{res}»")
    assert "Doliprane" in res
    assert "1 g" in res or "1g" in res


@pytest.mark.skipif(not GROQ_DISPONIBLE, reason="clé Groq absente")
def test_pas_de_reformulation():
    """Les hésitations du style oral doivent être conservées."""
    texte = "euh, je pense que... enfin, vous devriez prendre du Doliphrène"
    res = correction.corriger_segment(texte)
    print(f"\n  résultat : «{res}»")
    assert "euh" in res.lower()
    assert "Doliprane" in res


@pytest.mark.skipif(not GROQ_DISPONIBLE, reason="clé Groq absente")
def test_transcript_complet():
    """Correction globale : structure préservée, médicament corrigé."""
    entries = [
        ("10:00:05", "Medecin", "Bonjour, qu'est-ce qui vous amène ?"),
        ("10:00:12", "Patient", "J'ai mal à la tête depuis trois jours"),
        ("10:00:30", "Medecin", "Je vous prescris du Doliphrène un gramme"),
    ]
    res = correction.corriger_transcript_complet(entries)
    assert len(res) == 3
    # Horodatages et locuteurs intacts.
    for (h0, l0, _), (h1, l1, _) in zip(entries, res):
        assert h0 == h1 and l0 == l1
    print(f"\n  ligne corrigée : «{res[2][2]}»")
    assert "Doliprane" in res[2][2]


# ---------------------------------------------------------------- locuteurs

def test_attribuer_locuteurs_failsafe(monkeypatch):
    """Erreur API simulée → étiquettes « Conversation » conservées,
    aucun texte modifié."""
    def _client_casse(timeout):
        class Casse:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        raise RuntimeError("API down")
        return Casse()
    monkeypatch.setattr(correction, "_client", _client_casse)
    entries = [
        ("10:00:00", "Conversation", "Bonjour docteur, j'ai mal à la gorge"),
        ("10:00:08", "Conversation", "Je vous prescris du Doliprane"),
    ]
    res = correction.attribuer_locuteurs(entries)
    assert res == entries          # inchangé (labels + textes)


def test_attribuer_locuteurs_lignes_alterees(monkeypatch):
    """Nombre de lignes renvoyé différent → entries d'origine."""
    def _client_mock(timeout):
        class Mock:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        class R:
                            class choices:
                                pass
                        r = R()
                        msg = type("M", (), {"content": "[10:00:00] MÉDECIN : une seule ligne"})
                        r.choices = [type("C", (), {"message": msg})]
                        return r
        return Mock()
    monkeypatch.setattr(correction, "_client", _client_mock)
    entries = [
        ("10:00:00", "Conversation", "a"),
        ("10:00:08", "Conversation", "b"),
    ]
    assert correction.attribuer_locuteurs(entries) == entries


def test_attribuer_locuteurs_mapping(monkeypatch):
    """Le mapping des étiquettes fonctionne quand le LLM ne relabelle que
    l'étiquette (texte identique)."""
    def _client_mock(timeout):
        class Mock:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        contenu = ("[10:00:00] PATIENT : j'ai mal à la gorge\n"
                                   "[10:00:08] MÉDECIN : je vous prescris du Doliprane")
                        msg = type("M", (), {"content": contenu})
                        return type("R", (), {"choices": [type("C", (), {"message": msg})]})
        return Mock()
    monkeypatch.setattr(correction, "_client", _client_mock)
    entries = [
        ("10:00:00", "Conversation", "j'ai mal à la gorge"),
        ("10:00:08", "Conversation", "je vous prescris du Doliprane"),
    ]
    res = correction.attribuer_locuteurs(entries)
    assert res[0][1] == "Patient" and res[1][1] == "Medecin"
    assert res[0][2] == "j'ai mal à la gorge"
    assert res[1][2] == "je vous prescris du Doliprane"


def test_attribuer_locuteurs_texte_falsifie_rejete(monkeypatch):
    """Si le LLM altère le contenu (concaténation différente) → fail-safe :
    les entries d'origine (Conversation) sont conservées, texte intact."""
    def _client_mock(timeout):
        class Mock:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        contenu = ("[10:00:00] PATIENT : TEXTE FALSIFIÉ\n"
                                   "[10:00:08] MÉDECIN : AUTRE FALSIFICATION")
                        msg = type("M", (), {"content": contenu})
                        return type("R", (), {"choices": [type("C", (), {"message": msg})]})
        return Mock()
    monkeypatch.setattr(correction, "_client", _client_mock)
    entries = [
        ("10:00:00", "Conversation", "j'ai mal à la gorge"),
        ("10:00:08", "Conversation", "je vous prescris du Doliprane"),
    ]
    assert correction.attribuer_locuteurs(entries) == entries


@pytest.mark.skipif(not GROQ_DISPONIBLE, reason="clé Groq absente")
def test_attribuer_locuteurs_basique():
    """Transcript simple avec prescription → la ligne « je vous prescris... »
    devient MÉDECIN (appel Groq réel)."""
    entries = [
        ("10:00:00", "Conversation", "Bonjour docteur, j'ai mal à la gorge depuis trois jours"),
        ("10:00:12", "Conversation", "Je vais examiner ça. Je vous prescris du Doliprane, un gramme trois fois par jour"),
    ]
    res = correction.attribuer_locuteurs(entries)
    assert len(res) == 2
    # La réplique de prescription doit être attribuée au médecin.
    presc = next(r for r in res if "prescris" in r[2])
    assert presc[1] == "Medecin"
    # Les textes ne sont pas modifiés.
    assert [r[2] for r in res] == [e[2] for e in entries]


# ------------------------------------------------ hallucination anglaise

def test_bascule_anglaise_detectee():
    txt = ("des joies, attirance, satire, competition You've seen the victory "
           "of fish and the equalizer")
    assert correction.contient_bascule_anglaise(txt)


def test_bascule_anglaise_francais_intact():
    # Une consultation FR normale ne doit jamais matcher (pas de faux positif).
    for txt in [
        "Je vous prescris du Doliprane un gramme trois fois par jour",
        "Bonjour docteur, j'ai mal a la gorge depuis trois jours",
        "Ouvrez grand la bouche, ca pique quand vous avalez",
    ]:
        assert not correction.contient_bascule_anglaise(txt), txt


def test_est_hallucination_anglaise():
    import transcription_consultation as tc
    assert tc.est_hallucination_generique(
        "You've seen the victory of fish and the equalizer")
    assert not tc.est_hallucination_generique(
        "Je vous prescris du Doliprane")


# ------------------------------------------------ nettoyage residuel [?]

def test_conversation_residuelle_marquee():
    entries = [
        ("10:00:00", "Medecin", "Bonjour"),
        ("10:00:05", "Conversation", "un passage que le LLM n'a pas su classer"),
    ]
    res = correction._nettoyer_conversation_residuelle(entries)
    assert res[0] == ("10:00:00", "Medecin", "Bonjour")     # inchangé
    assert res[1][1] == "Conversation"
    assert res[1][2].startswith("[?] ")                     # marqué douteux


def test_conversation_residuelle_anglaise_supprimee():
    entries = [
        ("10:00:00", "Patient", "j'ai mal a la gorge"),
        ("10:00:05", "Conversation", "the victory of the equalizer you have seen"),
    ]
    res = correction._nettoyer_conversation_residuelle(entries)
    assert len(res) == 1                                    # charabia supprimé
    assert res[0][1] == "Patient"


# ------------------------------------------------ scission de lignes

def _mock_client_retour(contenu):
    def _c(timeout):
        class Mock:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        msg = type("M", (), {"content": contenu})
                        return type("R", (), {"choices": [type("C", (), {"message": msg})]})
        return Mock()
    return _c


def test_attribuer_scission_sans_perte(monkeypatch):
    """Une ligne fusionnée est scindée en deux (même timestamp) sans perte."""
    entries = [
        ("10:00:00", "Conversation", "Ça va très bien et vous ? Ça va très bien."),
    ]
    sortie = ("[10:00:00] PATIENT : Ça va très bien et vous ?\n"
              "[10:00:00] MEDECIN : Ça va très bien.")
    monkeypatch.setattr(correction, "_client", _mock_client_retour(sortie))
    res = correction.attribuer_locuteurs(entries)
    assert len(res) == 2
    assert res[0][1] == "Patient" and res[1][1] == "Medecin"
    # Concaténation identique au caractère près (hors espaces).
    src = correction._sans_espaces(entries[0][2])
    out = correction._sans_espaces(res[0][2] + res[1][2])
    assert src == out


def test_attribuer_scission_perte_rejetee(monkeypatch):
    """Si le LLM perd du texte (concaténation altérée) → fail-safe."""
    entries = [
        ("10:00:00", "Conversation", "Ça va très bien et vous ? Ça va très bien."),
    ]
    sortie = "[10:00:00] PATIENT : Ça va très bien et vous ?"   # 2e moitié perdue
    monkeypatch.setattr(correction, "_client", _mock_client_retour(sortie))
    res = correction.attribuer_locuteurs(entries)
    assert res == entries          # inchangé (fail-safe)


def test_attribuer_lignes_en_moins_rejetees(monkeypatch):
    entries = [
        ("10:00:00", "Conversation", "phrase une"),
        ("10:00:05", "Conversation", "phrase deux"),
    ]
    sortie = "[10:00:00] MEDECIN : phrase une"    # une ligne manquante
    monkeypatch.setattr(correction, "_client", _mock_client_retour(sortie))
    assert correction.attribuer_locuteurs(entries) == entries


# ------------------------------------------ exclusion lignes douteuses du résumé

def test_est_ligne_douteuse():
    assert correction.est_ligne_douteuse("Conversation", "un texte")
    assert correction.est_ligne_douteuse("Medecin", "[?] douteux")
    assert correction.est_ligne_douteuse("Patient", "  [?] espace avant")
    assert not correction.est_ligne_douteuse("Medecin", "je vous prescris du Doliprane")
    assert not correction.est_ligne_douteuse("Patient", "j'ai mal à la gorge")


def test_ligne_douteuse_exclue_du_resume():
    """Le transcript envoyé au résumé ne doit contenir AUCUNE ligne douteuse."""
    entries = [
        ("10:00:00", "Patient",  "j'ai mal à la gorge"),
        ("10:00:08", "Medecin",  "je vous prescris du Doliprane"),
        ("10:00:20", "Conversation", "[?] et, langage, vieille et grosse perte de poids, alcoolique et soins"),
    ]
    # Reproduit la construction du transcript de résumé (worker).
    transcript = "\n".join(
        "[%s] %s : %s" % (h, loc, t)
        for h, loc, t in entries
        if not correction.est_ligne_douteuse(loc, t))
    assert "perte de poids" not in transcript
    assert "alcoolique" not in transcript
    # Les lignes fiables restent présentes.
    assert "Doliprane" in transcript and "gorge" in transcript


# ------------------------------------------ charabia français

def test_charabia_francais_filtre():
    import transcription_consultation as tc
    assert tc.est_hallucination_generique(
        "et, langage, vieille et grosse perte de poids, alcoolique et soins.")


def test_enumeration_medicale_legitime_conservee():
    import transcription_consultation as tc
    # Énumération légitime (2 virgules + contexte) → gardée.
    assert not tc.est_hallucination_generique(
        "douleurs abdominales, nausées, vomissements depuis trois jours")
    # Longue énumération MAIS avec verbe → gardée (pas de faux positif).
    assert not tc.est_hallucination_generique(
        "depuis trois jours j'ai de la toux, de la fièvre, des courbatures, et des maux de tête")


# ------------------------------------------------ charabia mots inconnus

def test_est_charabia_mots_inventes():
    # ≥ 3 mots longs, aucun français reconnu → charabia.
    assert correction._est_charabia("préimbant animationnel wxcvbn")
    assert correction._est_charabia("xyzabc qwerty zzzzzz")


def test_est_charabia_conservateur_deux_mots():
    # Seulement 2 mots longs inconnus → PAS classé charabia (conservateur ;
    # ce cas est attrapé en amont par le RMS adaptatif / no_speech_prob).
    assert not correction._est_charabia("préimbant l'animationnel")


def test_est_charabia_francais_reel_conserve():
    # De vraies phrases FR ne doivent jamais être considérées charabia.
    for phrase in [
        "je vous prescris du Doliprane trois fois par jour",
        "bonjour docteur comment allez-vous aujourd'hui",
        "j'ai des douleurs au ventre depuis hier",
        "prenez ce traitement pendant une semaine",
        "un passage que le médecin n'a pas su classer facilement",
    ]:
        assert not correction._est_charabia(phrase), phrase


def test_est_charabia_un_mot_connu_suffit():
    # Un seul mot français reconnu → considéré plausible (conservateur).
    assert not correction._est_charabia("bonjour blarg zzzzz frbtn")


def test_conversation_residuelle_charabia_supprimee():
    entries = [
        ("10:00:00", "Patient", "j'ai mal à la gorge"),
        ("10:00:05", "Conversation", "préimbant animationnel wxcvbn qwerty"),
    ]
    res = correction._nettoyer_conversation_residuelle(entries)
    assert len(res) == 1                       # charabia supprimé
    assert res[0][1] == "Patient"


def test_conversation_residuelle_francais_marquee_pas_supprimee():
    # Une ligne FR non attribuée reste (marquée [?]), pas supprimée.
    entries = [
        ("10:00:05", "Conversation", "je pense que cela ira beaucoup mieux demain"),
    ]
    res = correction._nettoyer_conversation_residuelle(entries)
    assert len(res) == 1
    assert res[0][2].startswith("[?] ")


# ------------------------------------------------ [HORS-SUJET]

def test_hors_sujet_traite_comme_douteux(monkeypatch):
    """Une ligne étiquetée HORS-SUJET par le LLM reste 'Conversation',
    marquée [?] (annexe) et exclue du résumé — jamais Médecin/Patient."""
    entries = [
        ("10:00:00", "Conversation", "j'ai mal à la gorge depuis hier"),
        ("10:00:08", "Conversation", "je vous recommande de faire des recherches sur les produits de la société"),
    ]
    sortie = ("[10:00:00] PATIENT : j'ai mal à la gorge depuis hier\n"
              "[10:00:08] HORS-SUJET : je vous recommande de faire des recherches sur les produits de la société")
    monkeypatch.setattr(correction, "_client", _mock_client_retour(sortie))
    res = correction.attribuer_locuteurs(entries)
    assert len(res) == 2
    assert res[0][1] == "Patient"
    # HORS-SUJET → Conversation marquée [?]
    assert res[1][1] == "Conversation"
    assert res[1][2].startswith("[?] ")
    # Et exclue du résumé
    assert correction.est_ligne_douteuse(res[1][1], res[1][2])
    assert not correction.est_ligne_douteuse(res[0][1], res[0][2])
