# -*- coding: utf-8 -*-
"""
Phase bêta (secret backend ECHO_MODE_BETA=1) : personne n'est facturé.

  - Backend : le VRAI index.ts de l'Edge Function est exécuté sous Node
    (tests/edge/test_mode_beta.mjs) avec un compte dont l'essai est expiré.
  - App : `beta` est propagé (cache config → get_app_state), neutralise le
    bandeau d'essai, et l'UI affiche « Version bêta » au lieu du paiement.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transcription_consultation as tc

_RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------ backend (Node)

def _node_utilisable():
    node = shutil.which("node")
    if not node:
        return None
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True,
                           timeout=10).stdout.strip().lstrip("v").split(".")
        if (int(v[0]), int(v[1])) >= (22, 13):     # module.stripTypeScriptTypes
            return node
    except Exception:
        pass
    return None


def test_edge_function_mode_beta_essai_expire():
    """Compte à l'essai expiré → /verifier-licence répond valide:true, beta:true
    avec ECHO_MODE_BETA=1 ; sans le secret, comportement inchangé."""
    node = _node_utilisable()
    if not node:
        pytest.skip("Node >= 22.13 requis pour exécuter index.ts")
    r = subprocess.run([node, os.path.join(_RACINE, "tests", "edge", "test_mode_beta.mjs")],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK — mode bêta" in r.stdout


def test_edge_function_lit_le_secret_et_logge():
    with open(os.path.join(_RACINE, "supabase", "functions", "echo-api", "index.ts"),
              encoding="utf-8") as f:
        src = f.read()
    assert 'Deno.env.get("ECHO_MODE_BETA") === "1"' in src
    assert src.count("mode bêta actif") >= 2          # verifier-licence + connexion


# ------------------------------------------------------------ app : état

def _api(monkeypatch, cfg):
    store = dict(cfg)
    monkeypatch.setattr(tc, "charger_config", lambda: dict(store))
    monkeypatch.setattr(tc, "sauver_config", lambda c: (store.clear(), store.update(c)))
    monkeypatch.setattr(tc, "_licence_cache", {"t": 0.0, "statut": None})
    api = tc.Api()
    monkeypatch.setattr(api, "_rafraichir_licence_fond", lambda cle: None)
    return api, store


def test_get_app_state_beta_neutralise_l_essai(monkeypatch):
    api, _ = _api(monkeypatch, {"cle_licence": "K", "licence_valide_cache": True,
                                "en_essai_cache": True, "jours_restants": 2,
                                "beta_cache": True})
    s = api.get_app_state()
    assert s["beta"] is True and s["licence_ok"] is True
    assert s["en_essai"] is False and s["jours_restants"] == 0
    assert s["licence_expired"] is False


def test_get_app_state_hors_beta_inchange(monkeypatch):
    api, _ = _api(monkeypatch, {"cle_licence": "K", "licence_valide_cache": True,
                                "en_essai_cache": True, "jours_restants": 2})
    s = api.get_app_state()
    assert s["beta"] is False
    assert s["en_essai"] is True and s["jours_restants"] == 2
    # Sans licence du tout : pas de bêta.
    api, _ = _api(monkeypatch, {})
    assert api.get_app_state()["beta"] is False


def test_rafraichissement_persiste_beta_et_debloque(monkeypatch):
    """Cache « expiré » + backend en bêta → le rafraîchissement en fond
    persiste valide/beta et notifie l'UI (onLicenceMaj(true))."""
    store = {"cle_licence": "K", "licence_valide_cache": False}
    monkeypatch.setattr(tc, "charger_config", lambda: dict(store))
    monkeypatch.setattr(tc, "sauver_config", lambda c: (store.clear(), store.update(c)))
    monkeypatch.setattr(tc, "_licence_cache", {"t": 0.0, "statut": None})
    monkeypatch.setattr(tc, "_licence_refresh_actif", False)
    monkeypatch.setattr(tc, "_verifier_licence",
                        lambda cle: {"ok": True, "valide": True, "beta": True,
                                     "en_essai": False, "jours_restants": 0})
    js = []
    monkeypatch.setattr(tc, "_safe_js", lambda win, code: js.append(code))

    class _Sync:
        def __init__(self, target=None, daemon=None, **k):
            self._t = target

        def start(self):
            self._t()
    monkeypatch.setattr(tc.threading, "Thread", _Sync)
    api = tc.Api()
    api._main_win = object()
    assert api.get_app_state()["licence_expired"] is True      # 1er affichage : ancien cache
    assert store["licence_valide_cache"] is True and store["beta_cache"] is True
    assert any("onLicenceMaj(true)" in c for c in js)
    s = api.get_app_state()                                    # cache mémoire frais
    assert s["licence_ok"] is True and s["beta"] is True


def test_connexion_beta_efface_le_cache_expire(monkeypatch):
    api, store = _api(monkeypatch, {"licence_valide_cache": False})
    monkeypatch.setattr(tc, "_appel_api", lambda ep, p, timeout=10: {
        "ok": True, "valide": True, "beta": True, "cle_licence": "K",
        "medecin_id": "m", "nom": "Dr T", "en_essai": False, "jours_restants": 0})
    res = api.auth_connexion("a@b.fr", "x")
    assert res["ok"] and res["expired"] is False
    assert store["licence_valide_cache"] is True and store["beta_cache"] is True
    assert api.get_app_state()["licence_ok"] is True


# ------------------------------------------------------------ app : UI

def _html():
    with open(os.path.join(_RACINE, "ui", "main_window.html"), encoding="utf-8") as f:
        return f.read()


def test_ui_beta_masque_essai_et_paiement():
    html = _html()
    # Plus aucun affichage direct du bandeau hors de la fonction dédiée
    # (l'unique occurrence est dans appliquerStatutEssai, après le test bêta).
    assert len(re.findall(r"if \(state(?: && state)?\.en_essai\) showTrialBanner", html)) == 1
    assert html.count("  appliquerStatutEssai(state);") == 3      # 3 points d'entrée
    m = re.search(r"function appliquerStatutEssai\(state\) \{.*?\n\}", html, re.DOTALL)
    assert m and "state.beta" in m.group(0) and "hide($('trial-banner'))" in m.group(0)
    assert re.search(r"function showTrialBanner\(jours\) \{\s*if \(modeBeta\) return;", html)
    # Profil : badge « Version bêta », bouton d'abonnement masqué.
    assert "badge.textContent = 'Version bêta'" in html
    assert "$('pp-btn-abonnement').classList.toggle('hidden', modeBeta)" in html
    with open(os.path.join(_RACINE, "ui", "style.css"), encoding="utf-8") as f:
        assert ".profile-badge.beta" in f.read()
