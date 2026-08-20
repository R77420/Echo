# -*- coding: utf-8 -*-
"""
Journal central des erreurs — %APPDATA%\\Echo\\erreurs.log

Né de deux pannes invisibles : le message d'inscription menteur et
l'auto-update mort depuis toujours, tous deux causés par des
« except: pass » qui avalaient la vraie cause. Règle : une erreur
inattendue peut être TOLÉRÉE (l'app continue) mais jamais INVISIBLE.

Usage :
    from journal_erreurs import journaliser
    try:
        ...
    except Exception:
        journaliser("contexte lisible")   # trace l'exception courante
        ... # continuer / fallback

`installer_hooks()` capture aussi toute exception NON attrapée
(thread principal + threads) — plus aucune panne muette.
"""
import datetime
import os
import sys
import threading
import traceback

_MAX_OCTETS = 512 * 1024      # rotation simple : on tronque au-delà de 512 Ko


def _chemin():
    dossier = os.path.join(os.environ.get("APPDATA", ""), "Echo")
    os.makedirs(dossier, exist_ok=True)
    return os.path.join(dossier, "erreurs.log")


def journaliser(contexte, exc_info=None):
    """Ajoute une entrée horodatée avec le traceback de l'exception courante
    (ou de `exc_info` si fourni). Ne lève JAMAIS — le journal ne doit pas
    créer de panne à son tour."""
    try:
        chemin = _chemin()
        # Rotation grossière : repartir de zéro si le fichier enfle.
        try:
            if os.path.getsize(chemin) > _MAX_OCTETS:
                os.replace(chemin, chemin + ".1")
        except OSError:
            pass
        if exc_info is None:
            exc_info = sys.exc_info()
        tb = ""
        if exc_info and exc_info[0] is not None:
            tb = "".join(traceback.format_exception(*exc_info)).rstrip()
        with open(chemin, "a", encoding="utf-8") as f:
            f.write("%s  [%s]\n" % (
                datetime.datetime.now().isoformat(timespec="seconds"), contexte))
            if tb:
                f.write(tb + "\n")
            f.write("\n")
    except Exception:
        pass    # dernier recours assumé : le journal lui-même est best-effort


def installer_hooks():
    """Route toute exception NON attrapée (main + threads) vers le journal,
    en conservant le comportement par défaut (affichage stderr)."""
    defaut_sys = sys.excepthook

    def _hook(exctype, value, tb):
        journaliser("exception non attrapée (thread principal)",
                    (exctype, value, tb))
        defaut_sys(exctype, value, tb)

    sys.excepthook = _hook

    defaut_thread = threading.excepthook

    def _hook_thread(args):
        journaliser("exception non attrapée (thread %s)"
                    % (args.thread.name if args.thread else "?"),
                    (args.exc_type, args.exc_value, args.exc_traceback))
        defaut_thread(args)

    threading.excepthook = _hook_thread
