# -*- coding: utf-8 -*-
"""
Tests de la prise en main guidée : la consultation de démonstration
ne pollue jamais les données réelles (stats, patients), les flags de
visite sont mémorisés, et la démo est supprimable.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage
import transcription_consultation as tc


def _consult(cid, demo=False, nom="DUBOIS", date=None, dur=8):
    return {"id": cid, "date": (date or datetime.datetime.now().isoformat()),
            "patient": {"nom": nom, "prenom": ""}, "summary": "",
            "file_path": "", "duration_min": dur, "cr_valide": True,
            "cr_elements": None, "entries": [], "annexes": [],
            "demo": demo}


def _sandbox(monkeypatch, tmp_path, consultations):
    chemin = str(tmp_path / "consultations.json")
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(consultations, f)
    monkeypatch.setattr(tc, "chemin_consultations", lambda: chemin)
    monkeypatch.setattr(tc, "chemin_patients",
                        lambda: str(tmp_path / "patients.json"))
    return chemin


# ------------------------------------------------------------ exclusions

def test_demo_exclue_stats(monkeypatch, tmp_path):
    """Une consultation demo=true n'entre ni dans le compteur du mois,
    ni dans la durée moyenne, ni dans les patients suivis."""
    _sandbox(monkeypatch, tmp_path, [
        _consult("c1", demo=False, nom="MARTIN", dur=10),
        _consult("d1", demo=True, nom="DUBOIS", dur=99),
    ])
    api = tc.Api()
    s = api.get_stats()
    assert s["mois"] == 1
    assert s["semaine"] == 1
    assert s["patients"] == 1          # MARTIN seul
    assert s["duree_moy"] == 10        # la durée 99 de la démo est ignorée


def test_demo_patient_masque():
    """Le « patient » DUBOIS créé par la démo n'apparaît pas dans la vue
    Patients (extraire_patients)."""
    pats = storage.extraire_patients([
        _consult("c1", demo=False, nom="MARTIN"),
        _consult("d1", demo=True, nom="DUBOIS"),
    ])
    noms = [p["nom"] for p in pats]
    assert "MARTIN" in noms
    assert "DUBOIS" not in noms


# ------------------------------------------------------------ flags visite

def test_visite_flag(monkeypatch):
    """Visite marquée faite → get_decouverte le reflète : la visite ne se
    relance pas au prochain lancement (le JS lit ce flag)."""
    store = {}
    monkeypatch.setattr(tc, "charger_config", lambda: dict(store))
    monkeypatch.setattr(tc, "sauver_config", lambda c: store.update(c))
    api = tc.Api()
    assert api.get_decouverte()["visite_faite"] is False
    assert api.marquer_decouverte("visite_faite")["ok"] is True
    assert api.get_decouverte()["visite_faite"] is True
    # Flag inconnu refusé.
    assert api.marquer_decouverte("autre_chose")["ok"] is False


# ------------------------------------------------------------ suppression

def test_demo_supprimable(monkeypatch, tmp_path):
    """Supprimer la démo retire la consultation ET le patient DUBOIS."""
    chemin = _sandbox(monkeypatch, tmp_path, [
        _consult("c1", demo=False, nom="MARTIN"),
        _consult("d1", demo=True, nom="DUBOIS"),
    ])
    api = tc.Api()
    assert api.supprimer_demo()["ok"] is True
    restants = storage.charger_consultations(chemin)
    assert [c["id"] for c in restants] == ["c1"]
    assert all(p["nom"] != "DUBOIS" for p in storage.extraire_patients(restants))
    # Plus de démo → suppression idempotente refusée proprement.
    assert api.supprimer_demo()["ok"] is False


# ------------------------------------------- la démo n'est jamais une tâche

def _js_est_incomplete():
    """Extrait la VRAIE fonction _estIncomplete de main_window.html."""
    import re
    chemin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "ui", "main_window.html")
    with open(chemin, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"function _estIncomplete\(c\) \{.*?\n\}", html, re.DOTALL)
    assert m, "_estIncomplete introuvable"
    return m.group(0)


def test_demo_pas_a_completer():
    """demo=true sans nom ni CR validé → _estIncomplete() false : pas de
    bandeau, pas de badge, pas de file de rattrapage."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node absent")
    script = _js_est_incomplete() + """
const demo  = {demo:true,  nom_a_saisir:true, cr_valide:false};
const vraie = {demo:false, nom_a_saisir:true, cr_valide:false};
const bandeau = [demo, vraie].filter(_estIncomplete).length;
console.log(JSON.stringify({demo:_estIncomplete(demo),
                            vraie:_estIncomplete(vraie), bandeau}));
"""
    out = subprocess.run([node, "-e", script], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["demo"] is False       # jamais « à compléter »
    assert res["vraie"] is True       # une vraie consultation, si
    assert res["bandeau"] == 1        # le bandeau ne compte pas la démo


def test_demo_suppression_unique():
    """La modale d'une démo n'expose qu'une action de suppression :
    le bouton dédié existe et openDeleteModal bascule selon c.demo."""
    chemin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "ui", "main_window.html")
    with open(chemin, encoding="utf-8") as f:
        html = f.read()
    assert 'id="delete-demo"' in html
    assert "Supprimer la démonstration ?" in html
    assert "patient fictif seront retirés" in html
    # openDeleteModal masque les deux options « vraie consultation » en démo.
    import re
    m = re.search(r"function openDeleteModal.*?\n\}", html, re.DOTALL)
    assert m and "c.demo === true" in m.group(0)
    assert "delete-history-only" in m.group(0)
    assert "delete-permanent" in m.group(0)


def test_suppression_demo_complete(monkeypatch, tmp_path):
    """Supprimer la démo → entrée retirée, fichier .docx supprimé,
    patient fictif disparu."""
    docx = tmp_path / "demo.docx"
    docx.write_bytes(b"contenu")
    rec = _consult("d1", demo=True, nom="DUBOIS")
    rec["file_path"] = str(docx)
    chemin = _sandbox(monkeypatch, tmp_path, [rec])
    api = tc.Api()
    assert api.supprimer_demo()["ok"] is True
    restants = storage.charger_consultations(chemin)
    assert restants == []                                  # entrée retirée
    assert not docx.exists()                               # fichier supprimé
    assert storage.extraire_patients(restants) == []       # patient disparu


# ------------------------------------------------------- verdict micro démo

def test_demo_verdict_micro(monkeypatch):
    """La démo remplace le test micro jamais fait : RMS collectés pendant la
    démo → verdict stocké en config (pas de seconde phrase à lire)."""
    store = {}
    monkeypatch.setattr(tc, "charger_config", lambda: dict(store))
    monkeypatch.setattr(tc, "sauver_config", lambda c: store.update(c))
    api = tc.Api()
    api.set_demo_mode(True)
    tc._demo_capture["rms"] = [0.02, 0.03]
    api._finir_demo([["09:00", "Medecin", "Bonjour Madame Dubois"]])
    assert store["mic_test_verdict"] == "adapte"
    assert store["demo_faite"] is True
    assert api.get_decouverte()["demo_micro"] == "adapte"
    api.set_demo_mode(False)


def test_demo_micro_deja_teste(monkeypatch):
    """Test micro déjà fait → la démo n'écrase pas le verdict existant."""
    store = {"mic_test_date": "2026-07-01", "mic_test_verdict": "faible"}
    monkeypatch.setattr(tc, "charger_config", lambda: dict(store))
    monkeypatch.setattr(tc, "sauver_config", lambda c: store.update(c))
    api = tc.Api()
    api.set_demo_mode(True)
    tc._demo_capture["rms"] = [0.02]
    api._finir_demo([["09:00", "Medecin", "Bonjour"]])
    assert store["mic_test_verdict"] == "faible"   # inchangé
    assert store["demo_faite"] is True
    api.set_demo_mode(False)
