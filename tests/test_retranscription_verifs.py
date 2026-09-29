# -*- coding: utf-8 -*-
"""
Retranscription finale — vérifications avant commit :
  1. taille d'envoi (16 kHz mono, limite de taille API, rééchantillonnage) ;
  2. filtres identiques au temps réel + garde énergétique (silence de 20 s) ;
  3. nettoyage des WAV orphelins au démarrage (journalisé).
"""
import os
import sys
import wave

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio
import retranscription as rt
import transcription_consultation as tc
from test_retranscription import _FauxClient, _api_cabinet, _audio_avec_pauses, _rms, _sinus

SR = audio.SAMPLE_RATE


# ------------------------------------------------------------ 1. taille d'envoi

def test_wav_enregistre_en_16_khz_mono(tmp_path):
    """Le WAV continu est en 16 kHz mono 16 bits (32 Ko/s → 10 min ≈ 19 Mo)."""
    enr = rt.EnregistreurWav(str(tmp_path / "r.wav"))
    enr.fermer()
    with wave.open(enr.chemin, "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
    assert rt.taille_wav_octets(600 * SR) == 44 + 600 * SR * 2
    assert rt.taille_wav_octets(600 * SR) < rt.LIMITE_OCTETS_API


def test_bloc_max_borne_par_la_taille():
    # 16 kHz : 25 Mo × 0,9 ≈ 737 s → la durée nominale (600 s) reste le facteur limitant.
    assert rt.bloc_max_s(16000) == rt.BLOC_S
    # 48 kHz : la TAILLE limite avant la durée ; même déplacé de 10 s, < 25 Mo.
    b48 = rt.bloc_max_s(48000)
    assert b48 < rt.BLOC_S
    assert rt.taille_wav_octets((b48 + rt.MARGE_COUPE_S) * 48000) <= rt.LIMITE_OCTETS_API
    # Limite plus petite (test) → bloc plus court, jamais < 1 s.
    assert rt.bloc_max_s(16000, limite_octets=1_000_000) < 30
    assert rt.bloc_max_s(16000, limite_octets=10) == 1.0


def test_reechantillonnage_vers_16_khz():
    t = np.arange(48000 * 2) / 48000.0
    x = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    y = rt.reechantillonner(x, 48000, 16000)
    assert y.size == 32000
    assert _rms(y) == pytest.approx(_rms(x), rel=0.02)
    # Fréquence conservée : ~440 passages par zéro montants par seconde.
    montants = np.sum((y[:-1] < 0) & (y[1:] >= 0))
    assert 870 <= montants <= 890
    assert rt.reechantillonner(x, 16000, 16000) is x


def test_transcrire_audio_complet_reechantillonne_et_journalise_la_taille():
    client = _FauxClient()
    journal = []
    t = np.arange(48000 * 3) / 48000.0
    x = (0.3 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    rt.transcrire_audio_complet(x, client, sr=48000, journal=journal.append)
    buf = client.appels[0]["file"]
    buf.seek(0)
    with wave.open(buf, "rb") as w:
        assert w.getframerate() == 16000 and w.getnframes() == 48000
    assert any("48000 Hz → 16000 Hz" in m for m in journal)
    assert any("envoi 0.09 Mo (16000 Hz)" in m for m in journal)


def test_decoupage_suit_la_limite_de_taille():
    """Limite de 1 Mo (test) sur 90 s d'audio 16 kHz : blocs < 1 Mo chacun."""
    client = _FauxClient()
    x = _audio_avec_pauses(90.0, [(k * 10 - 0.1, k * 10 + 0.1) for k in range(1, 9)])
    rt.transcrire_audio_complet(x, client, limite_octets=1_000_000, marge_s=1)
    assert len(client.appels) >= 3
    for kw in client.appels:
        kw["file"].seek(0)
        assert len(kw["file"].read()) <= 1_000_000


# ------------------------------------------------------------ 2. filtres finaux

def test_filtres_finaux_identiques_au_temps_reel():
    f = tc.filtrer_segment_final
    assert f("Bonjour docteur, j'ai mal à la gorge.", 0.9) is None          # no_speech
    assert f("Sous-titrage amara.org", 0.1) is None                          # pattern connu
    assert f("Thank you for watching the video", 0.1) is None                # bascule anglaise
    assert f("et, langage, vieille et grosse perte, alcoolique et soins", 0.1) is None  # sans verbe
    assert f("Xorbulax frimbolette zandrique", 0.1) is None                  # charabia lexical
    assert f("   ", 0.1) is None and f(None, None) is None
    assert f("Bonjour docteur, j'ai mal à la gorge.", 0.1) == "Bonjour docteur, j'ai mal à la gorge."
    assert f("Bonjour docteur, j'ai mal à la gorge.", None) is not None


def test_plancher_et_segment_silencieux():
    rng = np.random.default_rng(3)
    x = (rng.standard_normal(SR * 40) * 0.003).astype(np.float32)   # pièce
    x[SR * 5:SR * 10] += _sinus(freq=200, amp=0.1, duree_s=5.0)     # parole
    x[SR * 30:SR * 35] += _sinus(freq=200, amp=0.1, duree_s=5.0)
    p = rt.plancher_energie(x)
    assert 0.002 < p < 0.004
    assert rt.segment_silencieux(x, 12.0, 28.0, p) is True
    assert rt.segment_silencieux(x, 5.0, 10.0, p) is False
    assert rt.segment_silencieux(x, 30.5, 34.0, p) is False
    # Plancher plafonné : un fichier sans aucune pause ne rejette rien.
    plein = _sinus(freq=200, amp=0.1, duree_s=20.0)
    assert rt.plancher_energie(plein) <= rt.SILENCE_PLANCHER_MAX
    assert rt.segment_silencieux(plein, 2.0, 8.0, rt.plancher_energie(plein)) is False


def test_silence_de_20_s_ne_produit_aucun_segment_invente(tmp_path, monkeypatch):
    """Whisper « invente » une phrase française plausible (no_speech faible)
    au milieu d'un silence de 20 s : la garde énergétique la rejette ; les
    segments de parole avant/après sont conservés."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    rep = {"segments": [
        {"start": 0.5, "end": 4.5, "text": " Bonjour docteur, j'ai mal à la gorge.", "no_speech_prob": 0.02},
        {"start": 12.0, "end": 16.0, "text": " Je vous prescris du Doliprane.", "no_speech_prob": 0.05},
        {"start": 27.0, "end": 31.0, "text": " Revenez me voir dans une semaine.", "no_speech_prob": 0.03},
    ]}
    monkeypatch.setattr(tc, "_init_cloud_client", lambda: _FauxClient(reponses=[rep]))
    rng = np.random.default_rng(4)
    x = (rng.standard_normal(SR * 32) * 0.002).astype(np.float32)
    x[:SR * 5] += _sinus(freq=180, amp=0.05, duree_s=5.0)
    x[SR * 26:SR * 32] += _sinus(freq=180, amp=0.05, duree_s=6.0)   # 20 s de silence entre 5 et 26
    chemin = str(tmp_path / "s20.wav")
    enr = rt.EnregistreurWav(chemin)
    enr.ecrire(x)
    enr.fermer()
    api = _api_cabinet()
    api._lancer_retranscription(chemin)
    assert api._final_evt.wait(10)
    assert api._final["status"] == "done"
    textes = [e[2] for e in api._final["entries"]]
    assert textes == ["Bonjour docteur, j'ai mal à la gorge.",
                      "Revenez me voir dans une semaine."]


# ------------------------------------------------------------ 3. WAV orphelins

def test_nettoyer_orphelins_au_demarrage(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(rt.tempfile, "gettempdir", lambda: str(temp))
    o1 = temp / "echo_cabinet_abc.wav"
    o2 = temp / "echo_cabinet_def.wav"
    autre = temp / "autre.wav"
    for f in (o1, o2, autre):
        f.write_bytes(b"RIFF")
    # Le dossier enregistrements\ du mode test n'est jamais touché.
    monkeypatch.setenv("APPDATA", str(tmp_path))
    garde = tmp_path / "Echo" / "enregistrements"
    garde.mkdir(parents=True)
    (garde / "cabinet_2026-09-29_10h00.wav").write_bytes(b"RIFF")
    journal = []
    supprimes = rt.nettoyer_orphelins(journal=journal.append)
    assert sorted(supprimes) == sorted([str(o1), str(o2)])
    assert not o1.exists() and not o2.exists() and autre.exists()
    assert (garde / "cabinet_2026-09-29_10h00.wav").exists()
    assert len(journal) == 2 and all("orphelin supprimé" in m for m in journal)
    # Rien à faire : liste vide, pas d'exception.
    assert rt.nettoyer_orphelins(journal=journal.append) == []


def test_main_nettoie_les_orphelins_avec_journal(monkeypatch):
    appels = []
    monkeypatch.setattr(tc, "installer_hooks", lambda: None)
    monkeypatch.setattr(tc, "_main_webview", lambda: None)
    monkeypatch.setattr(tc.retranscription, "nettoyer_orphelins",
                        lambda journal=None: appels.append(journal))
    monkeypatch.setattr(tc.sys, "argv", ["echo"])
    tc.main()
    assert appels == [tc.journaliser]
