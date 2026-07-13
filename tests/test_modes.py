# -*- coding: utf-8 -*-
"""
Tests du choix de mode de consultation (cabinet / téléconsultation).
Vérifie que start() initialise les bons threads de capture selon le mode.
Aucun périphérique réel ni thread réel n'est utilisé (tout est mocké).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transcription_consultation as tc


class _FakeDevice:
    def __init__(self, name):
        self.name = name


class _FakeThread:
    """Enregistre (target, args) et ne démarre aucun vrai thread."""
    instances = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args
        _FakeThread.instances.append(self)

    def start(self):
        pass

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


def _labels_captures():
    """Étiquettes passées à capturer() (2e arg des threads de capture)."""
    return [t.args[1] for t in _FakeThread.instances if len(t.args) >= 2]


def _prepare(monkeypatch):
    _FakeThread.instances = []
    monkeypatch.setattr(tc.threading, "Thread", _FakeThread)
    monkeypatch.setattr(tc, "sauver_config", lambda cfg: None)
    monkeypatch.setattr(tc, "charger_config", lambda: {})
    monkeypatch.setattr(tc, "resoudre_micro", lambda n: _FakeDevice("MicroTest"))
    monkeypatch.setattr(tc, "resoudre_loopback", lambda n: _FakeDevice("LoopbackTest"))
    monkeypatch.setattr(tc, "loopback_defaut", lambda: _FakeDevice("LoopbackTest"))
    monkeypatch.setattr(tc, "micro_defaut", lambda: _FakeDevice("MicroTest"))


def test_begin_consultation_mode_cabinet(monkeypatch):
    _prepare(monkeypatch)
    api = tc.Api()
    res = api.start("MicroTest", "SortieTest", mode="cabinet")
    assert res["ok"] and res["mode"] == "cabinet"
    # Micro seul : une unique capture, étiquetée « Conversation ».
    assert _labels_captures() == ["Conversation"]


def test_begin_consultation_mode_tele(monkeypatch):
    _prepare(monkeypatch)
    api = tc.Api()
    res = api.start("MicroTest", "SortieTest", mode="tele")
    assert res["ok"] and res["mode"] == "tele"
    # Deux captures : Patient (loopback) + Medecin (micro).
    assert set(_labels_captures()) == {"Patient", "Medecin"}


def test_mode_cabinet_sans_micro(monkeypatch):
    _prepare(monkeypatch)
    monkeypatch.setattr(tc, "resoudre_micro", lambda n: None)
    monkeypatch.setattr(tc, "micro_defaut", lambda: None)
    api = tc.Api()
    res = api.start("", "", mode="cabinet")
    assert not res["ok"]
    assert "micro" in res["error"].lower()


# ------------------------------------------------ arrêt immédiat de la capture

def test_arreter_capture_fige_et_purge(monkeypatch):
    """arreter_capture : stop_event levé, audio « en vol » purgé, segments déjà
    transcrits intégrés au transcript figé."""
    tc.stop_event.clear()
    api = tc.Api()
    api._threads = [_FakeThread()]   # threads factices (is_alive=False)

    # Segment brut non transcrit (doit être purgé) + segment déjà transcrit.
    tc.segment_queue.put(("Conversation", b"audio_brut"))
    tc.display_queue.put(("Conversation", "phrase déjà transcrite", 1))

    items = api.arreter_capture()

    assert tc.stop_event.is_set()                 # capture figée
    assert api._started is False
    # Audio brut purgé (ne produira jamais de texte).
    assert tc.segment_queue.qsize() == 0
    # Segment transcrit intégré au transcript.
    assert any(t == "phrase déjà transcrite" for _, _, t in api._entries)
    assert any(it["texte"] == "phrase déjà transcrite" for it in items)
    tc.stop_event.clear()


def test_arreter_capture_aucun_texte_apres(monkeypatch):
    """Après arreter_capture, un segment brut arrivé « après » n'ajoute rien."""
    tc.stop_event.clear()
    api = tc.Api()
    api._threads = []
    api.arreter_capture()
    n = len(api._entries)
    # Un segment audio brut post-arrêt ne doit pas devenir du texte tant que la
    # capture est arrêtée (il resterait dans la file, jamais transcrit).
    tc.segment_queue.put(("Conversation", b"bruit_apres"))
    # get_updates ne draine que display_queue (texte), pas l'audio brut.
    api.get_updates()
    assert len(api._entries) == n
    # Nettoyage
    while not tc.segment_queue.empty():
        tc.segment_queue.get_nowait()
    tc.stop_event.clear()


# ------------------------------------------------ mise à jour spécialité

def test_maj_specialite_local_et_backend(monkeypatch, tmp_path):
    cfg_store = {"medecin_id": "med-123", "specialty": "Cardiologie"}
    monkeypatch.setattr(tc, "charger_config", lambda: dict(cfg_store))
    saved = {}
    monkeypatch.setattr(tc, "sauver_config", lambda c: cfg_store.update(c))
    appels = []
    monkeypatch.setattr(tc, "_appel_api",
                        lambda ep, payload, **kw: appels.append((ep, payload)) or {"ok": True})

    api = tc.Api()
    res = api.maj_specialite("Dermatologie")
    assert res["ok"]
    assert cfg_store["specialty"] == "Dermatologie"          # persistance locale
    assert appels == [("maj-specialite",
                       {"medecin_id": "med-123", "specialite": "Dermatologie"})]


def test_save_settings_specialite_backend(monkeypatch):
    cfg_store = {"medecin_id": "med-9", "specialty": "Pédiatrie"}
    monkeypatch.setattr(tc, "charger_config", lambda: dict(cfg_store))
    monkeypatch.setattr(tc, "sauver_config", lambda c: cfg_store.update(c))
    monkeypatch.setattr(tc, "appliquer_titlebar_theme", lambda t: None)
    appels = []
    monkeypatch.setattr(tc, "_appel_api",
                        lambda ep, payload, **kw: appels.append((ep, payload)) or {"ok": True})
    api = tc.Api()
    # Spécialité inchangée → pas d'appel backend.
    api.save_settings({"specialty": "Pédiatrie"})
    assert appels == []
    # Spécialité modifiée → appel backend.
    api.save_settings({"specialty": "Neurologie"})
    assert appels == [("maj-specialite",
                       {"medecin_id": "med-9", "specialite": "Neurologie"})]
    assert cfg_store["specialty"] == "Neurologie"
