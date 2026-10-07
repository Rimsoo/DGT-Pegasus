#!/usr/bin/env bash
# Installe l'AppImage dans ~/.local : une commande « pegasus » et une entree
# dans le menu des applications. Rien en dehors du dossier personnel, aucun
# sudo.
#
#   ./install.sh                 installe le dernier AppImage construit ici
#   ./install.sh chemin.AppImage installe celui-la
#   ./install.sh -u              desinstalle
#
# Si l'outil « shortcut » (github.com/Rimsoo/shortcut) est dans le PATH, c'est
# lui qui fait le travail. Sinon on fait la meme chose a la main.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

NOM="pegasus"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/256x256/apps"

desinstaller() {
    if command -v shortcut >/dev/null 2>&1; then
        shortcut -u "$NOM" && return 0
    fi
    rm -f "$BIN/$NOM" "$APPS/$NOM.desktop" "$ICONS/$NOM.png"
    echo "desinstalle."
}

if [ "${1:-}" = "-u" ] || [ "${1:-}" = "--uninstall" ]; then
    desinstaller
    exit 0
fi

APP="${1:-}"
if [ -z "$APP" ]; then
    APP="$(ls -t ./*.AppImage 2>/dev/null | head -1 || true)"
fi
if [ -z "$APP" ] || [ ! -f "$APP" ]; then
    echo "aucun AppImage trouve. Construis-le d'abord :" >&2
    echo "  ./build-appimage.sh" >&2
    exit 1
fi
APP="$(readlink -f "$APP")"
chmod +x "$APP"

if command -v shortcut >/dev/null 2>&1; then
    echo "== installation avec shortcut"
    shortcut "$APP" "$NOM" "$PWD/appimage/pegasus.png"
else
    echo "== installation manuelle (shortcut absent du PATH)"
    mkdir -p "$BIN" "$APPS" "$ICONS"
    ln -sf "$APP" "$BIN/$NOM"
    cp -f appimage/pegasus.png "$ICONS/$NOM.png"
    # L'Exec du fichier livre vise le point d'entree interne a l'image ; pour
    # le menu du bureau il doit viser l'AppImage elle-meme.
    sed -e "s|^Exec=.*|Exec=$BIN/$NOM|" \
        -e "s|^Icon=.*|Icon=$NOM|" \
        -e '/^#/d' \
        appimage/pegasus.desktop > "$APPS/$NOM.desktop"
    chmod 644 "$APPS/$NOM.desktop"
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$APPS" 2>/dev/null || true
fi

echo
echo "== termine"
echo "   commande      :  $NOM"
echo "   menu          :  Pegasus (categorie Jeux)"
echo "   ligne de cmd  :  $NOM play --address XX:XX:XX:XX:XX:XX"
echo "   desinstaller  :  ./install.sh -u"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo
       echo "   note : $BIN n'est pas dans ton PATH."
       echo "          Ajoute-le dans ~/.zshrc ou ~/.bashrc :"
       echo "          export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
