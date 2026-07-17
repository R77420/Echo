# -*- coding: utf-8 -*-
"""
Garde anti-régression sur les textes de confidentialité : depuis le pivot
Groq, l'app ne peut plus prétendre « 100 % local » — l'audio transite par
une infrastructure externe. Ce test éradique la classe entière d'erreurs
(un texte obsolète qui survit à un refactor).
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Affirmations devenues fausses (l'audio part vers Groq). Les mentions
# légitimes du « modèle de secours hors-ligne » (vraie fonctionnalité)
# ne matchent pas ces motifs.
_INTERDITS = [
    r"100\s*(?:&nbsp;)?%\s*(?:&nbsp;)?\s*local",
    r"100\s*(?:&nbsp;)?%\s*(?:&nbsp;)?\s*confidentiel",
    r"aucune donn[ée]e ne quitte",
    r"ne quitte (?:pas|jamais) (?:votre|le) (?:poste|ordinateur)",
]

_FICHIERS = []
for dossier, exts in (("ui", (".html", ".js", ".css")), (".", (".iss",))):
    base = os.path.join(_RACINE, dossier)
    for f in os.listdir(base):
        if f.endswith(exts):
            _FICHIERS.append(os.path.join(base, f))
_FICHIERS.append(os.path.join(_RACINE, "README.md"))
_FICHIERS.append(os.path.join(_RACINE, "transcription_consultation.py"))


def test_pas_de_texte_100_local():
    trouves = []
    for chemin in _FICHIERS:
        with open(chemin, encoding="utf-8", errors="replace") as f:
            for i, ligne in enumerate(f, 1):
                for motif in _INTERDITS:
                    if re.search(motif, ligne, re.IGNORECASE):
                        trouves.append("%s:%d: %s"
                                       % (os.path.basename(chemin), i, ligne.strip()))
    assert not trouves, "Textes obsolètes (l'app n'est plus 100% locale) :\n" \
        + "\n".join(trouves)


# --------------------------------------------------- onboarding sans nom

def _html():
    with open(os.path.join(_RACINE, "ui", "main_window.html"),
              encoding="utf-8") as f:
        return f.read()


def test_onboarding_sans_etape_nom():
    """L'étape « Quel est votre nom ? » a disparu : le nom vient de
    l'inscription, l'écran de bienvenue enchaîne directement sur le thème."""
    html = _html()
    assert 'id="ob-name"' not in html
    assert "Quel est votre nom" not in html
    # Le bouton Continuer de la bienvenue mène au choix du thème.
    m = re.search(r"\$\('ob-next'\)\.addEventListener\('click',.*?\}\);",
                  html, re.DOTALL)
    assert m and "onboarding-theme" in m.group(0)


def test_complete_onboarding_nom_vide_conserve(monkeypatch):
    """complete_onboarding('') conserve le nom enregistré à l'inscription."""
    import transcription_consultation as tc
    store = {"doctor_name": "Dr Moussa"}
    monkeypatch.setattr(tc, "charger_config", lambda: dict(store))
    monkeypatch.setattr(tc, "sauver_config", lambda c: store.update(c))
    api = tc.Api()
    res = api.complete_onboarding("", "Micro X", "Sortie Y")
    assert res["ok"] is True
    assert store["doctor_name"] == "Dr Moussa"     # inchangé
    assert store["micro"] == "Micro X"
    # Aucun nom nulle part (cas anormal) → erreur propre, pas de config cassée.
    store.clear()
    assert api.complete_onboarding("", "m", "s")["ok"] is False
