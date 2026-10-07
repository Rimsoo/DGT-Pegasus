# DGT Pegasus → Lichess sur Arch

Deux façons d'utiliser le pont :

* **Mode direct** (`play`) — le pont parle à lichess lui-même. Les coups
  partent, ceux de l'adversaire allument les cases du plateau. **Aucun sudo,
  aucun périphérique, aucun LiveChess.** C'est le mode recommandé.
* **Mode LiveChess** (`serve`) — le pont se fait passer pour une carte DGT sur
  un port série. Utile si tu tiens à l'interface LiveChess ou à son API, mais
  il faut `tty0tty`, `sudo`, et les LED ne fonctionnent pas (LiveChess ne
  connaît pas le coup de l'adversaire et ne pilote aucune LED).

## Mode direct, en trois commandes

```bash
sudo pacman -S --needed bluez bluez-utils python
sudo systemctl enable --now bluetooth
python -m venv ~/.venv/pegasus && ~/.venv/pegasus/bin/pip install bleak chess

# jeton lichess, porte « board:play » :
#   https://lichess.org/account/oauth/token/create?scopes[]=board:play&description=Pegasus%20bridge
~/.venv/pegasus/bin/python pegasus_bridge.py play \
    --address XX:XX:XX:XX:XX:XX --token lip_xxxxx --save-token
```

Le jeton est rangé dans `~/.config/pegasus-bridge/token` en 0600 ; les fois
suivantes, `play --address XX:XX:XX:XX:XX:XX` suffit. Lance ensuite une partie
sur lichess.org : elle est détectée et suivie toute seule.

Pendant la partie, le pont compare en permanence la liste des coups de lichess
et celle déduite du plateau. Plateau en retard : les deux cases du coup à
rejouer s'allument. Plateau en avance d'un coup, et c'est ton tour : le coup
part. Les deux listes égales : les LED s'éteignent.

## 0. Calibrer le plateau (sinon rien ne marchera)

La détection du Pegasus est **inductive** — une détection de métal à très courte
portée. Deux conséquences :

* un aimant ne prouve rien, seules les pièces du Pegasus déclenchent les
  capteurs ;
* le plateau doit être **calibré**, et ça ne se fait pas tout seul à l'allumage.

Sans calibration, le plateau renvoie `01` sur les 64 cases (« tout occupé »),
ne signale jamais aucun changement, et se comporte exactement comme s'il était
en panne. Calibre d'abord, débogue ensuite.

Si le plateau reste muet, DGT publie une remise à zéro usine : charge complète,
plateau éteint et vide, puis appui long sur on/off **jusqu'à ce que d4, d5, e4
et e5 clignotent rapidement et simultanément**. Si ces quatre cases ne
clignotent pas, la carte capteurs/LED ne répond pas et c'est un défaut matériel.

## 2. Valider la liaison BLE (à faire en premier)

```bash
~/.venv/pegasus/bin/python pegasus_bridge.py scan
```

Tu dois voir une ligne marquée `<-- Nordic UART (Pegasus ?)`. Relève l'adresse,
puis :

```bash
~/.venv/pegasus/bin/python pegasus_bridge.py probe --address XX:XX:XX:XX:XX:XX -v
```

Le handshake doit afficher le numéro de série et `firmware 1.x (major=1 ->
Pegasus confirmé)`, puis l'échiquier se met à jour quand tu bouges une pièce.
Si le plateau apparaît à l'envers, ajoute `--flip`. **Rien ne sert de continuer
avant que cette étape marche.**

## 2 bis. Quand la carte répond mais ne livre pas l'occupation

Symptôme : le handshake passe (numéro de série, firmware, trademark), mais le
board dump annonce 64 cases occupées et aucun `field update` n'arrive.

```bash
# trace complète + scrutation automatique des dumps
~/.venv/pegasus/bin/python pegasus_bridge.py probe --address XX:XX:XX:XX:XX:XX -v --trace

# console brute : on envoie les octets à la main
~/.venv/pegasus/bin/python pegasus_bridge.py raw --address XX:XX:XX:XX:XX:XX
  44        # mode streaming d'abord
  42        # puis un dump
```

Trois causes possibles, que la trace départage :

| Indice dans la trace | Cause | Correctif |
|---|---|---|
| dump constant `01` × 64, aucun `field update` | carte non déverrouillée : la clé développeur doit être la **toute première** écriture | `--handshake ext` (défaut) |
| le nombre de cases occupées **baisse** quand tu ajoutes des pièces | codage inversé (0 = occupée) | `--invert` |
| dump figé mais plausible | la carte ne pousse rien | scrutation (auto après 5 s, ou `--poll 0.3`) |

Deux séquences d'initialisation coexistent dans les implémentations publiques :

* `--handshake ext` — celle de l'extension Chrome : clé développeur d'abord,
  puis `40`, `42`, `44`. **C'est le défaut.**
* `--handshake dgt` — celle du pilote Dart `mono424/dgtdriver` (reprise par
  BoardKit) : `40`, `45`, `4d`, *puis* la clé. Si la carte n'accepte la clé
  qu'en première écriture, cette séquence renvoie un plateau factice.

Test physique, indépendant des capteurs : `--led-test` allume la rangée du haut
pendant 3 s. Si les LED s'allument, la carte reçoit bien tes commandes et le
problème est du côté des données, pas de la liaison.

## 3. Le port série virtuel

### Option A — pty + lien symbolique (le plus rapide)

```bash
sudo ~/.venv/pegasus/bin/python pegasus_bridge.py serve \
     --address XX:XX:XX:XX:XX:XX --link /dev/ttyUSB0 --show-board
```

Marche **si** LiveChess devine ses ports en énumérant `/dev/ttyUSB*`.

Un pty n'a **pas de lignes de modem** : le noyau ne gère ni DTR, ni DSR, ni CTS
sur `/dev/pts/*`, et `TIOCMSET` y échoue. Une bibliothèque série qui pose DTR/RTS
à l'ouverture — c'est le cas de jSSC par défaut — échoue donc silencieusement,
ouvre le port et n'écrit jamais rien. Symptôme exact : le port apparaît dans la
liste, LiveChess dit `Opening port`, et le pont ne reçoit pas un octet.

### Option B — tty0tty (vraies lignes de modem)

`tty0tty` est un émulateur de câble null-modem : il crée de vrais tty noyau
appariés deux par deux, avec DTR/DSR/CTS/RTS émulés.

```bash
yay -S tty0tty-dkms          # ou paru, ou make depuis github.com/lcgamboa/tty0tty
sudo modprobe tty0tty
ls -l /dev/tnt*              # tnt0<->tnt1, tnt2<->tnt3, ...
```

Le nom `tnt0` ne correspond à aucun motif reconnu par les bibliothèques série
(`ttyS`, `ttyUSB`, `ttyACM`, `rfcomm`…), donc LiveChess ne le listerait pas : on
lui présente un lien au bon nom. Le pont s'en charge.

```bash
sudo ~/.venv/pegasus/bin/python pegasus_bridge.py serve \
     --address XX:XX:XX:XX:XX:XX --device /dev/tnt1 --link /dev/ttyUSB0 --debug
```

Le pont tient `/dev/tnt1`, ouvre les droits sur les deux bouts, et pointe
`/dev/ttyUSB0` vers `/dev/tnt0`, que LiveChess ouvrira.

### Ordre de lancement

Le pty (option A) est **recréé à chaque démarrage du pont**. LiveChess doit donc
toujours être lancé *après* le pont, sinon il pointe vers un périphérique
disparu. Avec tty0tty le problème ne se pose pas : `/dev/tnt*` est stable.

### Lire le dialogue

```bash
sudo ~/.venv/pegasus/bin/python pegasus_bridge.py serve … --debug 2>&1 | tee /tmp/pont.log
```

`--debug` trace chaque octet (`serie ->` entrant, `serie <-` sortant) et tait
les journaux bleak/D-Bus. Le pont annonce `LiveChess a ouvert le port et
commence a parler` au premier octet reçu, et prévient au bout de 20 s si
personne ne s'est présenté.

## 3 bis. Le mode bus

LiveChess est un logiciel de tournoi : il ne détecte pas les cartes en mode
simple, mais en **mode bus** DGT, le protocole multi-cartes. Sa séquence de
détection, lue au `--debug` :

```
40 40 40      reset
47            trademark        -> doit contenir « Digital Game Technology »
46            adresse de bus   -> 2 octets de 7 bits
4a            passage en mode bus
8b 00 00 0b   DGT_BUS_SEND_VERSION vers l'adresse 0 (diffusion)
```

Une carte qui ne répond pas à cette dernière trame n'existe pas pour LiveChess,
même si tout le reste est parfait. Le pont implémente le mode bus complet :
ping, dump, version, début de partie, changements et rejeu.

Les commandes du mode bus font 4 octets — `[code|0x80, adresse MSB, adresse LSB,
somme]` — et se distinguent du mode simple par le bit 7. LiveChess sonde aussi
d'autres marques sur le même port ; ces octets-là sont écartés par la somme de
contrôle.

## 4. Côté LiveChess puis lichess

1. Lance LiveChess, la carte doit apparaître dans son interface
   (`http://localhost:1982`).
2. Sur lichess.org : *Préférences → DGT board*, qui se connecte au WebSocket de
   LiveChess (`ws://localhost:1982/api/v1.0`) avec un jeton d'API.

## 5. Ce que le pont sait et ne sait pas faire

* **Coups déduits, pas lus.** L'occupation stable est comparée aux coups légaux
  (profondeur 1, puis 2 si besoin). Prises, roques, prise en passant et
  promotions sont couverts et testés.
* **Promotion** : supposée dame, `--promotion r|b|n` pour changer.
* **Désynchronisation** : si tu déplaces des pièces à la main sans jouer un coup
  légal, le pont le dit. Repose la position initiale : il repart sur une
  nouvelle partie tout seul (`--no-auto-reset` pour désactiver).
* **Orientation** : `--flip` si tu joues avec les noirs devant toi.
* **Pas de pendule** : le pont répond « aucune pendule branchée ».
* **LED du Pegasus** : pas encore pilotées (les commandes de LiveChess sont
  consommées puis ignorées). Faisable : le Pegasus attend la forme
  `60 <len> 05 <vitesse> <répét> <lumin> <cases…> 00`.
* Si LiveChess identifie les modèles de carte par numéro de série,
  `--serial-nr XXXXX` permet d'en essayer un autre.

## 6. Tests hors matériel

```bash
~/.venv/pegasus/bin/python test_pegasus_bridge.py
```

Vérifie le suivi de partie (20 demi-coups avec roques et prises, en passant,
promotion, pièce en l'air, resynchro, reprise de deux coups d'un coup),
l'encadrement des messages DGT, le réassemblage des trames BLE sur plusieurs
notifications et la consommation des commandes multi-octets (pendule, LED).

## 7. Si LiveChess refuse le port quoi qu'il arrive

Le cœur du pont (BLE + suivi de partie) est indépendant de la sortie série. Deux
sorties de remplacement, sans LiveChess du tout :

* un faux serveur WebSocket compatible LiveChess sur `ws://localhost:1982`, pour
  que la page « DGT board » de lichess.org fonctionne telle quelle ;
* un lien direct à l'API Board de lichess (jeton personnel), sans navigateur.

Dis-moi si tu veux l'une des deux : c'est le même noyau, seule la façade change.

---

Protocole Pegasus d'après l'implémentation publique
[BoardKit](https://github.com/fianchettochess/BoardKit) (MIT) ; protocole série
DGT d'après [PicoChess](https://github.com/jromang/picochess) (GPL) et la
documentation publique DGT.

## Interface graphique

```bash
~/.venv/pegasus/bin/python pegasus_gui.py
```

Une fenêtre Tk (rien à installer de plus, Tk est dans la bibliothèque standard) :
bouton **Chercher** pour trouver le plateau, champ pour le jeton lichess — il est
prérempli s'il a déjà été retenu —, puis **Connecter**. La position s'affiche en
direct, les cases allumées sur le plateau le sont aussi à l'écran, les cases à
corriger apparaissent en rouge, et le journal défile en bas.

La **pendule** occupe le haut de la fenêtre : ton temps en bas, celui de
l'adversaire au-dessus, le camp au trait en blanc et l'autre en gris, et du
rouge sous vingt secondes. Elle s'égrène entre deux nouvelles de lichess.

Le plateau virtuel **se masque tout seul au début de chaque partie** — le
regarder gâcherait le jeu. La case « Afficher le plateau » le rappelle quand tu
en as besoin, et se décoche à nouveau à la partie suivante. Hors partie, il
montre la position de départ et marque en rouge ce qui manque encore : pratique
pour remettre les pièces. La ligne de détail
donne le compte : adversaire, couleur, nombre de demi-coups et **niveau de
batterie** du plateau, rafraîchi toutes les deux minutes et annoté « en charge »
quand le câble est branché.

## AppImage

```bash
chmod +x build-appimage.sh && ./build-appimage.sh
```

Produit un fichier unique et exécutable qui embarque Python, `bleak`, `chess` et
l'interface : ni installation, ni venv, ni sudo chez l'utilisateur. La
construction télécharge une image Python manylinux depuis GitHub, elle doit donc
tourner sur une machine avec accès réseau. Le Bluetooth passe par le BlueZ de
l'hôte — rien à embarquer, mais `bluetooth.service` doit tourner.

Puis l'installation dans le menu et le PATH, sans sudo :

```bash
./install.sh          # ou ./install.sh -u pour désinstaller
```

Le script délègue à [shortcut](https://github.com/Rimsoo/shortcut) s'il est
présent, et fait la même chose à la main sinon : un lien `~/.local/bin/pegasus`,
l'icône dans le thème, et une entrée de menu dont l'`Exec` pointe sur l'AppImage.

Le dépôt est aussi un paquet Python ordinaire :

```bash
pip install .        # fournit pegasus-bridge et pegasus-gui
```

## Ce que le plateau te dit

| moment | signal lumineux |
|---|---|
| début de partie | ta rangée s'allume d'un bloc — c'est de ce côté qu'il faut t'installer |
| tu soulèves une pièce | sa case s'allume, tu vois d'où elle vient |
| coup de l'adversaire | départ et arrivée s'allument jusqu'à ce que tu l'aies rejoué |
| ton coup accepté | un clin d'œil bref sur le trajet, dès que lichess l'a validé |
| ton coup refusé | battement rapide et insistant sur le trajet |
| plateau à corriger | les cases fautives s'allument |
| victoire | la rangée du gagnant s'allume deux fois |
| nulle | les deux rangées à la fois, les seize cases |

### Qui anime, le plateau ou nous

Le Pegasus n'allume pas la liste de cases qu'on lui envoie : il la **parcourt**,
case après case, à la cadence donnée par le troisième octet de la trame. C'est
lui qui anime.

C'est pour ça que les premières animations ne marchaient pas : on envoyait la
rangée puis on l'éteignait toutes les 130 ms pour la faire clignoter, ce qui
relançait le parcours depuis le début à chaque fois — seule la première case
avait le temps d'apparaître. Et avec la cadence d'origine (2), le parcours était
si lent qu'on n'en voyait que trois cases en deux secondes et demie.

La bonne façon est donc d'envoyer la liste **une fois**, à la bonne cadence, et
de ne plus y toucher. La trame d'une rangée ressemble à ceci :

```
60 0d 05 40 00 01 38 39 3a 3b 3c 3d 3e 3f 00
      │  │  │  └ luminosité      └ a1 b1 c1 d1 e1 f1 g1 h1
      │  │  └ répétitions
      │  └ cadence du parcours  ← 64 : si rapide que tout paraît simultané
      │                            8 : balayage visible ; 2 : beaucoup trop lent
      └ sous-commande « suite de cases »
```

La cadence retenue est **64**, partout : à ce rythme le parcours est assez
rapide pour que seize cases s'allument d'un bloc. C'est ce qui donne la nulle
sur les deux rangées à la fois, et c'est aussi ce qu'on veut pour le coup de
l'adversaire, dont le départ et l'arrivée doivent se voir d'un même coup d'œil.

L'ordre de la liste reste celui du parcours, donc il compte toujours : la nulle
entrelace les deux rangées (a1, h8, b1, g8…) et les noirs reçoivent h8→a8.
`--led-speed N` change la cadence — mets 8 si tu préfères voir le balayage —
et `--led-style chase` fait animer par le programme au lieu du plateau, utile
si un jour un firmware ne parcourt pas la liste.

### Régler les lumières à ta main

Bouton **Lumières** dans l'interface. Une jauge de vitesse et une de durée par
événement, un nombre de clignotements là où ça a un sens, et un bouton
**Essayer** par ligne :

| événement | vitesse | durée | clignotements |
|---|---|---|---|
| début de partie | cadence du parcours | durée d'allumage | nombre d'éclats |
| fin de partie | idem | durée d'un éclat | nombre d'éclats |
| coup de l'adversaire | idem | battement, **0 = reste allumé** | — |
| pièce soulevée | idem | battement, **0 = reste allumé** | — |
| coup accepté | idem | durée d'un clignotement | nombre |
| coup refusé | idem | durée d'un clignotement | nombre |
| recherche d'un adversaire | idem | durée d'un tour de l'anneau | — |

Les jauges agissent **en direct** : le tableau de réglages est celui-là même
qu'utilise la partie en cours, il n'y a donc rien à relancer. Déplace, clique
sur *Essayer*, recommence — le plateau rejoue l'animation sans qu'il faille
attendre la prochaine partie. *Début* et *Fin* ont deux boutons chacun, pour
voir aussi le côté noir et la nulle.

Pendant la recherche d'un adversaire, une lumière tourne au centre du plateau
— l'anneau c3-c6-f6-f3 — pour dire que ça cherche. Elle s'arrête dès que la
partie part, ou dès que tu reposes les pièces pour annuler.

*Enregistrer* écrit dans `~/.config/pegasus-bridge/leds.json`, *Valeurs
d'usine* revient au réglage d'origine sans rien écrire. En ligne de commande,
`--leds-file` désigne un autre fichier et `--led-speed N` impose la même
cadence partout, quoi que dise le fichier.

Les deux états qui durent — le coup de l'adversaire et la pièce soulevée —
restent allumés tant que la durée vaut 0. Dès qu'elle dépasse 0, ils battent à
ce rythme jusqu'à ce que la situation change, et une animation qui passe
par-dessus (un coup accepté, par exemple) met le battement en pause puis le
laisse reprendre.

La sous-commande `led` rejoue les animations retenues, puis propose cinq
variantes pour la nulle et six cadences à comparer :

```bash
pegasus_bridge.py led --address XX:XX:XX:XX:XX:XX
```

## Les boutons de partie

À droite, sous les pendules, six boutons actifs seulement pendant une partie :

| bouton | ce qu'il fait |
|---|---|
| **Nulle** | propose la nulle, ou accepte celle qu'on te propose |
| **Reprise** | demande à reprendre un coup, ou accepte la demande adverse |
| **+15 s** | ajoute quinze secondes à la pendule de *l'adversaire* |
| **Abandonner** | abandonne — une confirmation est demandée |
| **Annuler** | annule la partie (possible seulement avant le premier coup) |
| **Refuser** | refuse la proposition en cours, nulle ou reprise |

Un mot sur **+15 s** : il ne passe pas par la même route que les autres
(`/api/round/…` au lieu de `/api/board/…`) et relève d'un autre droit. Si ton
jeton n'a que `board:play`, lichess refusera et le journal te dira exactement
quoi ajouter. Les cinq autres marchent avec le jeton habituel.

## Lancer une partie depuis le plateau

Hors partie, plateau garni de la position de départ : soulève deux pièces
convenues et la partie se lance. Reposer les pièces annule la recherche —
l'API de lichess garde la recherche active tant que la connexion vit, la
fermer l'annule.

Les gestes vivent dans `~/.config/pegasus-bridge/gestures.json`, créé au
premier lancement avec trois exemples :

```json
{
  "gestes": [
    { "nom": "Rapide 10+15 classée", "cases": ["a1", "h8"],
      "action": "seek", "minutes": 10, "increment": 15, "classee": true },
    { "nom": "Blitz 5+0 amicale", "cases": ["b1", "g8"],
      "action": "seek", "minutes": 5, "increment": 0, "classee": false },
    { "nom": "Ordinateur niveau 4", "cases": ["c1", "f8"],
      "action": "ai", "niveau": 4, "minutes": 10, "increment": 0,
      "couleur": "white" }
  ]
}
```

| champ | rôle |
|---|---|
| `cases` | les cases à libérer, en position de départ uniquement |
| `action` | `seek` (adversaire humain) ou `ai` (ordinateur) |
| `minutes` / `increment` | la cadence |
| `classee` | partie classée ou amicale (`seek`) |
| `niveau` | force de l'ordinateur, 1 à 8 (`ai`) |
| `couleur` | `white`, `black` ou `random` |
| `variante` | `standard`, `chess960`, `antichess`… |
| `fourchette` | plage d'Elo des adversaires, ex. `"1500-1800"` |

Le même jeton suffit : `board:play` couvre la recherche d'adversaire **et** le
défi à l'ordinateur. Rien à changer.

Le bouton **Modifier les gestes** de l'interface ouvre un petit éditeur : liste
à gauche, formulaire à droite, avec les cases à lever, la cadence, le type de
partie et la couleur. Il refuse deux gestes sur les mêmes cases. Les gestes sont
relus au démarrage du pont — reconnecte-toi après les avoir changés.

Deux options : `--no-gestures` pour désactiver, `--gesture-delay` pour régler
le temps pendant lequel les pièces doivent rester levées (1,2 s par défaut —
c'est ce délai qui laisse soulever deux pièces sans qu'un geste plus court
parte en chemin).

## Et chess.com ?

Pas de chemin équivalent. L'API publique de chess.com est **en lecture seule** —
leur documentation le dit mot pour mot : « You cannot send game-moves or other
commands to Chess.com from this system ». Il n'existe pas d'API Board, pas de
jeton de jeu, pas d'OAuth pour jouer. Le seul chemin officiel pour un Pegasus
passe par l'application mobile DGT.

Sur ordinateur, la communauté contourne par une extension de navigateur qui lit
la position dans le DOM de la page et y injecte les coups — c'est exactement ce
que fait
[PegasusChessComChromeExtension](https://github.com/EdNekebno/PegasusChessComChromeExtension),
dont nous avons repris le protocole BLE. Ça fonctionne, mais ça casse à chaque
refonte de leur interface, et l'automatisation de la page sort de ce que leurs
conditions d'utilisation autorisent.
