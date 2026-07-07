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
