# -*- coding: utf-8 -*-
"""
Tests de la retranscription finale du mode cabinet (retranscription.py +
intégration dans transcription_consultation.Api).

Tout est hors ligne et déterministe : le client Groq est simulé. Le test
réel (marqueur groq) valide le format verbose_json et le découpage en blocs
sur un vrai appel.
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

SR = audio.SAMPLE_RATE


def _sinus(freq=440, duree_s=1.0, amp=0.3):
    t = np.arange(int(SR * duree_s)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


# ------------------------------------------------------------ enregistreur WAV

def test_enregistreur_wav_ecrit_un_wav_valide(tmp_path):
    chemin = str(tmp_path / "c.wav")
    enr = rt.EnregistreurWav(chemin)
    frame = np.zeros(audio.FRAME_SAMPLES, dtype=np.float32)
    for _ in range(100):                       # 100 × 30 ms = 3 s
        assert enr.ecrire(frame)
    assert enr.duree_s == pytest.approx(3.0)
    assert enr.fermer() == chemin
    assert enr.fermer() == chemin              # idempotent
    with wave.open(chemin, "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == SR
        assert w.getnframes() == 100 * audio.FRAME_SAMPLES
    # Écrire après fermeture : refusé sans exception.
    assert enr.ecrire(frame) is False


def test_charger_wav_restitue_le_signal(tmp_path):
    chemin = str(tmp_path / "s.wav")
    sig = _sinus(duree_s=0.5)
    enr = rt.EnregistreurWav(chemin)
    enr.ecrire(sig)
    enr.fermer()
    relu, sr = rt.charger_wav(chemin)
    assert sr == SR and len(relu) == len(sig)
    assert np.max(np.abs(relu - sig)) < 1e-3


def test_enregistreur_erreur_ecriture_ne_casse_pas(tmp_path, monkeypatch):
    """Disque plein simulé : ecrire() renvoie False, `erreur` est renseignée,
    aucune exception ne remonte vers le thread de capture."""
    enr = rt.EnregistreurWav(str(tmp_path / "e.wav"))

    def boom(_):
        raise OSError("No space left on device")
    monkeypatch.setattr(enr._w, "writeframes", boom)
    assert enr.ecrire(np.zeros(10, dtype=np.float32)) is False
    assert "No space" in enr.erreur
    enr.fermer()


# ------------------------------------------------------------ prétraitement

def test_normaliser_ramene_au_niveau_cible_sans_ecretage():
    faible = _sinus(amp=0.02)                  # voix lointaine
    y = rt.normaliser(faible)
    assert _rms(y) == pytest.approx(rt.RMS_CIBLE, rel=0.05)
    assert np.max(np.abs(y)) <= rt.PIC_MAX + 1e-6
    # Signal déjà fort : plafonné par le pic, jamais au-dessus de PIC_MAX.
    fort = _sinus(amp=0.9)
    y = rt.normaliser(fort)
    assert np.max(np.abs(y)) <= rt.PIC_MAX + 1e-6


def test_normaliser_supprime_la_composante_continue_et_ignore_le_silence():
    x = _sinus(amp=0.1) + np.float32(0.2)
    y = rt.normaliser(x)
    assert abs(float(np.mean(y))) < 1e-3
    # Quasi-silence : non amplifié (on n'invente pas de signal).
    z = np.full(SR, 1e-7, dtype=np.float32)
    assert _rms(rt.normaliser(z)) < 1e-4


def test_passe_haut_coupe_le_ronflement_et_garde_la_voix():
    ronfle = _sinus(freq=50, amp=0.3, duree_s=2.0)
    voix = _sinus(freq=300, amp=0.3, duree_s=2.0)
    y_r = rt.filtre_passe_haut(ronfle)
    y_v = rt.filtre_passe_haut(voix)
    assert _rms(y_r) < 0.1 * _rms(ronfle)
    assert _rms(y_v) == pytest.approx(_rms(voix), rel=0.05)


def test_reduction_de_bruit_legere():
    """Bruit blanc seul → atténué ; ton pur au-dessus du bruit → préservé."""
    rng = np.random.default_rng(0)
    bruit = (rng.standard_normal(SR * 4) * 0.01).astype(np.float32)
    x = bruit.copy()
    x[SR * 2:] += _sinus(freq=440, amp=0.3, duree_s=2.0)
    y = rt.reduire_bruit(x)
    assert y.shape == x.shape
    # Première moitié (bruit seul) : au moins 30 % de réduction, mais jamais
    # muette (réduction légère, plancher DENOISE_PLANCHER).
    r_avant, r_apres = _rms(x[:SR * 2]), _rms(y[:SR * 2])
    assert r_apres < 0.7 * r_avant
    assert r_apres > rt.DENOISE_PLANCHER * 0.5 * r_avant
    # Seconde moitié (ton dominant) : énergie conservée à ±15 %.
    assert _rms(y[SR * 2:]) == pytest.approx(_rms(x[SR * 2:]), rel=0.15)


def test_reduction_de_bruit_par_tranches_sans_discontinuite():
    """Un signal plus long qu'une tranche (bloc_s réduit) : ni les bords ni
    les jonctions ne créent de saut d'amplitude (bug classique des bords de
    STFT : fenêtre quasi nulle → division par ~0)."""
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(SR * 3) * 0.01).astype(np.float32)
    # « Syllabes » : ton présent 0,3 s par seconde (non stationnaire, comme la
    # parole — un ton continu serait, lui, pris pour du bruit de fond).
    for k in range(3):
        d = k * SR
        x[d:d + int(0.3 * SR)] += _sinus(freq=220, amp=0.3, duree_s=0.3)
    y = rt.reduire_bruit(x, bloc_s=1)
    assert y.shape == x.shape
    # Pas de glitch : la dérivée reste bornée comme celle du signal d'entrée.
    assert np.max(np.abs(np.diff(y))) < 1.5 * np.max(np.abs(np.diff(x)))
    # Le ton ressort intact, y compris sur les 512 premiers échantillons
    # (bord de STFT) ; la fin (bruit seul) est atténuée, jamais amplifiée.
    assert _rms(y[:512]) == pytest.approx(_rms(x[:512]), rel=0.2)
    assert _rms(y[SR:SR + 512]) == pytest.approx(_rms(x[SR:SR + 512]), rel=0.2)
    assert _rms(y[-512:]) <= _rms(x[-512:])


def test_pretraiter_chaine_complete_et_vide():
    assert rt.pretraiter(np.zeros(0, dtype=np.float32)).size == 0
    y = rt.pretraiter(_sinus(amp=0.02, duree_s=2.0))
    assert _rms(y) == pytest.approx(rt.RMS_CIBLE, rel=0.1)
    assert np.max(np.abs(y)) <= rt.PIC_MAX + 1e-6


# ------------------------------------------------------------ découpage blocs

def _audio_avec_pauses(duree_s, pauses):
    """Bruit de parole simulé (sinus) avec des silences (amplitude ~0) aux
    instants donnés (liste de (debut_s, fin_s))."""
    x = _sinus(freq=200, amp=0.3, duree_s=duree_s)
    for d, f in pauses:
        x[int(d * SR):int(f * SR)] = 0.0
    return x


def test_decouper_blocs_un_seul_appel_si_court():
    blocs = rt.decouper_blocs(_sinus(duree_s=500.0))   # 8 min 20 → un seul bloc
    assert blocs == [(0.0, 500.0)]


def test_decouper_blocs_coupe_au_silence_sans_chevauchement():
    """25 min avec une pause à 604–605 s et 1197–1198 s : les frontières
    nominales (600, 1200) glissent dans les pauses ; blocs contigus."""
    x = _audio_avec_pauses(1500.0, [(604.0, 605.0), (1197.0, 1198.0)])
    blocs = rt.decouper_blocs(x, bloc_s=600, marge_s=10)
    assert len(blocs) == 3
    assert blocs[0][0] == 0.0 and blocs[-1][1] == 1500.0
    assert 604.0 < blocs[0][1] < 605.0
    assert 1197.0 < blocs[1][1] < 1198.0
    # Contigus : la fin de l'un est le début du suivant (ni trou ni doublon).
    assert all(blocs[i][1] == blocs[i + 1][0] for i in range(len(blocs) - 1))
    # Chaque bloc ≤ 610 s → WAV < 25 Mo (limite API).
    assert all((f - d) * SR * 2 < 25 * 1024 * 1024 for d, f in blocs)


def test_point_de_coupe_sans_pause_reste_pres_de_la_cible():
    x = _sinus(duree_s=100.0)
    c = rt.point_de_coupe(x, 50.0, marge_s=10)
    assert 40.0 <= c <= 60.0
    # Marge hors audio : cible inchangée.
    assert rt.point_de_coupe(np.zeros(100, dtype=np.float32), 50.0) == 50.0


def test_segments_absolus_decale_les_temps():
    segs = rt.segments_absolus(
        [{"start": 1.0, "end": 2.5, "text": " Bonjour ", "no_speech_prob": 0.1}], 600.0)
    assert segs == [{"debut": 601.0, "fin": 602.5, "texte": "Bonjour",
                     "no_speech_prob": 0.1}]


# ------------------------------------------------------------ appel Whisper simulé

class _FauxClient:
    """Simule client.audio.transcriptions.create (verbose_json)."""

    def __init__(self, reponses=None, echecs_avant_succes=0):
        self.appels = []
        self.reponses = reponses
        self.echecs = echecs_avant_succes

        class _T:
            def create(_s, **kw):
                self.appels.append(kw)
                if self.echecs > 0:
                    self.echecs -= 1
                    raise ConnectionError("réseau")
                i = len([a for a in self.appels]) - 1
                if self.reponses is not None:
                    return self.reponses[min(i, len(self.reponses) - 1)]
                return {"text": "bonjour docteur",
                        "segments": [{"start": 0.5, "end": 2.0,
                                      "text": " Bonjour docteur", "no_speech_prob": 0.01}]}

        class _A:
            transcriptions = _T()
        self.audio = _A()


def test_transcrire_audio_complet_un_seul_appel():
    client = _FauxClient()
    segs = rt.transcrire_audio_complet(_sinus(duree_s=3.0), client, prompt="Termes",
                                       langue="fr")
    assert len(client.appels) == 1
    kw = client.appels[0]
    assert kw["model"] == "whisper-large-v3"
    assert kw["response_format"] == "verbose_json"
    assert kw["language"] == "fr" and kw["temperature"] == 0
    assert kw["file"].name.endswith(".wav") and kw["file"].read(4) == b"RIFF"
    assert segs == [{"debut": 0.5, "fin": 2.0, "texte": "Bonjour docteur",
                     "no_speech_prob": 0.01}]


def test_transcrire_audio_complet_retry_puis_echec_leve():
    # 1 échec puis succès → OK (une nouvelle tentative par bloc).
    client = _FauxClient(echecs_avant_succes=1)
    segs = rt.transcrire_audio_complet(_sinus(duree_s=2.0), client)
    assert len(client.appels) == 2 and segs
    # 2 échecs → lève (l'appelant conserve le temps réel).
    client = _FauxClient(echecs_avant_succes=2)
    with pytest.raises(ConnectionError):
        rt.transcrire_audio_complet(_sinus(duree_s=2.0), client)


def test_transcrire_audio_complet_en_blocs_avec_contexte():
    """Audio de 25 s avec pauses à 10 s et 20 s, bloc_s=10 : 3 appels, coupes
    dans les pauses, contexte du bloc précédent en tête du prompt, segments en
    temps absolu triés (offset = début réel du bloc)."""
    rep = [
        {"segments": [{"start": 1.0, "end": 2.0, "text": "un"}]},
        {"segments": [{"start": 2.0, "end": 3.0, "text": "deux"}]},
        {"segments": [{"start": 1.0, "end": 2.0, "text": "trois"}]},
    ]
    client = _FauxClient(reponses=rep)
    x = _audio_avec_pauses(25.0, [(9.9, 10.1), (19.9, 20.1)])
    segs = rt.transcrire_audio_complet(x, client, prompt="Lexique",
                                       bloc_s=10, marge_s=1)
    assert len(client.appels) == 3
    assert client.appels[0]["prompt"] == "Lexique"
    assert client.appels[1]["prompt"].startswith("un Lexique")
    assert client.appels[2]["prompt"].startswith("deux Lexique")
    assert [s["texte"] for s in segs] == ["un", "deux", "trois"]
    debuts = [s["debut"] for s in segs]
    assert debuts[0] == 1.0
    assert 11.9 <= debuts[1] <= 12.1 and 20.9 <= debuts[2] <= 21.1


def test_transcrire_audio_complet_annulation():
    client = _FauxClient()
    with pytest.raises(RuntimeError):
        rt.transcrire_audio_complet(_sinus(duree_s=2.0), client, annule=lambda: True)
    assert client.appels == []


# ------------------------------------------------------------ mode test / fichiers

def test_mode_test_conserve_le_wav(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert rt.mode_test_actif() is False
    (tmp_path / "Echo").mkdir()
    (tmp_path / "Echo" / "enregistrement_test").write_text("")
    assert rt.mode_test_actif() is True
    src = tmp_path / "tmp.wav"
    src.write_bytes(b"RIFF")
    dest = rt.conserver_pour_analyse(str(src))
    assert os.path.dirname(dest) == rt.dossier_enregistrements()
    assert os.path.basename(dest).startswith("cabinet_") and dest.endswith(".wav")
    assert os.path.exists(dest) and not src.exists()


def test_supprimer_best_effort(tmp_path):
    f = tmp_path / "x.wav"
    f.write_bytes(b"x")
    assert rt.supprimer(str(f)) is True and not f.exists()
    assert rt.supprimer(str(f)) is False          # déjà absent : pas d'exception
    assert rt.supprimer(None) is False


# ------------------------------------------------------------ intégration Api

def _api_cabinet():
    api = tc.Api()
    api._mode = "cabinet"
    api._infos = {"nom": "TEST", "prenom": "", "ddn": "", "motif": ""}
    with api._lock:
        api._entries[:] = [("10:00:01", "Conversation", "aperçu temps réel")]
    return api


def _wav_de_test(tmp_path, duree_s=2.0):
    chemin = str(tmp_path / "consult.wav")
    enr = rt.EnregistreurWav(chemin)
    enr.ecrire(_sinus(duree_s=duree_s))
    enr.fermer()
    return chemin


def test_worker_remplace_le_temps_reel_et_supprime_le_wav(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))       # pas de flag test
    rep = {"segments": [
        {"start": 0.0, "end": 2.0, "text": " Bonjour docteur, j'ai mal à la gorge.",
         "no_speech_prob": 0.02},
        {"start": 2.0, "end": 3.0, "text": " Sous-titrage amara.org", "no_speech_prob": 0.1},
        {"start": 3.0, "end": 4.0, "text": " Merci beaucoup, bonne journée.", "no_speech_prob": 0.9},
    ]}
    monkeypatch.setattr(tc, "_init_cloud_client", lambda: _FauxClient(reponses=[rep]))
    api = _api_cabinet()
    wav = _wav_de_test(tmp_path)
    api._lancer_retranscription(wav)
    assert api._final_evt.wait(10)
    assert api._final["status"] == "done"
    assert not os.path.exists(wav)                    # WAV temporaire supprimé
    assert api._integrer_retranscription() is True
    with api._lock:
        entries = list(api._entries)
    # Hallucination connue et segment no_speech > 0.5 filtrés ; horodatage
    # relatif au début de consultation, étiquette Conversation (attribution ensuite).
    assert len(entries) == 1
    ts, loc, texte = entries[0]
    assert loc == "Conversation" and texte == "Bonjour docteur, j'ai mal à la gorge."
    assert ts == api._start_time.strftime("%H:%M:%S") if getattr(api, "_start_time", None) else True
    # Consommée : un second appel ne remplace plus rien.
    assert api._integrer_retranscription() is False


def test_worker_echec_conserve_le_temps_reel(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(tc, "_init_cloud_client",
                        lambda: _FauxClient(echecs_avant_succes=5))
    api = _api_cabinet()
    wav = _wav_de_test(tmp_path)
    api._lancer_retranscription(wav)
    assert api._final_evt.wait(10)
    assert api._final["status"] == "failed"
    assert not os.path.exists(wav)
    assert api._integrer_retranscription() is False
    with api._lock:
        assert api._entries == [("10:00:01", "Conversation", "aperçu temps réel")]


def test_worker_retranscription_vide_conserve_le_temps_reel(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(tc, "_init_cloud_client",
                        lambda: _FauxClient(reponses=[{"segments": []}]))
    api = _api_cabinet()
    api._lancer_retranscription(_wav_de_test(tmp_path))
    assert api._final_evt.wait(10)
    assert api._final["status"] == "failed"
    with api._lock:
        assert api._entries[0][2] == "aperçu temps réel"


def test_worker_mode_test_conserve_le_wav(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    (tmp_path / "Echo").mkdir()
    (tmp_path / "Echo" / "enregistrement_test").write_text("")
    monkeypatch.setattr(tc, "_init_cloud_client", lambda: _FauxClient())
    api = _api_cabinet()
    wav = _wav_de_test(tmp_path)
    api._lancer_retranscription(wav)
    assert api._final_evt.wait(10)
    assert api._final["status"] == "done"
    assert not os.path.exists(wav)                    # déplacé…
    gardes = os.listdir(rt.dossier_enregistrements())  # …dans enregistrements/
    assert len(gardes) == 1 and gardes[0].startswith("cabinet_")


def test_resultat_perime_ignore_apres_abandon(tmp_path, monkeypatch):
    """« Quitter sans enregistrer » pendant la retranscription : le résultat
    tardif n'écrase rien et le WAV est quand même supprimé."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import threading
    feu_vert = threading.Event()

    class _Lent(_FauxClient):
        def __init__(self):
            super().__init__()
            create0 = self.audio.transcriptions.create

            def create(**kw):
                feu_vert.wait(5)
                return create0(**kw)
            self.audio.transcriptions.create = create
    monkeypatch.setattr(tc, "_init_cloud_client", lambda: _Lent())
    api = _api_cabinet()
    wav = _wav_de_test(tmp_path)
    api._lancer_retranscription(wav)
    assert api._final["status"] == "running"
    api._final_gen += 1                                # ≡ end_consultation / nouvelle consultation
    api._final = {"status": "idle", "entries": None}
    feu_vert.set()
    import time
    for _ in range(50):
        if not os.path.exists(wav):
            break
        time.sleep(0.1)
    assert not os.path.exists(wav)
    assert api._final["status"] == "idle"
    assert api._integrer_retranscription() is False


def test_perform_save_integre_la_retranscription(tmp_path, monkeypatch):
    """perform_save écrit le .docx à partir de la retranscription (pas de
    l'aperçu) et l'extraction du CR part des mêmes entrées."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(tc, "charger_config", lambda: {})
    monkeypatch.setattr(tc, "sauver_config", lambda c: None)
    monkeypatch.setattr(tc, "chemin_consultations", lambda: str(tmp_path / "c.json"))
    ecrits = {}
    monkeypatch.setattr(tc.storage, "ecrire_docx",
                        lambda fp, infos, now, res, entries, annexes=None: ecrits.update(
                            {"entries": list(entries)}))
    monkeypatch.setattr(tc.storage, "ajouter_consultation", lambda chemin, rec: ecrits.update(
        {"record": rec}))
    monkeypatch.setattr(tc.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    api = _api_cabinet()
    api._final = {"status": "done",
                  "entries": [("10:00:00", "Conversation", "transcription complète")]}
    res = api.perform_save(str(tmp_path / "x.docx"), "", [])
    assert res["ok"]
    assert ecrits["entries"] == [("10:00:00", "Conversation", "transcription complète")]
    assert ecrits["record"]["entries"] == [["10:00:00", "Conversation", "transcription complète"]]


def test_arreter_capture_lance_la_retranscription_en_cabinet(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    lances = []
    api = _api_cabinet()
    monkeypatch.setattr(api, "_lancer_retranscription", lambda wav: lances.append(wav))
    # Enregistreur ouvert comme le ferait start(cabinet), avec 2 s d'audio.
    monkeypatch.setattr(rt, "chemin_temporaire", lambda: str(tmp_path / "live.wav"))
    enr = tc._ouvrir_enregistrement()
    assert enr is not None
    tc._enregistrer_frame(_sinus(duree_s=2.0))
    api.arreter_capture()
    assert lances == [str(tmp_path / "live.wav")]
    assert tc._enregistreur is None
    # Télé : aucun enregistreur → rien lancé, rien planté.
    lances.clear()
    api._mode = "tele"
    api.arreter_capture()
    assert lances == []


def test_capture_trop_courte_jetee(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    api = _api_cabinet()
    lances = []
    monkeypatch.setattr(api, "_lancer_retranscription", lambda wav: lances.append(wav))
    monkeypatch.setattr(rt, "chemin_temporaire", lambda: str(tmp_path / "court.wav"))
    tc._ouvrir_enregistrement()
    tc._enregistrer_frame(_sinus(duree_s=0.3))
    api.arreter_capture()
    assert lances == [] and not (tmp_path / "court.wav").exists()


def test_start_cabinet_ouvre_l_enregistrement_tele_non(monkeypatch, tmp_path):
    from test_modes import _prepare
    _prepare(monkeypatch)
    ouverts = []
    monkeypatch.setattr(tc, "_ouvrir_enregistrement", lambda: ouverts.append(1))
    api = tc.Api()
    assert api.start("MicroTest", "SortieTest", mode="cabinet")["ok"]
    assert ouverts == [1]
    api = tc.Api()
    assert api.start("MicroTest", "SortieTest", mode="tele")["ok"]
    assert ouverts == [1]


def test_ui_finalisation_branchee():
    """L'overlay attend la retranscription avant perform_save (cabinet) et
    affiche le message de finalisation."""
    chemin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "ui", "index.html")
    with open(chemin, encoding="utf-8") as f:
        html = f.read()
    assert "Finalisation de la transcription…" in html
    assert "get_finalisation_status" in html
    i_att = html.index("await attendreFinalisation()")
    i_save = html.index("await api('perform_save'")
    assert i_att < i_save


# ------------------------------------------------------------ Groq réel

from conftest import groq_reel   # noqa: E402


@groq_reel
def test_retranscription_reelle_verbose_json():
    """Vrai appel : le format verbose_json + timeout sont acceptés et les
    segments portent start/end. (Un ton pur ne produit pas de parole : on
    vérifie le contrat d'API, pas le texte.)"""
    from GROQ_KEY import GROQ_API_KEY
    import openai
    client = openai.OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    segs = rt.transcrire_audio_complet(_sinus(duree_s=2.0), client, prompt="Consultation")
    assert isinstance(segs, list)
    for s in segs:
        assert set(s) == {"debut", "fin", "texte", "no_speech_prob"}
