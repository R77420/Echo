# -*- coding: utf-8 -*-
"""
Tests de la saisie différée du nom (« Nommer plus tard ») et de la
détection du nom du patient dans la conversation.
"""
import datetime
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import correction
import storage
import transcription_consultation as tc

GROQ_OK = bool(correction.GROQ_API_KEY)


# ------------------------------------------------ détection du nom (Groq réel)

from conftest import groq_reel


@groq_reel
def test_detecter_nom_salutation():
    entries = [
        ("10:00:01", "Medecin", "Bonjour madame Dubois, comment allez-vous ?"),
        ("10:00:06", "Patient", "Bonjour docteur, ça va, j'ai un peu mal à la gorge"),
    ]
    d = correction.detecter_nom_patient(entries)
    assert d is not None
    assert d["nom"] == "DUBOIS"
    assert d["confiance"] == "haute"


@groq_reel
def test_detecter_nom_absent():
    entries = [
        ("10:00:01", "Medecin", "Bonjour, qu'est-ce qui vous amène aujourd'hui ?"),
        ("10:00:06", "Patient", "J'ai des douleurs au dos depuis une semaine"),
    ]
    d = correction.detecter_nom_patient(entries)
    assert d is None or d["nom"] == ""


@groq_reel
def test_detecter_nom_ambigu():
    """« le docteur Martin » n'est PAS le patient → confiance faible ou vide."""
    entries = [
        ("10:00:01", "Patient", "J'ai vu le docteur Martin la semaine dernière pour ce problème"),
        ("10:00:08", "Medecin", "Très bien, et qu'est-ce qu'il vous a dit ?"),
    ]
    d = correction.detecter_nom_patient(entries)
    assert d is None or d["nom"] == "" or d["confiance"] == "faible"


# ------------------------------------------------ fail-safe

def test_detecter_nom_failsafe(monkeypatch):
    monkeypatch.setattr(correction, "_client", lambda t: None)
    assert correction.detecter_nom_patient(
        [("10:00:01", "Medecin", "Bonjour madame Dubois")]) is None
    assert correction.detecter_nom_patient([]) is None


# ------------------------------------------------ « Nommer plus tard »

def _api_sandbox(monkeypatch, tmp_path):
    chemin = os.path.join(tmp_path, "consultations.json")
    monkeypatch.setattr(tc, "chemin_consultations", lambda: chemin)
    monkeypatch.setattr(tc, "charger_config",
                        lambda: {"dossier_sauvegarde": str(tmp_path)})
    monkeypatch.setattr(tc, "sauver_config", lambda c: None)
    # Neutraliser les workers IA (extraction CR) — pas d'appels réseau ici.
    monkeypatch.setattr(tc.Api, "_extraction_cr_worker", lambda self, cid: None)
    api = tc.Api()
    api._start_time = datetime.datetime(2026, 7, 14, 9, 15)
    with api._lock:
        api._entries[:] = [("09:15:02", "Medecin", "Bonjour madame Dubois"),
                           ("09:15:08", "Patient", "Bonjour docteur")]
    return api, chemin


def test_nommer_plus_tard(monkeypatch, tmp_path):
    api, chemin = _api_sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(correction, "detecter_nom_patient",
                        lambda entries: {"nom": "DUBOIS", "prenom": "", "confiance": "haute"})
    res = api.save_sans_nom([])
    assert res["ok"], res
    # Libellé provisoire + flag + fichier auto.
    data = storage.charger_consultations(chemin)
    c = data[0]
    assert c["patient"]["nom"] == "Consultation de 9h15"
    assert c["nom_a_saisir"] is True
    assert os.path.isfile(res["file_path"])
    assert "Consultation_2026-07-14_09h15" in res["file_path"]
    # Suggestion stockée par le worker (thread) — attendre brièvement.
    for _ in range(20):
        time.sleep(0.1)
        c = storage.charger_consultations(chemin)[0]
        if c.get("nom_suggere"):
            break
    assert c["nom_suggere"] == {"nom": "DUBOIS", "prenom": "", "confiance": "haute"}
    # File d'attente + flags de routage.
    file_attente = api.get_consultations_a_nommer()
    assert len(file_attente) == 1
    assert file_attente[0]["libelle"] == "Consultation de 9h15"
    assert len(file_attente[0]["extrait"]) == 2
    flags = api.get_derniere_consultation_flags()
    assert flags["a_nommer"] is True


def test_saisie_serie(monkeypatch, tmp_path):
    api, chemin = _api_sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(correction, "detecter_nom_patient", lambda e: None)
    api.save_sans_nom([])
    cid = api._saved_id
    # Nommer → flag retiré, libellé remplacé, docx réécrit avec le vrai nom.
    res = api.nommer_consultation(cid, "DUBOIS", "Marie")
    assert res["ok"], res
    c = storage.charger_consultations(chemin)[0]
    assert c["nom_a_saisir"] is False
    assert c["patient"]["nom"] == "DUBOIS"
    assert c["patient"]["prenom"] == "Marie"
    assert api.get_consultations_a_nommer() == []
    # Nom vide refusé.
    assert not api.nommer_consultation(cid, "  ")["ok"]
