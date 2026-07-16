# -*- coding: utf-8 -*-
"""
Tests du lancement au démarrage de Windows (demarrage.py).
Le registre est simulé (FakeWinreg) — aucun accès au vrai registre.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import demarrage


# --------------------------------------------------------------- faux winreg

class _FakeKey:
    def __init__(self, store, path, create):
        self.store = store
        self.path = path
        if create and path not in store:
            store[path] = {}
        if not create and path not in store:
            raise FileNotFoundError(path)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1
    KEY_SET_VALUE = 2
    KEY_QUERY_VALUE = 4

    def __init__(self, store=None, fail=False):
        self.store = store if store is not None else {}
        self.fail = fail

    def CreateKey(self, root, sub):
        if self.fail:
            raise OSError("accès refusé (politique d'entreprise)")
        return _FakeKey(self.store, (root, sub), create=True)

    def OpenKey(self, root, sub, res=0, access=0):
        if self.fail:
            raise OSError("accès refusé")
        return _FakeKey(self.store, (root, sub), create=False)

    def SetValueEx(self, key, name, res, typ, val):
        self.store[key.path][name] = val

    def DeleteValue(self, key, name):
        d = self.store.get(key.path, {})
        if name not in d:
            raise FileNotFoundError(name)
        del d[name]

    def QueryValueEx(self, key, name):
        d = self.store.get(key.path, {})
        if name not in d:
            raise FileNotFoundError(name)
        return (d[name], self.REG_SZ)


_KEY = ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run")


# --------------------------------------------------------------- tests

def test_registre_activer(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setattr(demarrage, "winreg", fake)
    res = demarrage.activer_demarrage()
    assert res["ok"] is True
    valeur = fake.store[_KEY]["Echo"]
    assert "--tray" in valeur                    # argument de démarrage tray
    assert valeur.endswith("--tray")
    assert demarrage.demarrage_actif() is True


def test_registre_desactiver(monkeypatch):
    fake = FakeWinreg({_KEY: {"Echo": '"C:\\x\\Echo.exe" --tray'}})
    monkeypatch.setattr(demarrage, "winreg", fake)
    assert demarrage.demarrage_actif() is True
    res = demarrage.desactiver_demarrage()
    assert res["ok"] is True
    assert "Echo" not in fake.store[_KEY]
    assert demarrage.demarrage_actif() is False


def test_registre_desactiver_deja_absent(monkeypatch):
    # Désactiver alors que la clé n'existe pas → ok (idempotent).
    fake = FakeWinreg({_KEY: {}})
    monkeypatch.setattr(demarrage, "winreg", fake)
    assert demarrage.desactiver_demarrage()["ok"] is True


def test_registre_echec_silencieux(monkeypatch):
    fake = FakeWinreg(fail=True)
    monkeypatch.setattr(demarrage, "winreg", fake)
    res = demarrage.activer_demarrage()
    assert res["ok"] is False and "error" in res
    # Aucune exception ne remonte, et l'état se lit sans planter.
    assert demarrage.demarrage_actif() is False


def test_etat_case_reflete_registre(monkeypatch):
    # Clé présente → actif ; absente → inactif.
    fake = FakeWinreg({_KEY: {"Echo": "cmd --tray"}})
    monkeypatch.setattr(demarrage, "winreg", fake)
    assert demarrage.demarrage_actif() is True
    fake.store[_KEY].clear()
    assert demarrage.demarrage_actif() is False


def test_api_startup(monkeypatch, tmp_path):
    """Les méthodes Api exposées au JS délèguent bien au registre."""
    import transcription_consultation as tc
    fake = FakeWinreg()
    monkeypatch.setattr(demarrage, "winreg", fake)
    api = tc.Api()
    assert api.get_startup_state()["enabled"] is False
    assert api.set_startup(True)["ok"] is True
    assert api.get_startup_state()["enabled"] is True
    assert api.set_startup(False)["ok"] is True
    assert api.get_startup_state()["enabled"] is False


def test_commande_contient_tray():
    cmd = demarrage.commande_demarrage()
    assert cmd.endswith("--tray")
