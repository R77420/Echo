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
