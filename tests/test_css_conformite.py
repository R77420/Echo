# -*- coding: utf-8 -*-
"""
Garde anti-régression CSS : tout élément FLOTTANT (modale, panneau,
dropdown, bulle, menu) doit avoir un fond opaque — var(--bg-float).

Bug récurrent verrouillé ici (3 occurrences : .profile-panel,
.confirm-box/.mode-card, .tour-bubble) : var(--bg-card) vaut
rgba(21,41,35,0.25) en thème sombre — on lit l'interface à travers.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "ui", "style.css")

# Sélecteurs considérés comme flottants.
_FLOTTANT = re.compile(
    r"panel|modal|popover|tooltip|dropdown|bubble|coach|confirm|float|menu|-list$",
    re.IGNORECASE)
# Exception : les BACKDROPS (voile assombri derrière une modale) sont
# volontairement translucides — ce sont des fonds, pas des surfaces de lecture.
_BACKDROP = re.compile(r"overlay$", re.IGNORECASE)

# Fonds interdits sur un flottant : la variable semi-transparente, un rgba()
# d'alpha < 0.9, ou un hex 8 chiffres d'alpha < 0.9 (ex. #15292340).
_BG_CARD = re.compile(r"background(?:-color)?\s*:\s*var\(--bg-card\)")
_RGBA    = re.compile(r"background(?:-color)?\s*:\s*rgba\([^)]*,\s*(0?\.\d+|0|1(?:\.0+)?)\s*\)")
_HEX8    = re.compile(r"background(?:-color)?\s*:\s*#[0-9a-f]{6}([0-9a-f]{2})\b", re.IGNORECASE)


def _regles(css):
    """Itère (selecteur, corps) — parser minimal suffisant pour style.css
    (pas de blocs imbriqués hors @media/@keyframes, qu'on aplatit)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        sel = m.group(1).strip()
        if sel.startswith("@"):        # en-tête @media/@keyframes sans corps utile
            continue
        yield sel, m.group(2)


def test_flottants_fond_opaque():
    with open(_CSS, encoding="utf-8") as f:
        css = f.read()
    fautes = []
    for sel, corps in _regles(css):
        # Un bloc peut avoir plusieurs sélecteurs ; il suffit qu'un matche.
        noms = [s.strip() for s in sel.split(",")]
        def _cible(n):
            # dernier segment du sélecteur, sans pseudo-classe (:hover…)
            mots = n.split(":")[0].split()
            return mots[-1] if mots else ""
        cibles = [_cible(n) for n in noms if _cible(n)]
        if not any(_FLOTTANT.search(c) and not _BACKDROP.search(c) for c in cibles):
            continue
        if _BG_CARD.search(corps):
            fautes.append((sel, "var(--bg-card)"))
            continue
        m = _RGBA.search(corps)
        if m and float(m.group(1)) < 0.9:
            fautes.append((sel, "rgba alpha=%s" % m.group(1)))
            continue
        m = _HEX8.search(corps)
        if m and int(m.group(1), 16) / 255 < 0.9:
            fautes.append((sel, "hex8 alpha faible"))
    assert not fautes, "\n".join(
        "Élément flottant %s avec fond non opaque (%s) — utiliser "
        "var(--bg-float). Voir CLAUDE.md." % (s, raison)
        for s, raison in fautes)


def test_bg_float_definie_dans_les_deux_themes():
    """--bg-float doit exister en clair ET en sombre (et rester opaque)."""
    with open(_CSS, encoding="utf-8") as f:
        css = f.read()
    valeurs = re.findall(r"--bg-float:\s*(#[0-9a-fA-F]{6})\s*;", css)
    assert len(valeurs) >= 2, "--bg-float doit être définie (clair + sombre)"
    assert "#FFFFFF" in [v.upper() for v in valeurs]
    assert "#1A2E28" in [v.upper() for v in valeurs]
