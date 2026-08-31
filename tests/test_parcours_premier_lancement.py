# -*- coding: utf-8 -*-
"""Parcours de premier lancement : un compte tout neuf doit passer par
l'onboarding (bienvenue → thème → périphériques) puis la visite guidée.

Régression verrouillée ici : onboarding_done était déduit de doctor_name,
que l'inscription s'est mise à stocker immédiatement → compte neuf
considéré comme déjà onboardé → visite guidée jamais proposée."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transcription_consultation as tc


def _api(monkeypatch, store):
    monkeypatch.setattr(tc, "charger_config", lambda: dict(store))
    monkeypatch.setattr(tc, "sauver_config", lambda c: store.update(c))
    monkeypatch.setattr(tc, "_verifier_licence",
                        lambda cle: {"valide": True})
    return tc.Api()


def test_compte_neuf_pas_onboarde(monkeypatch):
    """Inscription faite (doctor_name stocké) mais onboarding PAS fait →
    onboarding_done doit être False : le JS enchaîne sur la bienvenue."""
    store = {"doctor_name": "Dr Neuf", "cle_licence": "K"}
    api = _api(monkeypatch, store)
    assert api.get_app_state()["onboarding_done"] is False


def test_onboarding_marque_par_complete_onboarding(monkeypatch):
    store = {"doctor_name": "Dr Neuf", "cle_licence": "K"}
    api = _api(monkeypatch, store)
    assert api.complete_onboarding("", "Micro X", "Sortie Y")["ok"] is True
    assert store["onboarding_fait"] is True
    assert api.get_app_state()["onboarding_done"] is True


def test_retro_compat_installs_existantes(monkeypatch):
    """Les installs d'avant le flag (micro posé par l'ancien
    complete_onboarding) restent considérées onboardées — personne ne
    doit refaire l'onboarding à la mise à jour."""
    store = {"doctor_name": "Dr Ancien", "cle_licence": "K",
             "micro": "Micro USB", "sortie": "HP"}
    api = _api(monkeypatch, store)
    assert api.get_app_state()["onboarding_done"] is True


def test_visite_flag_pas_pose_par_inscription(monkeypatch):
    """Ni l'inscription ni complete_onboarding ne posent visite_faite :
    seul marquer_decouverte (fin de visite / Passer) le fait."""
    store = {"doctor_name": "Dr Neuf", "cle_licence": "K"}
    api = _api(monkeypatch, store)
    api.complete_onboarding("", "m", "s")
    assert not store.get("visite_faite")
    assert api.get_decouverte()["visite_faite"] is False
    api.marquer_decouverte("visite_faite")
    assert store["visite_faite"] is True
