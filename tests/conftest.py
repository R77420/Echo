# -*- coding: utf-8 -*-
"""
Configuration partagée des tests.

Fournit `groq_reel`, un décorateur unique pour les tests qui appellent
réellement l'API Groq :
  - marqueur `groq` (exclu par défaut via pytest.ini) ;
  - skip automatique si GROQ_KEY.py est absent ;
  - un unique retry sur erreur 429 (pic de débit), pour ne pas casser
    la validation des prompts sur une simple limite de quota.
"""
import functools
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import correction

GROQ_OK = bool(correction.GROQ_API_KEY)


def _est_429(exc):
    msg = str(exc).lower()
    return ("429" in msg or "rate limit" in msg or "too many requests" in msg)


def groq_reel(fn):
    """Décore un test faisant un vrai appel Groq (voir module docstring)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:            # noqa: BLE001
            if _est_429(exc):
                time.sleep(3)               # laisse retomber le pic de débit
                return fn(*args, **kwargs)  # un seul retry
            raise
    wrapper = pytest.mark.groq(wrapper)
    wrapper = pytest.mark.skipif(
        not GROQ_OK, reason="GROQ_KEY.py absent — test réel ignoré")(wrapper)
    return wrapper
