# -*- coding: utf-8 -*-
"""
Tests des seuils de verdict du test microphone (verdict_micro).
Aucune capture réelle : on injecte RMS + transcription et on vérifie le verdict.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transcription_consultation as tc

# Transcription « correcte » : reprend la phrase de référence.
PHRASE_OK = "Bonjour, je viens vous voir car j'ai mal à la gorge depuis trois jours."
# Transcription « partielle » : un seul mot-clé retrouvé.
PHRASE_PARTIELLE = "Bonjour."
PHRASE_VIDE = ""


# ------------------------------------------------------------ verdict adapté
def test_adapte_rms_fort_transcription_correcte():
    assert tc.verdict_micro(0.030, PHRASE_OK) == "adapte"
    # Juste au-dessus du seuil OK.
    assert tc.verdict_micro(0.016, PHRASE_OK) == "adapte"


# ------------------------------------------------------------ verdict faible
def test_faible_rms_intermediaire():
    # RMS entre 0.006 et 0.015 mais transcription correcte → faible.
    assert tc.verdict_micro(0.010, PHRASE_OK) == "faible"
    assert tc.verdict_micro(0.006, PHRASE_OK) == "faible"


def test_faible_rms_fort_mais_transcription_partielle():
    # RMS élevé mais transcription incomplète → faible (pas adapté).
    assert tc.verdict_micro(0.030, PHRASE_PARTIELLE) == "faible"


# --------------------------------------------------------- verdict insuffisant
def test_insuffisant_rms_trop_bas():
    assert tc.verdict_micro(0.005, PHRASE_OK) == "insuffisant"
    assert tc.verdict_micro(0.0, PHRASE_OK) == "insuffisant"


def test_insuffisant_aucune_transcription():
    # Même avec un RMS correct, aucune transcription → insuffisant.
    assert tc.verdict_micro(0.030, PHRASE_VIDE) == "insuffisant"
    assert tc.verdict_micro(0.030, "   ") == "insuffisant"


def test_insuffisant_transcription_hors_sujet():
    # Du bruit transcrit sans aucun mot-clé attendu → insuffisant.
    assert tc.verdict_micro(0.030, "euh hmm ok") == "insuffisant"


# ------------------------------------------------------ frontières exactes
def test_frontiere_seuil_ok():
    # Exactement 0.015 n'est PAS > 0.015 → faible.
    assert tc.verdict_micro(0.015, PHRASE_OK) == "faible"


def test_frontiere_seuil_faible():
    # Exactement 0.006 n'est PAS < 0.006 → faible (si transcription).
    assert tc.verdict_micro(0.006, PHRASE_OK) == "faible"
    # Juste en dessous → insuffisant.
    assert tc.verdict_micro(0.0059, PHRASE_OK) == "insuffisant"


# ----------------------------------------------- qualité de transcription
def test_qualite_insensible_accents_casse():
    # Sans accents / majuscules : les mots-clés doivent quand même compter.
    q = tc._qualite_transcription_test("BONJOUR VIENS VOIR MAL GORGE DEPUIS TROIS JOURS")
    assert q == 1.0


def test_mic_test_fait_flag(monkeypatch, tmp_path):
    """mic_test_fait() reflète la présence de mic_test_date en config."""
    store = {}
    monkeypatch.setattr(tc, "charger_config", lambda: dict(store))
    api = tc.Api()
    assert api.mic_test_fait()["fait"] is False
    store["mic_test_date"] = "2026-07-16T10:00:00"
    store["mic_test_verdict"] = "adapte"
    res = api.mic_test_fait()
    assert res["fait"] is True and res["verdict"] == "adapte"
