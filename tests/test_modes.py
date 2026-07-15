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


# ------------------------------------------------ calibrage adaptatif RMS cabinet

import numpy as np

class _FakeRec:
    """Recorder factice : renvoie des blocs à un RMS constant donné."""
    def __init__(self, rms_cible):
        self.rms = rms_cible
    def record(self, numframes):
        # signal constant dont le RMS vaut exactement self.rms
        return np.full((numframes, 1), self.rms, dtype=np.float32)


def test_calibrage_cabinet_calme(monkeypatch):
    tc.stop_event.clear()
    # Cabinet calme : bruit ambiant très faible → seuil = plancher.
    seuil = tc._calibrer_seuil_cabinet(_FakeRec(0.001), tc.RMS_MIN_CABINET)
    assert seuil == tc.CABINET_RMS_FLOOR


def test_calibrage_cabinet_moyen(monkeypatch):
    tc.stop_event.clear()
    # Bruit ambiant 0.005 → seuil = 0.005 × 2.5 = 0.0125 (entre plancher/plafond).
    seuil = tc._calibrer_seuil_cabinet(_FakeRec(0.005), tc.RMS_MIN_CABINET)
    assert abs(seuil - 0.005 * tc.CABINET_RMS_FACTOR) < 1e-6
    assert tc.CABINET_RMS_FLOOR <= seuil <= tc.CABINET_RMS_CEIL


def test_calibrage_cabinet_bruyant(monkeypatch):
    tc.stop_event.clear()
    # Cabinet bruyant : bruit ambiant élevé → seuil plafonné.
    seuil = tc._calibrer_seuil_cabinet(_FakeRec(0.05), tc.RMS_MIN_CABINET)
    assert seuil == tc.CABINET_RMS_CEIL


# ------------------------------------------------ filtre de confiance no_speech

def test_no_speech_seuil_defini():
    # Backstop conservateur : bien au-dessus de la parole (~0.002) pour ne pas
    # rejeter la voix distante, tout en coupant le non-parole franc.
    assert 0.4 <= tc.NO_SPEECH_MAX <= 0.75
    assert tc.NO_SPEECH_MAX > 0.002      # parole toujours conservée


def test_no_speech_filtre_worker_logique():
    # Vérifie la décision de rejet : no_speech au-dessus du seuil → rejeté.
    for nsp, rejete in [(0.002, False), (0.3, False), (0.7, True), (0.95, True)]:
        assert (nsp is not None and nsp > tc.NO_SPEECH_MAX) == rejete


# ------------------------------------------------ enchaînement « Patient suivant »

def test_enchainement_reinitialise(monkeypatch):
    """2 consultations enchaînées : la 2e ne contient RIEN de la 1re
    (transcript, contexte dynamique, infos patient)."""
    _prepare(monkeypatch)
    tc.stop_event.clear()
    api = tc.Api()

    # Consultation 1 (cabinet) + transcript + contexte + patient.
    assert api.begin_consultation("cabinet", "MicroTest", "SortieTest")["ok"]
    with api._lock:
        api._entries.append(("10:00:00", "Conversation", "Patient un : douleurs au dos"))
    tc._maj_contexte("douleurs au dos patient un")
    api._infos = {"nom": "PATIENT_UN"}
    tc.segment_queue.put(("Conversation", b"reste1"))
    tc._correction_queue.put((1, "reste1", ""))

    # Fin de la consultation 1 (comme le vrai flux), puis Patient suivant.
    api.end_consultation()
    assert api.patient_suivant()["ok"]

    # La consultation 2 démarre vierge.
    assert api._entries == []
    assert tc._dernier_contexte == ""
    assert api._infos is None
    assert tc.segment_queue.qsize() == 0
    assert tc._correction_queue.qsize() == 0
    tc.stop_event.clear()


def test_mode_conserve_enchainement(monkeypatch):
    """Mode cabinet → Patient suivant → toujours cabinet (micro seul)."""
    _prepare(monkeypatch)
    tc.stop_event.clear()
    api = tc.Api()
    api.begin_consultation("cabinet", "MicroTest", "SortieTest")
    assert api._mode == "cabinet"
    api.end_consultation()

    _FakeThread.instances = []          # observer les threads de la 2e consultation
    r = api.patient_suivant()
    assert r["ok"] and r.get("mode") == "cabinet"
    assert api._mode == "cabinet"
    assert _labels_captures() == ["Conversation"]   # micro seul, mode conservé
    tc.stop_event.clear()


def test_patient_suivant_echec_device(monkeypatch):
    """Démarrage impossible (aucun micro) → {ok:false} sans exception
    (le JS retombe proprement sur l'accueil)."""
    _prepare(monkeypatch)
    monkeypatch.setattr(tc, "resoudre_micro", lambda n: None)
    monkeypatch.setattr(tc, "micro_defaut", lambda: None)
    tc.stop_event.clear()
    api = tc.Api()
    api._mode = "cabinet"
    api._started = False
    r = api.patient_suivant()
    assert not r["ok"] and "error" in r
    tc.stop_event.clear()
