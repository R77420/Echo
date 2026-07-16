# -*- coding: utf-8 -*-
"""
demarrage.py — Lancement d'Écho au démarrage de Windows.

Écrit / lit / supprime la clé de registre HKCU\\...\\Run (aucun droit admin
requis, HKCU uniquement). L'exe est lancé avec l'argument --tray → Écho démarre
directement minimisé dans la barre système.

Toutes les fonctions sont fail-safe : une erreur registre (politique
d'entreprise, antivirus) renvoie {ok: False, error} sans jamais planter.
"""

import os
import sys

try:
    import winreg
except Exception:                       # non-Windows (tests CI, dev)
    winreg = None

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Echo"


def commande_demarrage():
    """Commande à inscrire dans le registre : « "<exe>" --tray ».
    En mode gelé (PyInstaller) : l'exe Écho. En dev : python + script."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return '"%s" --tray' % exe
    # Dev : python <script> --tray (permet de tester le mécanisme).
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else __file__
    return '"%s" "%s" --tray' % (sys.executable, script)


def activer_demarrage():
    """Inscrit Écho au démarrage de Windows. Renvoie {ok, error?}."""
    if winreg is None:
        return {"ok": False, "error": "Registre indisponible sur ce système."}
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.SetValueEx(k, _VALUE_NAME, 0, winreg.REG_SZ,
                              commande_demarrage())
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def desactiver_demarrage():
    """Retire Écho du démarrage. Absence de clé = déjà désactivé (ok)."""
    if winreg is None:
        return {"ok": False, "error": "Registre indisponible sur ce système."}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, _VALUE_NAME)
            except FileNotFoundError:
                pass                    # déjà absent → ok
        return {"ok": True}
    except FileNotFoundError:
        return {"ok": True}             # la clé Run elle-même n'existe pas
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def demarrage_actif():
    """Vrai si la valeur Echo existe dans HKCU\\...\\Run (état RÉEL du registre,
    pas une valeur mémorisée en config)."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as k:
            valeur, _ = winreg.QueryValueEx(k, _VALUE_NAME)
            return bool(valeur)
    except FileNotFoundError:
        return False
    except Exception:
        return False
