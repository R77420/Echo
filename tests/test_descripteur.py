# -*- coding: utf-8 -*-
"""
Tests du descripteur de ligne de consultation et de la synthèse patient
(storage.descripteur_consultation / storage.synthese_patient).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage


def _c(cid, date, valide=None, motif=None, traitements=None, entries=None):
    els = None
    if motif is not None or traitements is not None:
        els = {"motif": motif or [], "observations": [],
               "traitements": traitements or [], "suivi": []}
    return {"id": cid, "date": date, "cr_valide": valide,
            "cr_elements": els, "entries": entries or [],
            "duration_min": 8, "patient": {"nom": "YASMINE"}}


# ------------------------------------------------------ priorités du descripteur

def test_motif_valide_prioritaire():
    c = _c("c1", "2026-07-14T09:00:00", valide=True,
           motif=["Douleurs lombaires depuis une semaine"],
           entries=[["09:00", "Patient", "je viens vous voir"]])
    texte, typ = storage.descripteur_consultation(c)
    assert texte == "Douleurs lombaires depuis une semaine"
    assert typ == "valide"


def test_motif_extrait_non_valide():
    c = _c("c1", "2026-07-14T09:00:00", valide=False, motif=["angine"])
    texte, typ = storage.descripteur_consultation(c)
    assert texte == "angine"
    assert typ == "extrait"          # affiché grisé/italique par la vue


def test_fallback_premiere_replique():
    c = _c("c1", "2026-07-14T09:00:00", valide=False, entries=[
        ["09:00", "Medecin", "Bonjour, installez-vous."],
        ["09:00", "Patient", "je viens vous voir parce que j'ai mal à la gorge depuis trois jours et ça empire"],
    ])
    texte, typ = storage.descripteur_consultation(c)
    assert typ == "replique"
    assert texte.startswith("je viens vous voir parce que j'ai mal")
    assert texte.endswith("…")
    assert len(texte) <= storage._DESC_MAX + 2   # tronqué proprement


def test_fallback_replique_conversation():
    # Mode cabinet non attribué : les tours « Conversation » comptent aussi.
    c = _c("c1", "2026-07-14T09:00:00", entries=[
        ["09:00", "Conversation", "j'ai des vertiges le matin"]])
    texte, typ = storage.descripteur_consultation(c)
    assert (texte, typ) == ("j'ai des vertiges le matin", "replique")


def test_fallback_tiret():
    # Transcript vide (ou médecin seul) → aucun descripteur, la vue met « — ».
    assert storage.descripteur_consultation(_c("c1", "x")) == ("", "aucun")
    c = _c("c1", "x", entries=[["09:00", "Medecin", "Bonjour."]])
    assert storage.descripteur_consultation(c) == ("", "aucun")


# ------------------------------------------------------------ synthèse patient

def test_synthese_patient():
    consults = [
        _c("c1", "2026-07-01T09:00:00", valide=True, motif=["hypertension"],
           traitements=["Amlodipine 5 mg"]),
        _c("c2", "2026-07-08T09:00:00", valide=True, motif=["lombalgie"],
           traitements=["Doliprane 1 g", "Ibuprofène 400"]),
        _c("c3", "2026-07-14T09:00:00", valide=True,
           motif=["Douleurs lombaires depuis une semaine"],
           traitements=["Doliprane 1 g"]),   # doublon → dédoublonné
        _c("autre", "2026-07-15T09:00:00", valide=True, motif=["x"],
           traitements=["Autre médicament"]),   # autre patient : exclu
    ]
    s = storage.synthese_patient(consults, ["c1", "c2", "c3"])
    d = s["derniere"]
    assert d["date"] == "2026-07-14T09:00:00"
    assert d["duration_min"] == 8
    assert d["motif"] == "Douleurs lombaires depuis une semaine"
    assert d["traitements"] == ["Doliprane 1 g"]
    # Agrégat : plus récent d'abord, sans doublon, sans l'autre patient.
    assert s["traitements_recents"] == [
        "Doliprane 1 g", "Ibuprofène 400", "Amlodipine 5 mg"]


def test_synthese_ignore_non_validees():
    consults = [
        _c("c1", "2026-07-14T09:00:00", valide=False, motif=["angine"],
           traitements=["Amoxicilline"]),
    ]
    s = storage.synthese_patient(consults, ["c1"])
    # Dernière consultation affichée (motif extrait) mais traitements non
    # confirmés : pas de traitements tant que le CR n'est pas validé.
    assert s["derniere"]["motif"] == "angine"
    assert s["derniere"]["traitements"] == []
    assert s["traitements_recents"] == []


def test_synthese_patient_vide():
    s = storage.synthese_patient([], [])
    assert s == {"derniere": None, "traitements_recents": []}
