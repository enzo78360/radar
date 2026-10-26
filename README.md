# Radar

**Découvre ce qui remplit ton disque — et libère de la place en quelques clics.**

Radar analyse un dossier (par défaut ton dossier personnel), puis ouvre dans ton navigateur une carte interactive de tout ce qu'il contient :

- **Carte en anneaux** : chaque anneau est un niveau de dossiers, la taille d'un arc est proportionnelle à la place occupée. Clique pour zoomer dans un dossier, clique au centre pour remonter.
- **Pistes de nettoyage** : Radar repère automatiquement ce qui grossit en douce et peut être supprimé sans risque — corbeille, caches d'applications, `node_modules`, environnements Python, Xcode DerivedData, simulateurs iOS, sauvegardes d'iPhone, Docker, installeurs `.dmg` oubliés…
- **Plus gros fichiers** du dossier, et répartition **par type** (vidéos, images, applications, archives…).
- **Actions directes** : « Finder » affiche l'élément dans le Finder, « Corbeille » le met à la corbeille (récupérable).

Aucune dépendance : un seul fichier Python (bibliothèque standard) et une page HTML. Rien ne quitte ton ordinateur.

## Lancer

Double-clique sur **`Radar.command`**, ou dans un terminal :

```bash
python3 radar.py
```

Pour analyser un autre dossier :

```bash
python3 radar.py ~/Documents
```

Options : `--port 8000` pour choisir le port, `--no-open` pour ne pas ouvrir le navigateur.

> **Astuce macOS** : pour analyser tout ton dossier personnel (y compris `Library`, Documents, Bureau…), donne l'« Accès complet au disque » au Terminal dans *Réglages Système → Confidentialité et sécurité*. Sans cela, certains dossiers protégés sont simplement comptés comme « inaccessibles ».

## Comment ça marche

- L'analyse parcourt l'arborescence avec `os.scandir`, compte l'espace **réellement occupé** sur le disque (blocs alloués, donc les fichiers creux ne trompent pas), ne compte qu'une fois les fichiers à liens multiples et ne traverse pas les autres volumes.
- Pour rester léger, les fichiers de moins de 512 Ko sont regroupés par dossier ; seuls les plus gros sont gardés individuellement.
- Un petit serveur local (`127.0.0.1` uniquement) sert l'interface. Les actions (Finder, corbeille) exigent un jeton secret généré à chaque lancement, et les requêtes venant d'un autre nom d'hôte sont refusées.
- La mise à la corbeille passe par le Finder : l'élément reste récupérable avec « Remettre ». La première fois, macOS demande d'autoriser le Terminal à piloter le Finder.

Environ 40 000 fichiers analysés par seconde sur un Mac récent.

## Licence

MIT
