# Écho — Transcription médicale en temps réel

Logiciel Windows de transcription automatique pour la téléconsultation médicale. Conçu pour les médecins pratiquant des consultations vidéo via Doctolib ou tout autre outil de téléconsultation.

## Fonctionnalités

- Transcription en temps réel des deux interlocuteurs (médecin et patient)
- Détection automatique des tours de parole, étiquetés Médecin / Patient
- Résumé automatique structuré (Motif · Observations · Traitements · Suivi)
- Export Word (.docx) avec en-tête patient et transcription complète
- Ajout de documents annexes (ordonnance, arrêt de travail, etc.)
- Historique des consultations avec accès aux comptes-rendus
- 100 % local — aucune donnée ne quitte l'ordinateur

## Confidentialité

Écho fonctionne entièrement hors-ligne après le premier lancement. Aucune donnée audio, aucune transcription, aucune information patient n'est envoyée sur internet. Les modèles d'IA tournent localement sur votre machine.

## Installation

Télécharger le dernier installeur depuis la page [Releases](releases). Windows 10 / 11 64-bit requis. 8 Go de RAM recommandés. Une connexion internet est nécessaire uniquement au premier lancement (téléchargement des modèles, ~3,5 Go, opération unique).

## Technologies

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (OpenAI Whisper large-v3-turbo)
- [llama-cpp-python](https://github.com/abetlen/llama-cpp-python) + Qwen 2.5-3B
- [pywebview](https://pywebview.flowrl.com/)
- [python-docx](https://python-docx.readthedocs.io/)

## Licence

© 2026 [Ton nom]. Tous droits réservés.
