#!/bin/bash
# Double-clique sur ce fichier dans le Finder pour analyser ton dossier personnel.
cd "$(dirname "$0")" && exec python3 radar.py "$@"
