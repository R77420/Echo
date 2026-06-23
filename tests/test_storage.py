"""Tests du module storage.py (persistance JSON + génération .docx).

Tous les tests utilisent tmp_path : aucun vrai fichier n'est touché.
Lancer : pytest tests/test_storage.py -v
"""

import datetime
import os
import sys

import pytest

# Permet d'importer storage.py situé à la racine du projet.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import storage  # noqa: E402


# --------------------------------------------------------------------------- #
#  Fixtures / helpers
# --------------------------------------------------------------------------- #

def _chemin(tmp_path):
    return os.path.join(tmp_path, "consultations.json")


def _record(cid="a1", nom="DUPONT", file_path="", summary=""):
    return {
        "id": cid,
        "date": "2026-06-13T09:00:00",
        "patient": {"nom": nom, "prenom": "Marie",
                    "naissance": "12/03/1980", "motif": "Toux"},
        "summary": summary,
        "file_path": file_path,
        "duration_min": 12,
    }


# --------------------------------------------------------------------------- #
#  JSON : écriture / lecture
# --------------------------------------------------------------------------- #

def test_ecrire_lire_consultation(tmp_path):
    chemin = _chemin(tmp_path)
    storage.ajouter_consultation(chemin, _record("a1", "DUPONT"))
    storage.ajouter_consultation(chemin, _record("b2", "MARTIN"))

    data = storage.charger_consultations(chemin)
    assert len(data) == 2
    # ajouter_consultation insère en tête → b2 d'abord.
    assert data[0]["id"] == "b2"
    assert data[1]["id"] == "a1"
    assert data[1]["patient"]["nom"] == "DUPONT"
    assert data[0]["patient"]["motif"] == "Toux"


def test_charger_consultations_absent(tmp_path):
    # Fichier inexistant → liste vide, pas d'erreur.
    assert storage.charger_consultations(_chemin(tmp_path)) == []


# --------------------------------------------------------------------------- #
#  Suppression
# --------------------------------------------------------------------------- #

def test_supprimer_consultation_historique_seul(tmp_path):
    chemin = _chemin(tmp_path)
    # Crée un faux .docx sur le disque.
    docx = os.path.join(tmp_path, "cr.docx")
    with open(docx, "w", encoding="utf-8") as f:
        f.write("contenu word")

    storage.ajouter_consultation(chemin, _record("a1", file_path=docx))
    storage.ajouter_consultation(chemin, _record("b2"))

    rec = storage.supprimer_consultation(chemin, "a1")

    assert rec is not None and rec["id"] == "a1"
    # L'entrée json a disparu...
    ids = [c["id"] for c in storage.charger_consultations(chemin)]
    assert ids == ["b2"]
    # ...mais le .docx reste sur le disque.
    assert os.path.isfile(docx)


def test_supprimer_consultation_definitif(tmp_path):
    chemin = _chemin(tmp_path)
    docx = os.path.join(tmp_path, "cr.docx")
    with open(docx, "w", encoding="utf-8") as f:
        f.write("contenu word")

    storage.ajouter_consultation(chemin, _record("a1", file_path=docx))

    rec = storage.supprimer_consultation_avec_fichier(chemin, "a1")

    assert rec is not None and rec["id"] == "a1"
    # .docx ET entrée json ont disparu.
    assert storage.charger_consultations(chemin) == []
    assert not os.path.exists(docx)


def test_suppression_definitive_fichier_absent(tmp_path):
    # Le .docx a déjà été déplacé/supprimé manuellement → pas d'erreur,
    # l'entrée est quand même retirée.
    chemin = _chemin(tmp_path)
    storage.ajouter_consultation(
        chemin, _record("a1", file_path=os.path.join(tmp_path, "introuvable.docx")))

    rec = storage.supprimer_consultation_avec_fichier(chemin, "a1")
    assert rec is not None
    assert storage.charger_consultations(chemin) == []


def test_suppression_id_inexistant(tmp_path):
    chemin = _chemin(tmp_path)
    storage.ajouter_consultation(chemin, _record("a1"))

    # Ne doit pas lever, renvoie None, et ne touche pas aux entrées existantes.
    rec = storage.supprimer_consultation(chemin, "zzz")
    assert rec is None
    assert len(storage.charger_consultations(chemin)) == 1


# --------------------------------------------------------------------------- #
#  Mise à jour du résumé
# --------------------------------------------------------------------------- #

def test_maj_resume(tmp_path):
    chemin = _chemin(tmp_path)
    storage.ajouter_consultation(chemin, _record("a1", summary=""))
    storage.ajouter_consultation(chemin, _record("b2", summary="ancien"))

    storage.maj_consultation_resume(chemin, "a1", "Nouveau résumé patient")

    data = {c["id"]: c for c in storage.charger_consultations(chemin)}
    # Le résumé ciblé est mis à jour...
    assert data["a1"]["summary"] == "Nouveau résumé patient"
    # ...les autres champs de a1 restent intacts...
    assert data["a1"]["patient"]["nom"] == "DUPONT"
    assert data["a1"]["duration_min"] == 12
    # ...et les autres entrées ne bougent pas.
    assert data["b2"]["summary"] == "ancien"


def test_maj_resume_id_inexistant(tmp_path):
    chemin = _chemin(tmp_path)
    storage.ajouter_consultation(chemin, _record("a1", summary="x"))
    # id inconnu → pas d'erreur, rien ne change.
    storage.maj_consultation_resume(chemin, "zzz", "ignoré")
    assert storage.charger_consultations(chemin)[0]["summary"] == "x"


# --------------------------------------------------------------------------- #
#  Génération .docx
# --------------------------------------------------------------------------- #

def test_generation_docx_champs_optionnels(tmp_path):
    pytest.importorskip("docx")  # python-docx requis
    from docx import Document

    chemin = os.path.join(tmp_path, "cr.docx")
    now = datetime.datetime(2026, 6, 13, 9, 0)
    infos = {"nom": "DUPONT", "prenom": "", "naissance": "", "motif": ""}
    entries = [("09:00:00", "Medecin", "Bonjour"),
               ("09:00:05", "Patient", "Bonjour docteur")]

    # Ne doit pas crasher avec seulement le Nom.
    storage.ecrire_docx(chemin, infos, now, None, entries, annexes=[])
    assert os.path.isfile(chemin)

    doc = Document(chemin)
    texte_complet = "\n".join(p.text for p in doc.paragraphs)
    cellules = [c.text for t in doc.tables for r in t.rows for c in r.cells]

    # Le Nom est présent ; prénom/né(e) le omis (vides) ; Motif = "—".
    assert "DUPONT" in cellules
    assert "Prénom" not in cellules
    assert "Né(e) le" not in cellules
    assert "—" in cellules                      # motif vide → tiret
    assert "Compte-rendu de consultation" in texte_complet
    # La transcription intégrale figure bien.
    assert "Transcription intégrale" in texte_complet


def test_generation_docx_complet(tmp_path):
    pytest.importorskip("docx")
    from docx import Document

    chemin = os.path.join(tmp_path, "cr.docx")
    now = datetime.datetime(2026, 6, 13, 9, 0)
    infos = {"nom": "MARTIN", "prenom": "Paul",
             "naissance": "05/09/1975", "motif": "Suivi"}
    entries = [("10:00:00", "Patient", "J'ai mal à la tête")]

    storage.ecrire_docx(chemin, infos, now, None, entries)
    cellules = [c.text for t in Document(chemin).tables for r in t.rows for c in r.cells]
    assert "MARTIN" in cellules
    assert "Paul" in cellules
    assert "05/09/1975" in cellules
    assert "Suivi" in cellules


# --------------------------------------------------------------------------- #
#  Retry sur verrou Windows
# --------------------------------------------------------------------------- #

def test_retry_sur_lock(tmp_path, monkeypatch):
    """Simule un PermissionError aux 2 premières ouvertures en écriture,
    puis un succès → le retry doit aboutir."""
    chemin = _chemin(tmp_path)
    storage.ajouter_consultation(chemin, _record("a1"))
    storage.ajouter_consultation(chemin, _record("b2"))

    vrai_open = open
    etat = {"echecs_restants": 2}

    def open_capricieux(file, mode="r", *args, **kwargs):
        # Ne fait échouer que les ouvertures en écriture du fichier ciblé.
        if str(file) == chemin and "w" in mode and etat["echecs_restants"] > 0:
            etat["echecs_restants"] -= 1
            raise PermissionError("verrou simulé (OneDrive)")
        return vrai_open(file, mode, *args, **kwargs)

    # sleep neutralisé pour un test rapide.
    monkeypatch.setattr(storage.time, "sleep", lambda s: None)
    monkeypatch.setattr("builtins.open", open_capricieux)

    rec = storage.supprimer_consultation(chemin, "a1")

    # Les deux premiers essais ont échoué, le 3e a réussi.
    assert etat["echecs_restants"] == 0
    assert rec is not None and rec["id"] == "a1"

    # Restaure open pour la vérification finale.
    monkeypatch.undo()
    assert [c["id"] for c in storage.charger_consultations(chemin)] == ["b2"]


def test_retry_echoue_apres_3_tentatives(tmp_path, monkeypatch):
    """Si le verrou persiste, l'exception est propagée après 3 tentatives."""
    chemin = _chemin(tmp_path)
    storage.ajouter_consultation(chemin, _record("a1"))

    vrai_open = open

    def open_toujours_bloque(file, mode="r", *args, **kwargs):
        if str(file) == chemin and "w" in mode:
            raise PermissionError("verrou permanent")
        return vrai_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(storage.time, "sleep", lambda s: None)
    monkeypatch.setattr("builtins.open", open_toujours_bloque)

    with pytest.raises(PermissionError):
        storage.supprimer_consultation(chemin, "a1")
