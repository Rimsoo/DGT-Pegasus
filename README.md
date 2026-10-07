# Pegasus — jouer sur lichess avec un DGT Pegasus, sous Linux

DGT ne fournit qu'un `.deb` de LiveChess pour Linux, et LiveChess ne voit pas un
plateau connecté en Bluetooth : il énumère les ports série. Ce projet se passe
des deux. Il parle directement au Pegasus en BLE et à lichess par son API Board.

**Aucun sudo, aucun périphérique à créer, aucun LiveChess.** Tu poses tes pièces,
tu joues, les cases du coup adverse s'allument, les pendules tournent.

![L'interface pendant une partie](apercu_interface.png)

---

## Ce que ça fait

* **Jouer sur lichess depuis le plateau.** Les coups partent tout seuls, ceux de
  l'adversaire s'allument sur le plateau jusqu'à ce que tu les rejoues.
* **Lancer une partie sans toucher à l'ordinateur.** Soulève deux pièces
  convenues et la partie démarre.
* **Tout ce qui ne se fait pas en poussant une pièce** : abandon, nulle, reprise
  de coup, ajout de temps — six boutons.
* **Des animations lumineuses réglables** : début de partie, victoire, nulle,
  pièce soulevée, coup accepté, recherche d'adversaire.
* **Le mode LiveChess**, en annexe, pour qui y tient : le pont se fait passer
  pour une carte DGT sur un port série (annexe A).

Trois fichiers suffisent : `pegasus_bridge.py` (le moteur et la ligne de
commande), `pegasus_gui.py` (l'interface), `test_pegasus_bridge.py` (288
contrôles qui tournent sans plateau).

---

## Installation

### A. L'AppImage — rien à installer

```bash
chmod +x build-appimage.sh && ./build-appimage.sh
./install.sh                       # ./install.sh -u pour désinstaller
```

Le premier script produit un exécutable unique qui embarque Python, `bleak`,
`chess` et l'interface. Il télécharge une image Python manylinux, donc il lui
faut du réseau — une seule fois. Le Bluetooth passe par le BlueZ de la machine :
rien à embarquer, mais `bluetooth.service` doit tourner.

Le second installe sans sudo : une commande `pegasus` dans `~/.local/bin`,
l'icône dans le thème, une entrée dans le menu. Il délègue à
[shortcut](https://github.com/Rimsoo/shortcut) s'il est dans le PATH, et fait la
même chose à la main sinon.

### B. Depuis les sources

```bash
sudo pacman -S --needed bluez bluez-utils python tk
sudo systemctl enable --now bluetooth

python -m venv ~/.venv/pegasus
~/.venv/pegasus/bin/pip install bleak chess
```

Python 3.9 ou plus. Tk fait partie de la bibliothèque standard mais est souvent
dans un paquet à part (`tk` sur Arch, `python3-tk` sur Debian) — il n'est
nécessaire que pour l'interface, pas pour la ligne de commande.

Le dossier est aussi un paquet Python ordinaire :

```bash
pip install .        # fournit les commandes pegasus-bridge et pegasus-gui
```

### Le jeton lichess

Un jeton personnel avec la portée **`board:play`** :

<https://lichess.org/account/oauth/token/create?scopes[]=board:play&description=Pegasus%20bridge>

Colle-le dans l'interface et coche *retenir*, ou passe-le une fois en ligne de
commande avec `--save-token`. Il atterrit dans
`~/.config/pegasus-bridge/token`, en 0600.

Ce seul jeton couvre tout : jouer, chercher un adversaire, défier l'ordinateur,
abandonner, proposer nulle, reprendre un coup. Seul le bouton **+15 s** demande
davantage (voir plus bas).

---

## Démarrage

**Avec l'interface :**

```bash
pegasus                              # AppImage installée
~/.venv/pegasus/bin/python pegasus_gui.py    # depuis les sources
```

*Chercher* → choisis le plateau → *Connecter*. C'est tout.

**En ligne de commande :**

```bash
pegasus play --address XX:XX:XX:XX:XX:XX --token lip_xxxxx --save-token
pegasus play --address XX:XX:XX:XX:XX:XX     # les fois suivantes
```

Lance ensuite une partie sur lichess.org, ou depuis le plateau : elle est
détectée et suivie toute seule. Tu peux aussi te brancher au milieu d'une partie
en cours — le pont vérifie que la position concorde, il ne te fait pas tout
rejouer.

---

## L'interface

**Les pendules** occupent la droite : la tienne en bas, celle de l'adversaire
au-dessus. Le camp au trait est en blanc, l'autre en gris, et ça passe au rouge
sous vingt secondes. Elles s'égrènent entre deux nouvelles de lichess, y compris
pendant ta réflexion.

**Le plateau virtuel se masque tout seul au début de chaque partie** — le
regarder gâcherait le jeu. La case *Afficher le plateau* le rappelle quand tu en
as besoin, et se décoche à nouveau à la partie suivante. Hors partie, il montre
la position de départ et marque en rouge ce qui manque encore : pratique pour
remettre les pièces avant de commencer.

**La ligne de détail** donne l'adversaire, ta couleur, le nombre de demi-coups et
le **niveau de batterie** du plateau, rafraîchi toutes les deux minutes et
annoté « en charge » quand le câble est branché.

### Les boutons de partie

Six boutons, actifs seulement pendant une partie.

| bouton | ce qu'il fait |
|---|---|
| **Nulle** | propose la nulle, ou accepte celle qu'on te propose |
| **Reprise** | demande à reprendre un coup, ou accepte la demande adverse |
| **+15 s** | ajoute quinze secondes à la pendule de *l'adversaire* |
| **Abandonner** | abandonne — une confirmation est demandée |
| **Annuler** | annule la partie, possible seulement avant le premier coup |
| **Refuser** | refuse la proposition en cours, nulle ou reprise |

*Nulle* et *Reprise* servent aussi bien à proposer qu'à accepter : chez lichess
c'est la même route.

**+15 s** est le seul qui sorte du lot. Il passe par `/api/round/…` au lieu de
`/api/board/…`, ce qui relève d'un autre droit. Avec un jeton `board:play` seul,
lichess refusera, et le journal te dira d'ajouter `challenge:write`. Les cinq
autres marchent avec le jeton habituel.

### Lancer une partie depuis le plateau

Hors partie, plateau garni de la position de départ : soulève deux pièces
convenues, garde-les levées une seconde, la partie se lance. Pendant la
recherche, **une lumière tourne au centre du plateau**. Reposer les pièces
annule — l'API de lichess garde la recherche active tant que la connexion vit,
la fermer l'annule.

Le bouton **Modifier les gestes** ouvre un éditeur : liste à gauche, formulaire à
droite, avec les cases à lever, la cadence, le type de partie et la couleur. Il
refuse deux gestes sur les mêmes cases. Les gestes sont relus au démarrage du
pont — reconnecte-toi après les avoir changés.

Trois exemples sont écrits au premier lancement dans
`~/.config/pegasus-bridge/gestures.json` :

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
| `cases` | les cases à libérer ; elles doivent être occupées en position de départ |
| `action` | `seek` (adversaire humain) ou `ai` (ordinateur) |
| `minutes` / `increment` | la cadence |
| `classee` | partie classée ou amicale (`seek` seulement) |
| `niveau` | force de l'ordinateur, 1 à 8 (`ai` seulement) |
| `couleur` | `white`, `black` ou `random` |
| `variante` | `standard`, `chess960`, `antichess`, `atomic`… |
| `fourchette` | plage d'Elo des adversaires, ex. `"1500-1800"` |

`--no-gestures` désactive le tout ; `--gesture-delay` règle le temps pendant
lequel les pièces doivent rester levées (1,2 s par défaut — c'est ce délai qui
laisse soulever deux pièces sans qu'un geste plus court parte en chemin).

### Les réglages lumineux

Bouton **Lumières**. Une jauge de vitesse et une de durée par événement, un
nombre de clignotements là où ça a un sens, et un bouton *Essayer* par ligne.

![Les réglages lumineux](apercu_lumieres.png)

| événement | durée = | clignotements |
|---|---|---|
| début de partie | durée d'allumage | oui |
| fin de partie | durée d'un éclat | oui |
| coup de l'adversaire | battement, **0 = reste allumé** | — |
| pièce soulevée | battement, **0 = reste allumé** | — |
| coup accepté | durée d'un clignotement | oui |
| coup refusé | durée d'un clignotement | oui |
| recherche d'un adversaire | durée d'un tour de l'anneau | — |

Les jauges agissent **en direct** : le tableau de réglages n'est pas copié, c'est
celui-là même qu'utilise la partie en cours. Déplace, clique sur *Essayer*,
recommence — inutile d'attendre une fin de partie pour voir l'animation de fin.
*Début* et *Fin* ont deux boutons chacun, pour voir aussi le côté noir et la
nulle.

*Enregistrer* écrit dans `~/.config/pegasus-bridge/leds.json`, *Valeurs d'usine*
revient au réglage d'origine sans rien écrire.

Les deux états qui durent — coup de l'adversaire, pièce soulevée — restent
allumés tant que leur durée vaut 0. Au-delà, ils battent à ce rythme jusqu'à ce
que la situation change, et une animation qui passe par-dessus met le battement
en pause puis le laisse reprendre.

---

## Ce que le plateau te dit

| moment | signal lumineux |
|---|---|
| début de partie | ta rangée s'allume d'un bloc — c'est de ce côté qu'il faut t'installer |
| recherche d'un adversaire | une lumière tourne au centre, sur l'anneau c3-c6-f6-f3 |
| tu soulèves une pièce | sa case s'allume, tu vois d'où elle vient |
| coup de l'adversaire | départ et arrivée s'allument jusqu'à ce que tu l'aies rejoué |
| ton coup accepté | un clin d'œil bref sur le trajet, dès que lichess l'a validé |
| ton coup refusé | battement rapide et insistant sur le trajet |
| plateau à corriger | les cases fautives s'allument |
| victoire | la rangée du gagnant s'allume deux fois |
| nulle | les deux rangées à la fois, les seize cases |

### Qui anime : le plateau, pas nous

Le Pegasus n'allume pas la liste de cases qu'on lui envoie : il la **parcourt**,
case après case, à la cadence donnée par le troisième octet de la trame.

```
60 0d 05 40 00 01 38 39 3a 3b 3c 3d 3e 3f 00
      │  │  │  │  └ les cases : a1 b1 c1 d1 e1 f1 g1 h1
      │  │  │  └ luminosité
      │  │  └ répétitions
      │  └ cadence du parcours  ← 64 : si rapide que tout paraît simultané
      │                            8 : balayage visible ; 2 : beaucoup trop lent
      └ sous-commande « suite de cases »
```

Ignorer ça coûte cher. Les premières animations faisaient clignoter une rangée
en l'éteignant toutes les 130 ms : chaque extinction relançait le parcours
depuis le début, et seule la première case avait le temps d'apparaître. D'où le
symptôme « seule a1 clignote », qui n'avait rien d'un problème de LED.

La bonne façon est d'envoyer la liste **une fois**, à la bonne cadence, et de ne
plus y toucher. L'ordre de la liste donne le sens du parcours : les noirs
reçoivent h8→a8 pour que la lumière parte de leur gauche comme celle des blancs
part de la leur, et la nulle entrelace les deux rangées (a1, h8, b1, g8…) pour
faire avancer deux lumières de front.

`--led-speed N` impose la même cadence partout, quoi que disent les réglages ;
`--led-style chase` fait animer par le programme au lieu du plateau, au cas où
un firmware ne parcourrait pas la liste.

**Une seule couleur.** Cinq paramètres dans la trame, aucun n'est une couleur.
L'octet de luminosité, lui, reste un point d'interrogation : l'essai 10 de la
sonde `led` le teste à six intensités.

---

## Les fichiers de configuration

Tout vit dans `~/.config/pegasus-bridge/` (ou `$XDG_CONFIG_HOME`).

| fichier | contenu | édité par |
|---|---|---|
| `token` | le jeton lichess, en 0600 | la case *retenir*, ou `--save-token` |
| `gestures.json` | les gestes de lancement | le bouton *Modifier les gestes* |
| `leds.json` | les réglages lumineux | le bouton *Lumières* |

Chacun a son option pour pointer ailleurs : `--token`, `--gestures`,
`--leds-file`.

---

## La ligne de commande

```
pegasus_bridge.py {scan,probe,watch,diag,raw,led,play,serve}
```

| sous-commande | à quoi ça sert |
|---|---|
| `scan` | lister les périphériques BLE et repérer le Pegasus |
| `probe` | vérifier la liaison et voir l'occupation en direct |
| `watch` | n'afficher que les changements de capteurs |
| `diag` | interroger la carte : verrouillage, autorisation, version |
| `raw` | console brute, envoyer des octets à la main |
| `led` | sonder les LED : animations retenues, variantes, cadences |
| `play` | **jouer sur lichess** — le mode normal |
| `serve` | se faire passer pour une carte DGT sur un port série (annexe A) |

Les options les plus utiles de `play` :

| option | défaut | rôle |
|---|---|---|
| `--address` | — | adresse BLE du plateau ; sans elle, il est cherché |
| `--token` | `$LICHESS_TOKEN`, puis le fichier | le jeton lichess |
| `--save-token` | — | retenir le jeton pour les fois suivantes |
| `--flip` | — | plateau tourné de 180°, noirs devant toi |
| `--game` | — | rejoindre une partie précise par son identifiant |
| `--promotion` | `q` | pièce supposée à la promotion |
| `--settle` | 0,15 s | stabilisation avant d'analyser une position |
| `--gesture-delay` | 1,2 s | temps pendant lequel les pièces restent levées |
| `--no-leds` / `--no-gestures` | — | couper les lumières ou les gestes |
| `--led-speed` / `--leds-file` | — | cadence imposée, autre fichier de réglages |
| `--show-board` | — | afficher le plateau en continu dans le terminal |
| `-v` / `--trace` | — | journal détaillé / toutes les notifications BLE |

`-v` et `--debug` s'acceptent avant **comme** après la sous-commande.

---

## Quand ça ne marche pas

### 1. Calibrer — de loin la cause la plus fréquente

La détection du Pegasus est **inductive**, pas magnétique : une détection de
métal à très courte portée. Un aimant ne prouve donc rien, et surtout le plateau
doit être **calibré** — ça ne se fait pas tout seul à l'allumage, contrairement
à ce que les LED du démarrage laissent croire.

Sans calibration, le plateau renvoie `01` sur les 64 cases (« tout occupé »), ne
signale jamais aucun changement, et se comporte exactement comme s'il était en
panne. **Calibre d'abord, débogue ensuite.**

Si le plateau reste muet, DGT publie une remise à zéro usine : charge complète,
plateau éteint et vide, puis appui long sur on/off **jusqu'à ce que d4, d5, e4 et
e5 clignotent rapidement et simultanément**. Si ces quatre cases ne clignotent
pas, la carte capteurs/LED ne répond pas et c'est un défaut matériel.

### 2. La liaison BLE

```bash
pegasus_bridge.py scan
```

Une ligne doit porter `<-- Nordic UART (Pegasus ?)`. Relève l'adresse, puis :

```bash
pegasus_bridge.py probe --address XX:XX:XX:XX:XX:XX -v
```

Le handshake doit afficher le numéro de série et `firmware 1.x (major=1 ->
Pegasus confirmé)`, puis l'échiquier se met à jour quand tu bouges une pièce.
Rien ne sert de continuer avant que cette étape marche.

### 3. La carte répond mais ne livre pas l'occupation

Le handshake passe — numéro de série, firmware, trademark — mais le dump annonce
64 cases occupées et aucun changement n'arrive.

```bash
pegasus_bridge.py probe --address XX:XX:XX:XX:XX:XX -v --trace
pegasus_bridge.py raw --address XX:XX:XX:XX:XX:XX --led-test
```

| indice dans la trace | cause | correctif |
|---|---|---|
| dump constant `01` × 64 | plateau non calibré (voir 1), ou carte non déverrouillée | calibrer ; `--handshake ext` |
| le nombre de cases occupées **baisse** quand tu ajoutes des pièces | codage inversé | `--invert` |
| dump figé mais plausible | la carte ne pousse rien d'elle-même | `--poll 0.3` |

`--led-test` allume une rangée trois secondes : c'est la preuve physique que la
carte reçoit tes commandes. Si les LED s'allument, le problème est du côté des
données, pas de la liaison.

Deux séquences d'initialisation coexistent dans les implémentations publiques :

* `--handshake ext` — celle de l'extension Chrome : clé développeur **en toute
  première écriture**, puis `40`, `42`, `44`. C'est le défaut, et c'est celle qui
  marche sur le Pegasus.
* `--handshake dgt` — celle du pilote Dart `mono424/dgtdriver` : `40`, `45`,
  `4d`, *puis* la clé. Si la carte n'accepte la clé qu'en première écriture,
  cette séquence renvoie un plateau factice.

### 4. Un geste ne lance pas de partie

Le journal te dit pourquoi, une fois par situation :

```
geste : le plateau n'est pas en position de depart : e4 ne devrait pas etre occupee
geste : cases levees : a1, d1 — aucun geste ne correspond (connus : a1+h8 : …)
geste : les gestes sont desactives (--no-gestures)
```

Un geste ne part que si le plateau porte **exactement** la position initiale
privée des cases du geste : une pièce oubliée au milieu suffit à tout bloquer.

### 5. Le plateau est à l'envers

Si les pièces blanches apparaissent en haut, `--flip` (ou la case *Plateau
tourné* dans l'interface). Attention à ne pas confondre avec une inversion
gauche-droite : `--flip` fait tourner de 180°, il n'y a pas de miroir seul.

---

## Les tests

```bash
python test_pegasus_bridge.py
```

288 contrôles, 33 sections, sans aucun matériel. Ils couvrent le suivi d'une
partie complète (roques, prises, prise en passant, promotion, pièce en l'air,
resynchronisation, reprise de coups), l'orientation des cases, le réassemblage
des trames BLE sur plusieurs notifications, le protocole série et le mode bus
DGT, les animations lumineuses et leurs réglages, les gestes de lancement, les
actions de partie et la pendule.

C'est là qu'ont été attrapées la plupart des régressions de ce projet : si tu
touches au code, lance-les.

---

## Annexe A — le mode LiveChess (`serve`)

Si tu tiens à l'interface LiveChess ou à son API, le pont peut se faire passer
pour une carte DGT branchée en série. C'est plus lourd — il faut `tty0tty` et
`sudo` — et les LED ne fonctionnent pas, LiveChess ne connaissant pas le coup de
l'adversaire. Le mode `play` est meilleur en tout point ; cette annexe est là
pour mémoire.

**Le pty ne suffit pas.** Un pseudo-terminal n'a pas de lignes de modem : le
noyau ne gère ni DTR, ni DSR, ni CTS sur `/dev/pts/*`, et `TIOCMSET` y échoue.
jSSC, qu'utilise LiveChess, pose DTR/RTS à l'ouverture : il échoue
silencieusement, ouvre le port et n'écrit jamais rien. Symptôme exact : le port
apparaît dans la liste, LiveChess dit `Opening port`, et le pont ne reçoit pas un
octet.

**La solution est `tty0tty`**, un émulateur de câble null-modem qui crée de vrais
tty noyau appariés, avec les lignes de modem émulées :

```bash
yay -S tty0tty-dkms          # ou make depuis github.com/lcgamboa/tty0tty
sudo modprobe tty0tty
ls -l /dev/tnt*              # tnt0<->tnt1, tnt2<->tnt3, ...

sudo ~/.venv/pegasus/bin/python pegasus_bridge.py serve \
     --address XX:XX:XX:XX:XX:XX --device /dev/tnt1 --link /dev/ttyUSB0 --debug
```

Le nom `tnt0` ne correspond à aucun motif reconnu par les bibliothèques série
(`ttyS`, `ttyUSB`, `ttyACM`, `rfcomm`…), donc LiveChess ne le listerait pas :
`--link` lui présente un lien au bon nom. Le pont tient `/dev/tnt1`, ouvre les
droits sur les deux bouts, et pointe `/dev/ttyUSB0` vers `/dev/tnt0`.

**Et surtout, le mode bus.** LiveChess est un logiciel de tournoi : il ne détecte
pas les cartes en mode simple, mais en mode bus DGT, le protocole multi-cartes.
Sa séquence de détection, lue au `--debug` :

```
40 40 40      reset
47            trademark        -> doit contenir « Digital Game Technology »
46            adresse de bus   -> 2 octets de 7 bits
4a            passage en mode bus
8b 00 00 0b   DGT_BUS_SEND_VERSION vers l'adresse 0 (diffusion)
```

Une carte qui ne répond pas à cette dernière trame n'existe pas pour LiveChess,
même si tout le reste est parfait. Le pont implémente le mode bus complet : ping,
dump, version, début de partie, changements et rejeu. Les commandes du mode bus
font 4 octets — `[code|0x80, adresse MSB, adresse LSB, somme]` — et se
distinguent du mode simple par le bit 7.

Ensuite : LiveChess sur `http://localhost:1982`, puis sur lichess.org
*Préférences → DGT board*, qui se connecte à son WebSocket.

Deux détails qui coûtent cher à retrouver : la réponse au trademark **doit**
contenir `Digital Game Technology\r\nCopyright (c)`, et le pty étant recréé à
chaque démarrage du pont, LiveChess doit toujours être lancé *après* lui.

---

## Annexe B — ce qu'on a appris du protocole

* **BLE** : Nordic UART Service `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`, écriture
  sur `…0002`, notifications sur `…0003`.
* **Clé développeur** : `63 07 be f5 ae dd a9 5f 00`, en **toute première
  écriture**. Sans elle, la carte répond `7f` sur les 64 cases ; avec elle,
  `a5 01` puis de vraies données.
* **Trames plateau → hôte** : `[id|0x80, lenHi, lenLo, charge…]`, longueur totale
  `(lenHi<<7)|lenLo`, en-tête de 3 octets compris. Une trame peut être coupée sur
  plusieurs notifications : il faut la réassembler.
* **Index des cases** : 0 = a8 … 63 = h1, soit l'inverse vertical de la
  convention habituelle (0 = a1). Confondre les deux donne un plateau au miroir
  qui ressemble à une inversion de couleurs.
* **Occupation seule.** Le plateau dit quelles cases sont occupées, jamais par
  quoi. L'identité des pièces est reconstruite en suivant la partie : occupation
  stable comparée aux coups légaux, en profondeur 1 puis 2. Prises, roques, prise
  en passant et promotions sont couverts — et chacun a cassé au moins une fois
  avant d'avoir son test.
* **Batterie** (message `0xA0`) : pourcentage, heures et minutes restantes, temps
  d'allumage, temps de veille, et un octet d'état dont le bit 0 dit « en charge »
  et le bit 1 « sur batterie ».
* **LED** : voir « Qui anime » plus haut.

---

## Et chess.com ?

Pas de chemin équivalent. L'API publique de chess.com est **en lecture seule** —
leur documentation le dit mot pour mot : *« You cannot send game-moves or other
commands to Chess.com from this system »*. Pas d'API Board, pas de jeton de jeu,
pas d'OAuth pour jouer. Le seul chemin officiel pour un Pegasus passe par
l'application mobile DGT.

Sur ordinateur, la communauté contourne par une extension de navigateur qui lit
la position dans le DOM et y injecte les coups — c'est ce que fait
[PegasusChessComChromeExtension](https://github.com/EdNekebno/PegasusChessComChromeExtension),
dont vient le protocole BLE utilisé ici. Ça marche, mais ça casse à chaque
refonte de leur interface, et l'automatisation de la page sort de ce que leurs
conditions d'utilisation autorisent.

---

## Crédits

Protocole Pegasus d'après l'implémentation publique
[BoardKit](https://github.com/fianchettochess/BoardKit) (MIT) et
[PegasusChessComChromeExtension](https://github.com/EdNekebno/PegasusChessComChromeExtension).
Protocole série DGT d'après [PicoChess](https://github.com/jromang/picochess)
(GPL) et la documentation publique DGT. API Board de
[lichess.org](https://lichess.org/api).
