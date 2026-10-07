#!/usr/bin/env bash
# Construit Pegasus-x86_64.AppImage a partir de ce dossier.
#
#   chmod +x build-appimage.sh && ./build-appimage.sh
#
# Resultat : un fichier unique et executable qui embarque Python, bleak, chess,
# le pont et l'interface. Aucune installation, aucun venv, aucun sudo chez
# l'utilisateur. La construction telecharge une image Python manylinux et
# appimagetool depuis GitHub : il lui faut un acces reseau.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PROJET="$PWD"

PYVER="${PYVER:-3.12}"
VENV="${VENV:-.build-venv}"

echo "== verification des fichiers"
for f in pyproject.toml pegasus_bridge.py pegasus_gui.py \
         appimage/pegasus.desktop appimage/pegasus.png appimage/entrypoint.sh; do
    [ -f "$f" ] || { echo "manquant : $f" >&2; exit 1; }
done
if grep -q '^Name=.* ' appimage/pegasus.desktop; then
    echo "le champ Name du fichier .desktop contient un espace : python-appimage" >&2
    echo "en fait le nom du fichier AppImage sans le proteger, et la" >&2
    echo "construction echoue a la derniere etape." >&2
    exit 1
fi

echo "== environnement de construction"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip python-appimage

# python-appimage lance « pip install » depuis SON dossier de travail, pas
# depuis la recette : un « . » dans requirements.txt y designerait le mauvais
# repertoire. On fabrique donc la recette dans un dossier temporaire, avec le
# chemin ABSOLU du projet — seule forme qui ne depende d'aucun repertoire
# courant.
RECETTE="$(mktemp -d)"
trap 'rm -rf "$RECETTE"' EXIT
# entrypoint.sh est le fichier que python-appimage transforme en AppRun.
# Sans lui, l'image lance simplement l'interpreteur Python.
cp appimage/pegasus.desktop appimage/pegasus.png appimage/entrypoint.sh "$RECETTE/"
printf '%s\n' "$PROJET" > "$RECETTE/requirements.txt"

echo "== construction (Python $PYVER manylinux)"
set +e
"$VENV/bin/python" -m python_appimage build app -p "$PYVER" "$RECETTE"
CODE=$?
set -e

# python-appimage echoue parfois apres avoir produit le fichier, sur la simple
# recopie finale. On cherche donc le resultat avant de conclure a un echec.
OUT="$(find . "$RECETTE" -maxdepth 2 -name '*.AppImage' -newermt '-10 minutes' \
       2>/dev/null | head -1 || true)"
if [ -z "$OUT" ]; then
    echo >&2
    echo "aucun AppImage produit (python-appimage a rendu $CODE)" >&2
    exit 1
fi
if [ "$(dirname "$OUT")" != "." ]; then
    mv "$OUT" .
fi
OUT="./$(basename "$OUT")"
chmod +x "$OUT"

echo
echo "== verification de l'image"
if "$OUT" --appimage-extract-and-run --python -c "import pegasus_bridge, chess, bleak" \
        >/dev/null 2>&1; then
    echo "   pont et dependances : OK"
else
    echo "   ATTENTION : le pont ne s'importe pas dans l'image" >&2
fi
if "$OUT" --appimage-extract-and-run --python -c "import tkinter" >/dev/null 2>&1; then
    echo "   interface graphique : OK"
else
    echo "   ATTENTION : tkinter absent du Python embarque. La ligne de"
    echo "   commande fonctionne, pas l'interface. Dis-le moi, on changera"
    echo "   de base Python."
fi

echo
echo "== termine : $OUT"
echo "   interface         :  $OUT"
echo "   ligne de commande :  $OUT play --help"
echo "   python embarque   :  $OUT --python -c '...'"
echo
echo "Le Bluetooth passe par le BlueZ du systeme hote : rien a embarquer,"
echo "mais bluetooth.service doit tourner."
echo
echo "Si le lancement repond « dlopen(): error loading libfuse.so.2 », deux"
echo "solutions : installer fuse2 (sudo pacman -S fuse2), ou lancer sans FUSE"
echo "avec $OUT --appimage-extract-and-run"
