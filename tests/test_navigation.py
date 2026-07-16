# -*- coding: utf-8 -*-
"""
Tests de la pile de navigation (ui/nav.js — logique pure, exécutée sous Node).
Le scénario JS simule les entrées empilées par navigate() ; on vérifie que
goBack() ramène sur la bonne page AVEC son contexte (key).
"""
import json
import os
import shutil
import subprocess

import pytest

_UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node absent")


def _run_js(script):
    """Exécute un scénario Node qui require nav.js et imprime du JSON."""
    full = (
        "const NavStack = require(" + json.dumps(os.path.join(_UI, "nav.js")) + ");\n"
        + script
    )
    out = subprocess.run([_NODE, "-e", full], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def test_retour_depuis_fiche_patient():
    """Consultation ouverte depuis un patient → goBack revient sur CE patient."""
    r = _run_js("""
NavStack.push({view:'patients'});
NavStack.push({view:'patient-detail', key:'DUBOIS Marie'});
// on est maintenant sur la fiche consultation → retour
const prev = NavStack.pop();
console.log(JSON.stringify({view: prev.view, key: prev.key, size: NavStack.size()}));
""")
    assert r["view"] == "patient-detail"
    assert r["key"] == "DUBOIS Marie"
    assert r["size"] == 1          # la liste patients reste dessous


def test_retour_depuis_accueil():
    """Consultation ouverte depuis l'accueil → goBack revient à l'accueil."""
    r = _run_js("""
NavStack.push({view:'home'});
const prev = NavStack.pop();
console.log(JSON.stringify({view: prev.view, size: NavStack.size()}));
""")
    assert r["view"] == "home"
    assert r["size"] == 0


def test_pile_pas_de_doublon():
    """Naviguer 2× vers la même page (view+key) → une seule entrée."""
    r = _run_js("""
NavStack.push({view:'patient-detail', key:'DUBOIS'});
NavStack.push({view:'patient-detail', key:'DUBOIS'});   // doublon ignoré
NavStack.push({view:'patient-detail', key:'MARTIN'});   // autre patient : gardé
console.log(JSON.stringify({size: NavStack.size()}));
""")
    assert r["size"] == 2


def test_pile_vide_fallback():
    """goBack sans historique → null (l'appelant retombe sur l'accueil)."""
    r = _run_js("""
console.log(JSON.stringify({prev: NavStack.pop(), size: NavStack.size()}));
""")
    assert r["prev"] is None
    assert r["size"] == 0


def test_deconnexion_vide_pile():
    """La déconnexion vide la pile — aucun historique ne survit."""
    r = _run_js("""
NavStack.push({view:'patients'});
NavStack.push({view:'patient-detail', key:'DUBOIS'});
NavStack.clear();
console.log(JSON.stringify({size: NavStack.size(), prev: NavStack.pop()}));
""")
    assert r["size"] == 0
    assert r["prev"] is None
