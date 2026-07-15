# -*- coding: utf-8 -*-
"""
Tests de l'écran de validation du compte-rendu :
extraction structurée (resume.py), flux cr_valide (storage + Api).
Les tests Groq réels nécessitent GROQ_KEY.py + réseau.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resume
import storage

try:
    from GROQ_KEY import GROQ_API_KEY
except Exception:
    GROQ_API_KEY = ""

GROQ_OK = bool(GROQ_API_KEY)

TRANSCRIPT = """[10:00:01] Patient : Bonjour docteur, j'ai mal à la gorge depuis trois jours et j'ai trop faim tout le temps
[10:00:15] Médecin : Je vais examiner ça. Ouvrez grand la bouche
[10:00:30] Médecin : C'est une angine. Je vous prescris du Doliprane, un gramme trois fois par jour
[10:00:45] Médecin : Revenez me voir dans une semaine si ça ne passe pas"""


# ------------------------------------------------------------ parsing/failsafe

def test_json_malforme_failsafe():
    assert resume._parse_elements_json("pas du json") is None
    assert resume._parse_elements_json("") is None
    assert resume._parse_elements_json('{"motif": "pas une liste"}') == {
        "motif": [], "observations": [], "traitements": [], "suivi": []}
    # JSON dans un bloc markdown → accepté.
    d = resume._parse_elements_json('```json\n{"motif": ["mal de gorge"]}\n```')
    assert d["motif"] == ["mal de gorge"]
    assert d["traitements"] == []


def test_extraction_sans_cle_structure_vide():
    d = resume.extraire_elements_cr(TRANSCRIPT, "")
    assert d == resume.elements_vides()
    assert resume.extraire_elements_cr("", "cle") == resume.elements_vides()


def test_elements_vers_resume_format():
    """Le texte produit suit le format standard parsé par ecrire_docx."""
    elements = {"motif": ["mal de gorge"], "observations": [],
                "traitements": ["Doliprane 1 g 3x/j"], "suivi": []}
    txt = resume.elements_vers_resume(elements)
    assert "Motif :" in txt and "- mal de gorge" in txt
    assert "Traitements et prescriptions évoqués :" in txt
    assert "- Doliprane 1 g 3x/j" in txt
    # Catégorie vide → "Non précisé" (rendu docx existant).
    assert "Non précisé" in txt


# ------------------------------------------------------------ Groq réel

@pytest.mark.skipif(not GROQ_OK, reason="clé Groq absente")
def test_extraire_elements_json():
    d = resume.extraire_elements_cr(TRANSCRIPT, GROQ_API_KEY)
    assert set(d.keys()) == set(resume.CR_CATEGORIES)
    # Motif et traitements peuplés depuis le transcript.
    assert d["motif"], d
    assert any("olipran" in t for t in d["traitements"]), d
    assert d["suivi"], d


@pytest.mark.skipif(not GROQ_OK, reason="clé Groq absente")
def test_pas_de_terme_medical_invente():
    """« j'ai trop faim » ne doit PAS devenir « hyperphagie »."""
    d = resume.extraire_elements_cr(TRANSCRIPT, GROQ_API_KEY)
    tout = " ".join(sum(d.values(), [])).lower()
    assert "hyperphagie" not in tout, d
    assert "polyphagie" not in tout, d


@pytest.mark.skipif(not GROQ_OK, reason="clé Groq absente")
def test_categorie_vide():
    """Transcript sans traitement → traitements: []."""
    t = ("[10:00:01] Patient : Bonjour docteur, je viens pour mon certificat "
         "de sport\n[10:00:10] Médecin : Très bien, je vous l'établis tout de suite")
    d = resume.extraire_elements_cr(t, GROQ_API_KEY)
    assert d["traitements"] == [], d


# ------------------------------------------------------------ flag cr_valide

def _record(cid, cr_valide=False):
    return {"id": cid, "date": "2026-07-13T10:00:00",
            "patient": {"nom": "TEST", "prenom": "", "naissance": "", "motif": ""},
            "summary": "", "file_path": "", "duration_min": 5,
            "cr_valide": cr_valide, "cr_elements": None,
            "entries": [["10:00:01", "Patient", "bonjour"]], "annexes": []}


def test_cr_valide_flag(tmp_path):
    chemin = os.path.join(tmp_path, "consultations.json")
    storage.ajouter_consultation(chemin, _record("c1"))
    # Sauvegardé non validé → « à valider ».
    data = storage.charger_consultations(chemin)
    assert data[0]["cr_valide"] is False
    # Extraction terminée → cr_elements rempli.
    storage.maj_consultation_cr(chemin, "c1",
                                cr_elements={"motif": ["x"], "observations": [],
                                             "traitements": [], "suivi": []})
    data = storage.charger_consultations(chemin)
    assert data[0]["cr_elements"]["motif"] == ["x"]
    assert data[0]["cr_valide"] is False        # toujours à valider
    # Validation → cr_valide=True + summary écrit.
    storage.maj_consultation_cr(chemin, "c1", cr_valide=True,
                                summary="RÉSUMÉ...\nMotif :\n- x")
    data = storage.charger_consultations(chemin)
    assert data[0]["cr_valide"] is True
    assert "Motif" in data[0]["summary"]


def test_api_valider_et_ignorer(monkeypatch, tmp_path):
    import transcription_consultation as tc
    chemin = os.path.join(tmp_path, "consultations.json")
    monkeypatch.setattr(tc, "chemin_consultations", lambda: chemin)
    storage.ajouter_consultation(chemin, _record("c1"))
    storage.ajouter_consultation(chemin, _record("c2"))
    api = tc.Api()

    p = api.get_cr_a_valider()
    assert p["count"] == 2 and p["dernier_id"] == "c2"

    # Valider c2 avec des éléments cochés → summary construit, flag posé.
    res = api.valider_cr("c2", {"motif": ["mal de gorge"], "observations": [],
                                "traitements": ["Doliprane"], "suivi": []})
    assert res["ok"], res
    d = api.get_cr_elements("c2")
    assert d["cr_valide"] is True
    data = storage.charger_consultations(chemin)
    c2 = next(c for c in data if c["id"] == "c2")
    assert "- mal de gorge" in c2["summary"] and "- Doliprane" in c2["summary"]

    # Ignorer c1 → plus rien à valider.
    assert api.ignorer_cr("c1")["ok"]
    assert api.get_cr_a_valider()["count"] == 0


# ------------------------------------------------ validation groupée

def _rec_v(cid, nom="DUPONT", cr_valide=False, nom_a_saisir=False, elements=None):
    return {"id": cid, "date": "2026-07-14T10:00:00",
            "patient": {"nom": nom, "prenom": "", "naissance": "", "motif": ""},
            "summary": "", "file_path": "", "duration_min": 8,
            "cr_valide": cr_valide, "nom_a_saisir": nom_a_saisir,
            "cr_elements": elements or {"motif": ["x"], "observations": [],
                                        "traitements": [], "suivi": []},
            "entries": [["10:00:01", "Patient", "bonjour"]], "annexes": []}


def test_validation_groupee(monkeypatch, tmp_path):
    import transcription_consultation as tc
    chemin = os.path.join(tmp_path, "consultations.json")
    monkeypatch.setattr(tc, "chemin_consultations", lambda: chemin)
    for cid in ("a", "b", "c"):
        storage.ajouter_consultation(chemin, _rec_v(cid, nom=cid.upper()))
    api = tc.Api()

    res = api.valider_cr_groupe([
        {"cid": "a", "elements": {"motif": ["mal de gorge"], "observations": [],
                                   "traitements": [], "suivi": []}},
        {"cid": "b", "elements": {"motif": ["fièvre"], "observations": [],
                                   "traitements": [], "suivi": []}},
        {"cid": "c", "elements": {"motif": ["toux"], "observations": [],
                                   "traitements": [], "suivi": []}},
    ])
    assert res["ok"]
    assert sorted(res["valides"]) == ["a", "b", "c"]
    assert res["ignores"] == []
    data = {c["id"]: c for c in storage.charger_consultations(chemin)}
    assert all(data[cid]["cr_valide"] is True for cid in ("a", "b", "c"))
    assert "- mal de gorge" in data["a"]["summary"]
    assert "- fièvre" in data["b"]["summary"]


def test_groupee_ignore_sans_nom(monkeypatch, tmp_path):
    import transcription_consultation as tc
    chemin = os.path.join(tmp_path, "consultations.json")
    monkeypatch.setattr(tc, "chemin_consultations", lambda: chemin)
    storage.ajouter_consultation(chemin, _rec_v("a", nom="MARTIN"))
    storage.ajouter_consultation(chemin, _rec_v("b", nom="DURAND"))
    # 3e sans nom (libellé provisoire + flag).
    storage.ajouter_consultation(chemin, _rec_v(
        "c", nom="Consultation de 9h15", nom_a_saisir=True))
    api = tc.Api()

    res = api.valider_cr_groupe([
        {"cid": "a", "elements": {"motif": ["m"], "observations": [], "traitements": [], "suivi": []}},
        {"cid": "b", "elements": {"motif": ["m"], "observations": [], "traitements": [], "suivi": []}},
        {"cid": "c", "elements": {"motif": ["m"], "observations": [], "traitements": [], "suivi": []}, "nom": ""},
    ])
    assert sorted(res["valides"]) == ["a", "b"]
    assert res["ignores"] == ["c"]
    data = {c["id"]: c for c in storage.charger_consultations(chemin)}
    assert data["a"]["cr_valide"] is True and data["b"]["cr_valide"] is True
    # La 3e reste dans la file (non validée, toujours à nommer).
    assert data["c"]["cr_valide"] is False
    assert data["c"]["nom_a_saisir"] is True


def test_groupee_nomme_puis_valide(monkeypatch, tmp_path):
    """Un nom saisi en ligne → la consultation est nommée PUIS validée."""
    import transcription_consultation as tc
    chemin = os.path.join(tmp_path, "consultations.json")
    monkeypatch.setattr(tc, "chemin_consultations", lambda: chemin)
    storage.ajouter_consultation(chemin, _rec_v(
        "c", nom="Consultation de 9h15", nom_a_saisir=True))
    api = tc.Api()
    res = api.valider_cr_groupe([
        {"cid": "c", "elements": {"motif": ["m"], "observations": [], "traitements": [], "suivi": []},
         "nom": "LEROY", "prenom": "Emma"},
    ])
    assert res["valides"] == ["c"]
    c = storage.charger_consultations(chemin)[0]
    assert c["cr_valide"] is True and c["nom_a_saisir"] is False
    assert c["patient"]["nom"] == "LEROY" and c["patient"]["prenom"] == "Emma"


def test_decochage_dans_liste(monkeypatch, tmp_path):
    """Un élément décoché (absent des elements envoyés) n'apparaît pas dans
    le résumé validé."""
    import transcription_consultation as tc
    chemin = os.path.join(tmp_path, "consultations.json")
    monkeypatch.setattr(tc, "chemin_consultations", lambda: chemin)
    storage.ajouter_consultation(chemin, _rec_v("a", nom="MARTIN"))
    api = tc.Api()
    # L'IA proposait « hallucination » ; le médecin l'a décochée en liste.
    api.valider_cr_groupe([
        {"cid": "a", "elements": {"motif": ["vrai motif"], "observations": [],
                                   "traitements": [], "suivi": []}},
    ])
    c = storage.charger_consultations(chemin)[0]
    assert "vrai motif" in c["summary"]
    assert "hallucination" not in c["summary"]
