\# Écho — contexte projet



App Windows de transcription médicale. Python + pywebview.



\## Commandes

\- Dev : `.\\.venv\\Scripts\\python.exe .\\transcription\_consultation.py`

\- Tests : `.\\.venv\\Scripts\\python.exe -m pytest tests\\ -v`

\- Build : PyInstaller Echo.spec puis ISCC setup.iss



\## Architecture

\- transcription\_consultation.py — app principale, classe Api (pont pywebview)

\- audio.py — capture WASAPI + micro, VAD, seuils RMS

\- correction.py — appels Groq : correction médicale, attribution

&#x20; des locuteurs, extraction JSON du compte-rendu

\- storage.py — consultations.json, génération .docx

\- retranscription.py — cabinet : enregistrement continu (WAV temporaire)

&#x20; + retranscription finale de l'audio complet au « Terminer » (prétraitement

&#x20; numpy, blocs coupés au silence, fail-safe = garder le temps réel).

&#x20; Flag debug `%APPDATA%\\Echo\\enregistrement_test` → WAV conservé dans

&#x20; `%APPDATA%\\Echo\\enregistrements\\` (jamais en prod sans consentement).

\- config.py, demarrage.py, tray.py

\- ui/ — main\_window.html, index.html (overlay), style.css



\## Règles

\- GROQ\_KEY.py ne doit JAMAIS être commité

\- Toujours tester en dev avant de rebuilder

\- Principe produit : l'IA propose, le médecin valide

\- Les seuils audio sont calibrés par test réel, pas au jugé

## Pièges connus (à ne jamais refaire)

### Fonds transparents en thème sombre — BUG RÉCURRENT (3×)
`--bg-card` vaut `rgba(21,41,35,0.25)` en thème sombre : c'est un
effet verre voulu pour les cartes POSÉES sur le fond de l'app.

Tout élément FLOTTANT (modale, bulle, dropdown, panneau, tooltip,
popover, coach mark) qui utilise `--bg-card` sera ILLISIBLE en
sombre — on voit l'interface à travers.

Règle : tout élément flottant utilise un fond OPAQUE explicite.
  [data-theme="dark"] .mon-element-flottant { background: #1a2e28; }
  (thème clair : #ffffff)
Jamais de rgba avec alpha < 0.9 sur un élément flottant.

Déjà touchés et corrigés : .profile-panel, .confirm-box, .mode-card,
.combo-list, .ac-list. Vérifier tout nouveau composant flottant.

### Textes obsolètes
Le pivot Groq a rendu faux le discours « 100 % local ».
Ne jamais réintroduire : "100 % local", "aucune donnée ne quitte",
"Qwen", "faster-whisper", "téléchargement des modèles".
Message de référence : audio transcrit via infrastructure sécurisée,
non conservé, jamais utilisé pour entraîner ; comptes-rendus en local.
(Un test `test_pas_de_texte_100_local` garde cette règle.)

### Piège du grep
Le HTML utilise `&nbsp;` — un grep sur "100 % local" rate
"100&nbsp;% local". Toujours chercher les variantes d'espaces.

### Éléments flottants (CSS)
Toute surface FLOTTANTE (modale, panneau, dropdown, bulle, menu) utilise
`var(--bg-float)` — #ffffff clair / #1a2e28 sombre, toujours opaque.
JAMAIS `var(--bg-card)` : elle vaut rgba(21,41,35,0.25) en sombre et on
lit l'interface à travers (bug survenu 3 fois : .profile-panel,
.confirm-box/.mode-card, .tour-bubble). Les backdrops `*-overlay`
restent translucides par design. Gardé par tests/test_css_conformite.py.

## Reprise 
## État actuel (août 2026)

Version en cours : v2.3.0 (à builder)
Dossier : en cours de renommage Écho → Echo
Deux médecins utilisateurs réels (ma mère + Dr Ben-Salah/Ahmed Amri)

### Fait récemment (validé, à builder en v2.3.0)
- Vérification email à l'inscription (code 6 chiffres via Resend,
  domaine send.echo-medical.fr) + mot de passe oublié — backend testé
- Auto-update RÉPARÉ (était cassé depuis toujours : requests absent
  du build, remplacé par urllib)
- Démarrage 3x plus rapide (lazy import faster_whisper, licence
  non bloquante avec cache local fail-open)
- Journal central des erreurs (%APPDATA%\Echo\erreurs.log)
- Fix visite guidée après inscription (flag onboarding_fait explicite)
- Messages d'erreur honnêtes (fin du fourre-tout "Erreur réseau")

### À faire ensuite
- Tester fallback hors-ligne réel (couper réseau en consultation)
- Build + release v2.3.0, installer à la main chez les 2 médecins
- Vérifier mode build onefile vs onedir (démarrage exe)
- Blocages avant vente : certificat EV, mentions légales, webhook Stripe,
  chiffrement données locales, passer Supabase Pro

### Question terrain en suspens
Est-ce que ma mère valide vraiment ses comptes-rendus ?
