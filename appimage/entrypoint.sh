#! /bin/bash
# Point d'entree de l'AppImage Pegasus.
#
# python-appimage cherche un fichier nomme « entrypoint.* » dans la recette et
# en fait l'AppRun de l'image. Sans ce fichier, l'AppRun par defaut se contente
# de lancer l'interpreteur Python embarque — d'ou l'invite >>> au lieu de
# l'interface.
#
# La variable de modele juste sous ces lignes est remplacee a la construction
# par le chemin du Python embarque, du genre ${APPDIR}/usr/bin/python3.12.
#
#   ./Pegasus-x86_64.AppImage                 -> interface graphique
#   ./Pegasus-x86_64.AppImage play --address… -> ligne de commande
#   ./Pegasus-x86_64.AppImage --python …      -> l'interpreteur embarque

PY="{{ python-executable }}"
if [ ! -x "$PY" ]; then                       # filet, si la substitution rate
    PY="${APPDIR}/usr/bin/python3"
fi

if [ "${1:-}" = "--python" ]; then
    shift
    exec "$PY" "$@"
fi

if [ "${APPIMAGE_ENTRY:-}" = "pegasus-bridge" ]; then
    exec "$PY" -m pegasus_bridge "$@"
fi

case "${1:-}" in
    scan|watch|diag|raw|probe|play|serve|-h|--help)
        exec "$PY" -m pegasus_bridge "$@"
        ;;
esac

exec "$PY" -m pegasus_gui "$@"
