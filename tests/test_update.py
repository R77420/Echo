# -*- coding: utf-8 -*-
"""Auto-update : detection de version et absence de dependances non
embarquees (l'ancien `import requests` echouait en silence dans le build
-> l'auto-update n'a jamais fonctionne)."""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import transcription_consultation as tc


def test_version_tuple_ignore_le_v():
    assert tc._version_tuple("2.2.1") == (2, 2, 1)
    # le tag GitHub arrive avec 'v' -> retire par lstrip('v') dans l'appelant
    assert tc._version_tuple("v2.2.1".lstrip("v")) == (2, 2, 1)
    assert tc._version_tuple("2.10.0") > tc._version_tuple("2.9.9")


def test_update_sans_requests():
    """Les fonctions d'update n'importent QUE la stdlib : `requests` n'est
    pas embarque dans le build PyInstaller."""
    for fn in (tc.verifier_mise_a_jour, tc._dl_update):
        src = inspect.getsource(fn)
        assert "import requests" not in src, fn.__name__


def test_detection_maj(monkeypatch):
    """tag v2.9.9 vs locale 2.2.1 -> disponible, avec l'URL du .exe."""
    import io, json as _j
    class _R:
        headers = {}
        def read(self): return _j.dumps({
            "tag_name": "v2.9.9",
            "assets": [{"name": "EchoSetup.exe",
                        "browser_download_url": "https://x/EchoSetup.exe"}],
        }).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(tc.urllib.request, "urlopen", lambda req, timeout=0: _R())
    monkeypatch.setattr(tc, "_log_update", lambda m: None)
    r = tc.verifier_mise_a_jour()
    assert r == {"disponible": True, "version": "2.9.9",
                 "url": "https://x/EchoSetup.exe"}


def test_echec_api_ne_plante_pas(monkeypatch):
    """Rate limit / reseau -> {'disponible': False} + ligne de log, pas de crash."""
    logs = []
    def boom(req, timeout=0): raise OSError("rate limited")
    monkeypatch.setattr(tc.urllib.request, "urlopen", boom)
    monkeypatch.setattr(tc, "_log_update", logs.append)
    assert tc.verifier_mise_a_jour() == {"disponible": False}
    assert logs and "OSError" in logs[0]
