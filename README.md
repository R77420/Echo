<div align="center">

# Écho

**L'assistant de transcription médicale pour la téléconsultation**

Transcription en temps réel · Correction médicale par IA · Compte-rendu structuré

[Site web](https://echo-site-web.vercel.app/) · [Dernière version](https://github.com/R77420/Echo/releases/latest)

</div>

---

## Ce qu'Écho fait

Écho écoute votre téléconsultation (Doctolib ou tout autre outil) et
produit automatiquement un compte-rendu médical structuré, prêt à être
versé au dossier patient.

- **Transcription en temps réel** des deux interlocuteurs, avec
  étiquetage automatique Médecin / Patient
- **Correction médicale par IA** — les noms de médicaments, posologies
  et termes cliniques mal captés sont corrigés par un modèle de langage
  entraîné (ex. « Doliphrène » → « Doliprane »)
- **Compte-rendu structuré** généré en quelques secondes :
  Motif · Observations · Traitements · Suivi
- **Export Word (.docx)** — résumé médical en tête, transcription
  intégrale en annexe
- **Documents annexes** — ordonnances, arrêts de travail, imagerie
- **Historique et recherche** — retrouvez toutes les consultations
  d'un patient
- **Compte médecin** avec période d'essai de 7 jours

## Confidentialité et données

- L'audio est transcrit via l'API Groq (infrastructure sécurisée) ;
  les enregistrements ne sont **pas conservés** après transcription
  et ne servent **jamais** à entraîner des modèles
- Les comptes-rendus et données patient sont stockés **uniquement en
  local** sur le poste du médecin — aucun stockage cloud
- Aucune donnée nominative de patient n'est transmise à des tiers

## Installation

1. Télécharger `EchoSetup.exe` depuis la page
   [Releases](https://github.com/R77420/Echo/releases/latest)
2. Lancer l'installeur et suivre les étapes
3. Créer votre compte médecin au premier lancement — 7 jours
   d'essai gratuit

**Configuration requise** : Windows 10/11 64-bit · 4 Go de RAM ·
connexion internet

Les mises à jour sont automatiques : l'application vous prévient
quand une nouvelle version est disponible.

## Architecture technique

| Composant | Technologie |
|---|---|
| Transcription | Whisper large-v3 via Groq |
| Correction médicale | Llama 3.3 70B via Groq |
| Résumé structuré | Llama 3.3 70B via Groq |
| Capture audio | WASAPI loopback + microphone (deux canaux) |
| Interface | pywebview (HTML/CSS/JS) |
| Export | python-docx |
| Comptes & licences | Supabase + Stripe |

## Développement

```bash
git clone https://github.com/R77420/Echo.git
cd Echo/realtime-transcription
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python transcription_consultation.py
```

Les tests : `pytest tests/ -v`

## Licence

© 2026 Rayane Moussa. Tous droits réservés.
Logiciel propriétaire — l'utilisation commerciale requiert une
licence Écho active.
