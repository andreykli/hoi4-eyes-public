# HOI4-EYES Beta


Diplomatie par IA locale et discussions gouvernementales pour Hearts of Iron IV.
Auteur : **NightPulse** · [Projet original](https://github.com/andreykli/hoi4-eyes-public).

[English](README.md) · [Русский](README_RU.md)

**0.9.0-beta.2 : beta publique, pas une version 1.0 achevée.** Cible : Windows,
HOI4 **1.20.0.0**, Python 3.10+, Ollama `qwen3:4b-instruct`.
La fenêtre utilise PyWebView/WebView2 et le serveur Flask existant.
Aucune API payante nécessaire ; par défaut, le modèle fonctionne localement.

**Mise à jour beta.2 : recette en jeu du mod principal encore incomplète.** Le contrôle indépendant des traités est obligatoire pour Windows, Flask et le moteur direct. Les données manquantes reportent la signature. Le texte du joueur et l’accord du modèle n’autorisent pas l’exécution. Les signatures anciennes sont conservées sans créer de nouveaux accords. Le profil moteur 1.19.5 reste disponible, sans nouveau test en jeu.

## Installation

1. Installer Python avec son lanceur Windows, Ollama et WebView2 Runtime.
2. Exécuter une fois `ollama pull qwen3:4b-instruct`. Le téléchargement nécessite
   Internet. Les poids et la licence du modèle sont distribués séparément.
3. Extraire Windows source dans un dossier accessible en écriture. Lancer une fois
   `setup_windows.bat`, puis utiliser `start.bat` pour les démarrages suivants.
4. Vérifier les services. Enregistrer le mod local dans les contrôles existants,
   HOI4 fermé. Activer une seule copie EYES.
5. Créer une nouvelle campagne jetable pour le premier essai. Les outils QA internes sont exclus de la distribution publique.

Le jeu ne démarre pas automatiquement. Cette distribution Python source n’est pas
un EXE signé. Sauvegarder l’installation avant remplacement. L’historique et les
réglages restent dans `%LOCALAPPDATA%/HOI4-EYES` ; l’extraction ne les efface pas.

## Fonctions

- Diplomatie et conférences de 2 à 6 pays, clauses typées, confirmations de
  révision, décisions structurées d’Ollama, refus et contre-propositions.
- Signature, fichiers contrôlés, installation après fermeture, reçus Bridge,
  protection contre les doublons, mémoire et génération de nouvelles.
- Atlas, Recognition Atlas, Country Maker, réunions, projets, presse et tableau
  de bord naval. Interface EN/RU/FR ; événements dans la langue de HOI4.

**Un accord n’est pas une exécution.** Toutes les parties requises confirment
la même révision. SIGNED/PREPARED/SENT ne prouvent pas un effet en jeu.
Les clauses non prises en charge bloquent l’exécution. Certains parcours
nécessitent de fermer le jeu, installer les scripts et le relancer.
Le Bridge observe les résultats. Les réunions, réformes et modèles navals
restent consultatifs sans exécuteur compatible et confirmation du joueur.
La géométrie non validée bloque les actions territoriales.

Un précédent QA isolé sur 1.19.5.0 : **21/21 PASS, Bridge 21/21**. Cela ne valide
pas toutes les fonctions ni les archives actuelles. Voir [guide et limites](docs/BETA_GUIDE.md),
[recette](docs/BETA_ACCEPTANCE.md), [changements](CHANGELOG.md) et [Workshop](docs/WORKSHOP.md).

Code original sous [Apache-2.0](LICENSE). Conserver [NOTICE](NOTICE), l’auteur,
son pseudonyme et le lien original, et signaler les modifications.
Les [composants tiers](THIRD-PARTY-NOTICES.md) conservent leurs licences.
Projet non officiel, sans affiliation à Paradox Interactive.

Author attribution and links: [NOTICE](NOTICE), [CREDITS](CREDITS.md), [ATTRIBUTION](ATTRIBUTION.md).

Original Steam Workshop: https://steamcommunity.com/sharedfiles/filedetails/?id=3842411221
