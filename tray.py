# -*- coding: utf-8 -*-
"""
tray.py — Icône Écho dans la barre système (system tray).

pystray + Pillow, dans un thread daemon parallèle à pywebview (sans le bloquer).
Trois états visuels (variantes du logo « onde vocale » d'Écho) :
    vert  : prêt
    rouge : consultation en cours
    gris  : hors ligne / licence expirée

Menu (clic droit) : Nouvelle consultation · Ouvrir Écho · Quitter Écho.
Double-clic : Ouvrir Écho.

Tout est fail-safe : si pystray/Pillow manquent ou échouent, l'app fonctionne
normalement sans icône (jamais de plantage).
"""

import threading

# Barres du logo « onde vocale » : (x, y, largeur, hauteur) sur une base 125x120.
_BARRES = [
    (10, 47, 9, 26), (26, 37, 9, 46), (42, 27, 9, 66), (58, 17, 9, 86),
    (74, 27, 9, 66), (90, 37, 9, 46), (106, 47, 9, 26),
]
_COULEURS = {
    "vert": (52, 216, 153, 255),    # #34D899 — prêt
    "rouge": (255, 80, 80, 255),    # #FF5050 — consultation en cours
    "gris": (150, 160, 156, 255),   # hors ligne / licence expirée
}


def _image_icone(etat):
    """Construit l'image de l'icône (64x64) pour l'état donné."""
    from PIL import Image, ImageDraw
    couleur = _COULEURS.get(etat, _COULEURS["vert"])
    taille = 64
    img = Image.new("RGBA", (taille, taille), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ech = taille / 125.0
    for (x, y, w, h) in _BARRES:
        x0, y0 = x * ech, y * ech
        x1, y1 = (x + w) * ech, (y + h) * ech
        r = (w * ech) / 2.0
        d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=couleur)
    return img


class EchoTray:
    """Gère l'icône tray. Les callbacks (ouvrir/nouvelle/quitter) sont fournis
    par l'appelant et exécutés dans le thread pystray."""

    def __init__(self, on_open, on_new, on_quit):
        self._on_open = on_open
        self._on_new = on_new
        self._on_quit = on_quit
        self._icon = None
        self._thread = None
        self._etat = "vert"

    def demarrer(self):
        """Lance l'icône dans un thread daemon. Renvoie True si OK."""
        try:
            import pystray
        except Exception:
            return False
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Ouvrir Écho", self._appeler(self._on_open),
                                 default=True),          # double-clic = ouvrir
                pystray.MenuItem("+ Nouvelle consultation",
                                 self._appeler(self._on_new)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quitter Écho", self._appeler(self._on_quit)),
            )
            self._icon = pystray.Icon(
                "echo", icon=_image_icone(self._etat),
                title="Écho", menu=menu)
            self._thread = threading.Thread(target=self._icon.run, daemon=True)
            self._thread.start()
            return True
        except Exception:
            # Sans tray, « X » minimise vers rien : l'app semble disparue.
            from journal_erreurs import journaliser
            journaliser("tray.demarrer: icône barre système impossible")
            self._icon = None
            return False

    def _appeler(self, fn):
        def handler(icon=None, item=None):
            try:
                fn()
            except Exception:
                pass
        return handler

    def set_etat(self, etat):
        """Change la couleur de l'icône (vert / rouge / gris). Best-effort."""
        if etat == self._etat or not self._icon:
            return
        self._etat = etat
        try:
            self._icon.icon = _image_icone(etat)
        except Exception:
            pass

    def notifier(self, message, titre="Écho"):
        """Notification système (bulle). Best-effort."""
        if not self._icon:
            return
        try:
            self._icon.notify(message, titre)
        except Exception:
            pass

    def arreter(self):
        """Retire l'icône de la barre système."""
        try:
            if self._icon:
                self._icon.stop()
        except Exception:
            pass
        self._icon = None
