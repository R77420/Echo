# -*- coding: utf-8 -*-
"""_appel_api distingue les causes d'échec (DNS / réseau / HTTP / réponse)
au lieu d'un « Erreur réseau » fourre-tout indiagnosticable."""
import os, sys, socket, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import transcription_consultation as tc


class _HTTP(urllib.error.HTTPError):
    def __init__(self, code, body): self.code = code; self._b = body
    def read(self): return self._b


def _avec(monkeypatch, fn):
    monkeypatch.setattr(tc.urllib.request, "urlopen", fn)


def test_appel_api_entetes_supabase():
    """URL exacte + Authorization Bearer anon (clé en dur, donc embarquée)."""
    assert tc._ECHO_API == "https://muxoyiitqdnehuvbwcac.supabase.co/functions/v1/echo-api"
    assert tc._SUPABASE_ANON.startswith("eyJ")


def test_dns_distingue(monkeypatch):
    def f(req, timeout=0): raise urllib.error.URLError("[Errno 11001] getaddrinfo failed")
    _avec(monkeypatch, f)
    assert tc._appel_api("x", {}) is None
    assert tc._derniere_erreur_api["type"] == "dns"
    assert "injoignable" in tc._message_erreur_api()


def test_timeout_distingue(monkeypatch):
    def f(req, timeout=0): raise socket.timeout()
    _avec(monkeypatch, f)
    assert tc._appel_api("x", {}) is None
    assert tc._derniere_erreur_api["type"] == "reseau"
    assert "connexion" in tc._message_erreur_api()


def test_http_json_metier_remonte(monkeypatch):
    """Un 4xx avec {ok:false,error} n'est PAS une erreur réseau : le vrai
    message serveur est rendu à l'appelant."""
    def f(req, timeout=0): raise _HTTP(400, b'{"ok":false,"error":"Email d\xc3\xa9j\xc3\xa0 utilis\xc3\xa9."}')
    _avec(monkeypatch, f)
    r = tc._appel_api("inscription", {})
    assert r == {"ok": False, "error": "Email déjà utilisé."}
    assert tc._derniere_erreur_api["type"] == "http"


def test_http_sans_json(monkeypatch):
    def f(req, timeout=0): raise _HTTP(502, b"<html>Bad Gateway</html>")
    _avec(monkeypatch, f)
    assert tc._appel_api("x", {}) is None
    assert tc._derniere_erreur_api["type"] == "http"
    assert "502" in tc._message_erreur_api()


def test_inscription_message_precis(monkeypatch):
    """auth_inscription rend le message précis, plus « Erreur réseau »."""
    def f(req, timeout=0): raise urllib.error.URLError("[Errno 11001] getaddrinfo failed")
    _avec(monkeypatch, f)
    res = tc.Api().auth_inscription("Dr X", "x@y.fr", "pw")
    assert res["ok"] is False
    assert res["error"] != "Erreur réseau. Vérifiez votre connexion."
    assert "injoignable" in res["error"]
