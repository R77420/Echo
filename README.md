Remplacer entièrement README.md par le contenu ci-dessous,
puis commit "docs: README v1.9" et push.

═══ CONTENU DU NOUVEAU README.md ═══

<div align="center">

# Écho

**L'assistant de consultation médicale qui rédige vos comptes-rendus**

Transcription en temps réel · Correction médicale par IA · Compte-rendu validé par le médecin

[Site web](https://echo-site-web.vercel.app/) · [Télécharger la dernière version](https://github.com/R77420/Echo/releases/latest)

</div>

---

## Ce qu'Écho fait

Écho écoute votre consultation — au cabinet ou en téléconsultation —
et prépare automatiquement un compte-rendu médical structuré que
vous validez en quelques secondes.

- **Deux modes de consultation**
  - *Au cabinet* : le microphone capte la conversation, l'IA
    distingue automatiquement les répliques du médecin et du patient
  - *Téléconsultation* : capture séparée des deux interlocuteurs
    (Doctolib ou tout autre outil vidéo)
- **Transcription en temps réel** avec étiquetage automatique
  Médecin / Patient
- **Correction médicale par IA** — noms de médicaments, posologies
  et termes cliniques mal captés sont corrigés par contexte
  (« Doliphrène » → « Doliprane »)
- **Compte-rendu validé par le médecin** — l'IA propose une liste
  structurée (Motif · Observations · Traitements · Suivi), le
  médecin coche, décoche, édite ou complète. Rien n'est écrit sans
  sa validation.
- **Fiabilité renforcée** — triple filtre anti-erreur : les passages
  douteux sont signalés et jamais intégrés au compte-rendu sans
  contrôle
- **Dossiers patients** — consultations regroupées par patient,
  autocomplétion du nom, consultation lancée depuis la fiche
- **Export Word (.docx)** — compte-rendu validé en tête,
  transcription intégrale en annexe
- **Documents annexes** — ordonnances, arrêts de travail, imagerie
- **Compte médecin** avec période d'essai de 7 jours

## Le médecin garde le contrôle

Écho ne remplace pas le jugement médical : il prépare un brouillon.
Chaque compte-rendu est **validé par le médecin** avant d'être
enregistré — c'est lui qui décide de ce qui figure au dossier. Le
logiciel assiste la rédaction, il ne la décide pas.

## Confidentialité et données

- L'audio est transcrit via l'API Groq (infrastructure sécurisée) ;
  les enregistrements ne sont **pas conservés** après transcription
  et ne servent **jamais** à entraîner des modèles
- Les comptes-rendus et données patient sont stockés **en local**
  sur le poste du médecin
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
lorsqu'une nouvelle version est disponible.

## Architecture technique

| Composant | Technologie |
|---|---|
| Transcription | Whisper large-v3 via Groq |
| Correction médicale & attribution des locuteurs | Llama 3.3 70B via Groq |
| Extraction du compte-rendu | Llama 3.3 70B (sortie structurée JSON) |
| Capture audio | WASAPI loopback + microphone |
| Interface | pywebview (HTML/CSS/JS) |
| Export | python-docx |
| Comptes & paiement | Supabase + Stripe |
| Tests | pytest (76+ tests automatisés) |

## Développement

```bash
git clone https://github.com/R77420/Echo.git
cd Echo/realtime-transcription
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python transcription_consultation.py
```

Tests : `pytest tests/ -v`

## Licence

© 2026 Rayane Moussa. Tous droits réservés.
Logiciel propriétaire — l'utilisation commerciale requiert une
licence Écho active.

═══ FIN DU CONTENU ═══
