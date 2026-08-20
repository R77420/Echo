# -*- coding: utf-8 -*-
"""Méthodes Api de vérification d'email (inscription 2 étapes + reset).
Le backend est mocké au niveau _appel_api : on vérifie les payloads exacts
et la remontée du VRAI message serveur (jamais un générique)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transcription_consultation as tc


def _capture(monkeypatch, reponse):
    appels = []
    def faux(endpoint, payload, timeout=10):
        appels.append((endpoint, payload))
        return reponse
    monkeypatch.setattr(tc, "_appel_api", faux)
    return appels


def test_demander_code_payload(monkeypatch):
    appels = _capture(monkeypatch, {"ok": True})
    res = tc.Api().demander_code("x@y.fr", "inscription")
    assert res == {"ok": True}
    assert appels == [("demander-code",
                       {"email": "x@y.fr", "type": "inscription"})]


def test_demander_code_email_pris_remonte(monkeypatch):
    _capture(monkeypatch, {"ok": False, "email_pris": True,
                           "erreur": "Un compte existe déjà avec cet email"})
    res = tc.Api().demander_code("x@y.fr", "inscription")
    assert res["email_pris"] is True
    assert "existe déjà" in res["erreur"]        # message serveur, pas générique


def test_verifier_code_jeton(monkeypatch):
    appels = _capture(monkeypatch, {"ok": True, "jeton_verification": "abc123"})
    res = tc.Api().verifier_code("x@y.fr", "123456", "reset")
    assert res["jeton_verification"] == "abc123"
    assert appels[0] == ("verifier-code",
                         {"email": "x@y.fr", "code": "123456", "type": "reset"})


def test_verifier_code_messages_serveur(monkeypatch):
    for msg in ("Code incorrect", "Code expiré",
                "Trop de tentatives, demandez un nouveau code"):
        _capture(monkeypatch, {"ok": False, "erreur": msg})
        res = tc.Api().verifier_code("x@y.fr", "000000", "inscription")
        assert res["erreur"] == msg              # affiché tel quel par l'UI


def test_inscription_transmet_le_jeton(monkeypatch):
    appels = _capture(monkeypatch, {"ok": True, "cle_licence": "K",
                                    "medecin_id": "m1", "nom": "Dr X"})
    store = {}
    monkeypatch.setattr(tc, "charger_config", lambda: dict(store))
    monkeypatch.setattr(tc, "sauver_config", lambda c: store.update(c))
    res = tc.Api().auth_inscription("Dr X", "x@y.fr", "pw", "Cardiologie", "JETON42")
    assert res["ok"] is True
    endpoint, payload = appels[0]
    assert endpoint == "inscription"
    assert payload["jeton_verification"] == "JETON42"


def test_reinitialiser_mot_de_passe_payload(monkeypatch):
    appels = _capture(monkeypatch, {"ok": True})
    res = tc.Api().reinitialiser_mot_de_passe("x@y.fr", "JETON42", "NouveauMdp")
    assert res == {"ok": True}
    assert appels[0] == ("reinitialiser-mot-de-passe",
                         {"email": "x@y.fr", "jeton_verification": "JETON42",
                          "nouveau_mot_de_passe": "NouveauMdp"})


def test_echec_reseau_message_precis(monkeypatch):
    def faux(endpoint, payload, timeout=10):
        tc._derniere_erreur_api = {"type": "dns", "detail": "getaddrinfo"}
        return None
    monkeypatch.setattr(tc, "_appel_api", faux)
    for res in (tc.Api().demander_code("x@y.fr", "reset"),
                tc.Api().verifier_code("x@y.fr", "1", "reset"),
                tc.Api().reinitialiser_mot_de_passe("x@y.fr", "j", "p")):
        assert res["ok"] is False
        assert "injoignable" in res["error"]     # message DNS précis, pas générique
