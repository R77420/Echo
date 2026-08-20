# -*- coding: utf-8 -*-
"""Journal central des erreurs (%APPDATA%\\Echo\\erreurs.log) : toute panne
tolérée doit rester visible — né des deux bugs avalés par except:pass
(message d'inscription menteur, auto-update mort)."""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import journal_erreurs


def _dans(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return os.path.join(str(tmp_path), "Echo", "erreurs.log")


def test_journalise_avec_traceback(tmp_path, monkeypatch):
    log = _dans(tmp_path, monkeypatch)
    try:
        raise ValueError("boum de test")
    except ValueError:
        journal_erreurs.journaliser("contexte de test")
    contenu = open(log, encoding="utf-8").read()
    assert "contexte de test" in contenu
    assert "ValueError: boum de test" in contenu       # traceback complet
    assert "Traceback" in contenu


def test_journalise_sans_exception(tmp_path, monkeypatch):
    log = _dans(tmp_path, monkeypatch)
    journal_erreurs.journaliser("simple note")
    assert "simple note" in open(log, encoding="utf-8").read()


def test_journaliser_ne_leve_jamais(tmp_path, monkeypatch):
    # APPDATA pointant sur un FICHIER (mkdir impossible) → aucun crash
    # (le journal est best-effort assumé).
    fichier = tmp_path / "pas_un_dossier"
    fichier.write_text("x")
    monkeypatch.setenv("APPDATA", str(fichier))
    journal_erreurs.journaliser("ne doit pas lever")


def test_hook_thread_capture(tmp_path, monkeypatch):
    """Une exception NON attrapée dans un thread finit dans le journal."""
    log = _dans(tmp_path, monkeypatch)
    ancien = threading.excepthook
    try:
        journal_erreurs.installer_hooks()
        # Neutraliser l'affichage stderr par le hook par défaut du test.
        t = threading.Thread(target=lambda: 1 / 0, name="thread-test")
        t.start(); t.join()
        contenu = open(log, encoding="utf-8").read()
        assert "thread-test" in contenu
        assert "ZeroDivisionError" in contenu
    finally:
        threading.excepthook = ancien


def test_rotation(tmp_path, monkeypatch):
    log = _dans(tmp_path, monkeypatch)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "w", encoding="utf-8") as f:
        f.write("x" * (journal_erreurs._MAX_OCTETS + 1))
    journal_erreurs.journaliser("après rotation")
    assert os.path.exists(log + ".1")                  # ancien tronçon conservé
    assert "après rotation" in open(log, encoding="utf-8").read()
