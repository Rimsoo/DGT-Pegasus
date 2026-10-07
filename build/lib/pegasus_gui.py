#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pegasus_gui.py — interface graphique du pont DGT Pegasus <-> lichess.

Une fenetre Tk : on choisit le plateau, on colle son jeton lichess, on clique
sur Connecter. La position du plateau s'affiche en direct, les cases allumees
sont mises en evidence, et le journal defile en bas.

Aucune dependance en plus du pont : Tk fait partie de la bibliotheque standard.
Le moteur tourne dans un fil a lui, avec sa propre boucle asyncio ; l'interface
ne fait que lire une file de messages.
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import queue
import sys
import time
import threading
from typing import Optional

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:  # Python sans Tk : on le dira clairement au lieu de casser
    tk = None
    ttk = None
    messagebox = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pegasus_bridge as pb  # noqa: E402

try:
    import chess
except ImportError:  # pragma: no cover
    chess = None

# On utilise partout les glyphes PLEINS (ceux des noirs) et on les colore :
# les glyphes ajoures des blancs se perdent sur les cases claires.
GLYPHS = {
    "p": "♟", "n": "♞", "b": "♝",
    "r": "♜", "q": "♛", "k": "♚",
}
WHITE_PIECE = "#fdfdfd"
BLACK_PIECE = "#1a1a1a"

LIGHT, DARK = "#f0d9b5", "#b58863"
LIT = "#7fd1c4"          # case allumee sur le plateau
WRONG = "#e08b7a"        # case a corriger
BG = "#2b2b2b"
FG = "#e8e8e8"
CELL = 52


class Engine:
    """Le pont, dans son fil, pilote par des messages."""

    def __init__(self, outbox: "queue.Queue") -> None:
        self.outbox = outbox
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.task: Optional[asyncio.Task] = None
        # La partie en cours, quand il y en a une : c'est par elle que
        # passent les essais de reglage et les actions (abandon, nulle...).
        self.leds = None
        self.session = None

    # -- cycle de vie du fil ---------------------------------------------
    def _run_loop(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def ensure_thread(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        while self.loop is None:  # le temps que la boucle existe
            threading.Event().wait(0.01)

    def submit(self, coro) -> "asyncio.Future":
        self.ensure_thread()
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # -- actions ----------------------------------------------------------
    async def _scan(self) -> None:
        from bleak import BleakScanner
        self.outbox.put(("log", "Recherche des plateaux BLE (10 s)…"))
        found = await BleakScanner.discover(timeout=10.0, return_adv=True)
        boards = []
        for dev, adv in found.values():
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            name = dev.name or ""
            if pb.NUS_SERVICE in uuids or "pegas" in name.lower():
                boards.append((dev.address, name or "(sans nom)"))
        self.outbox.put(("boards", boards))
        if not boards:
            self.outbox.put(("log", "Aucun plateau trouvé. Est-il allumé ?"))

    def scan(self) -> None:
        self.submit(self._scan())

    def start(self, address: str, token: str, save: bool, flip: bool,
              led_settings: Optional[dict] = None) -> None:
        args = _default_args()
        args.address = address or None
        args.token = token or None
        args.save_token = bool(save and token)
        args.flip = flip
        # Le meme dictionnaire que celui des jauges : bouger une jauge agit
        # donc en direct, sans reconnexion ni message a faire circuler.
        args.led_settings = led_settings

        def on_update(session, tracker, event):
            self.leds = session.leds
            self.session = session
            self.outbox.put(("state", _snapshot(session, tracker)))

        async def runner():
            try:
                await pb.cmd_play(args, on_update=on_update)
            except SystemExit as exc:
                self.outbox.put(("log", str(exc)))
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.outbox.put(("log", f"Erreur : {exc}"))
            finally:
                self.outbox.put(("stopped", None))

        self.task = None
        future = self.submit(runner())
        self.outbox.put(("started", None))
        self._future = future

    def test_led(self, kind: str) -> None:
        """Rejoue une animation sur le plateau, pour regler les jauges."""
        leds = self.leds
        if leds is None or not (self.thread and self.thread.is_alive()):
            self.outbox.put(("log", "Connecte le plateau pour essayer les "
                                    "lumières."))
            return

        async def go():
            leds.demo(kind)

        self.submit(go())

    def game_action(self, nom: str, seconds: int = 15) -> None:
        """Abandon, nulle, reprise, ajout de temps : c'est lichess qui tranche."""
        session = self.session
        if session is None or not (self.thread and self.thread.is_alive()):
            self.outbox.put(("log", "Aucune partie en cours."))
            return
        self.submit(session.action(nom, seconds))

    def stop(self) -> None:
        self.leds = None
        self.session = None
        fut = getattr(self, "_future", None)
        if fut is not None and not fut.done():
            self.loop.call_soon_threadsafe(fut.cancel)


def _default_args():
    """Les memes valeurs par defaut que la ligne de commande."""
    parser = pb.build_parser()
    return parser.parse_args(["play"])


def _snapshot(session, tracker) -> dict:
    en_partie = session.game_id is not None and not session.finished
    if en_partie:
        pieces = {sq: p.symbol() for sq, p in tracker.pieces.items()}
        fautives = tuple(sum(tracker.mismatch(), [])) if not tracker.in_sync else ()
    else:
        # Hors partie, le suivi n'a plus de sens : on montre la position de
        # depart, et on marque en rouge ce qui manque encore sur le plateau.
        depart = chess.Board().piece_map()
        pieces = {sq: p.symbol() for sq, p in depart.items()}
        fautives = tuple(sorted((set(depart) - tracker.occ)
                                | (tracker.occ - set(depart))))
    return {
        "pieces": pieces,
        "playing": en_partie,
        "blanc": session.remaining(chess.WHITE),
        "noir": session.remaining(chess.BLACK),
        "trait": (tracker.logic.turn if en_partie else None),
        "pris_a": time.monotonic(),
        "moi": session.color,
        "lit": tuple(session.lit),
        "wrong": fautives,
        "status": session.status_line(),
        "account": session.account,
        "opponent": session.opponent,
        "game": session.game_id or "",
        "color": ("blancs" if session.color else "noirs")
                 if session.color is not None else "",
        "moves": len(session.moves),
        "battery": session.link.battery_text() if session.link else "",
        "gestures": (session.launcher.describe()
                     if getattr(session, "launcher", None) else []),
        "seeking": bool(getattr(session, "launcher", None)
                        and session.launcher.pending),
        "flip": session.color == (chess.BLACK if chess else False),
    }


class GuiLogHandler(logging.Handler):
    def __init__(self, outbox: "queue.Queue") -> None:
        super().__init__()
        self.outbox = outbox

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.outbox.put(("log", self.format(record)))
        except Exception:
            pass


class GestureEditor(tk.Toplevel):
    """Petite fenetre pour composer les gestes sans ouvrir de JSON."""

    CASES = [f"{c}{r}" for r in (1, 2, 7, 8) for c in "abcdefgh"]

    def __init__(self, parent: "App") -> None:
        super().__init__(parent)
        self.parent = parent
        self.title("Gestes de lancement")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.gestes = pb.load_gestures()
        self.index: Optional[int] = None

        gauche = ttk.Frame(self, padding=10)
        gauche.grid(row=0, column=0, sticky="ns")
        ttk.Label(gauche, text="Gestes").pack(anchor="w")
        self.liste = tk.Listbox(gauche, width=30, height=12, bg="#1e1e1e",
                                fg=FG, relief="flat",
                                selectbackground="#3a6ea5")
        self.liste.pack()
        self.liste.bind("<<ListboxSelect>>", self._select)
        barre = ttk.Frame(gauche)
        barre.pack(fill="x", pady=(6, 0))
        ttk.Button(barre, text="Nouveau", command=self._new).pack(side="left")
        ttk.Button(barre, text="Supprimer",
                   command=self._delete).pack(side="left", padx=4)

        droite = ttk.Frame(self, padding=10)
        droite.grid(row=0, column=1, sticky="n")

        def ligne(rang, texte, widget):
            ttk.Label(droite, text=texte).grid(row=rang, column=0, sticky="w",
                                               pady=3)
            widget.grid(row=rang, column=1, sticky="w", pady=3, padx=(8, 0))

        self.nom = ttk.Entry(droite, width=26)
        ligne(0, "Nom", self.nom)

        cases = ttk.Frame(droite)
        self.case1 = ttk.Combobox(cases, values=self.CASES, width=5,
                                  state="readonly")
        self.case2 = ttk.Combobox(cases, values=["(aucune)"] + self.CASES,
                                  width=8, state="readonly")
        self.case1.pack(side="left")
        ttk.Label(cases, text=" + ").pack(side="left")
        self.case2.pack(side="left")
        ligne(1, "Pieces a lever", cases)

        self.action = ttk.Combobox(droite, width=24, state="readonly",
                                   values=["Chercher un adversaire",
                                           "Jouer contre l'ordinateur"])
        self.action.bind("<<ComboboxSelected>>", lambda _e: self._refresh_fields())
        ligne(2, "Action", self.action)

        cadence = ttk.Frame(droite)
        self.minutes = ttk.Spinbox(cadence, from_=0, to=180, width=5)
        self.increment = ttk.Spinbox(cadence, from_=0, to=180, width=5)
        self.minutes.pack(side="left")
        ttk.Label(cadence, text=" min  +  ").pack(side="left")
        self.increment.pack(side="left")
        ttk.Label(cadence, text=" s").pack(side="left")
        ligne(3, "Cadence", cadence)

        self.classee = tk.BooleanVar(value=False)
        self.case_classee = ttk.Checkbutton(droite, text="partie classee",
                                            variable=self.classee)
        ligne(4, "", self.case_classee)

        self.niveau = ttk.Spinbox(droite, from_=1, to=8, width=5)
        ligne(5, "Niveau (1-8)", self.niveau)

        self.couleur = ttk.Combobox(droite, width=12, state="readonly",
                                    values=["au hasard", "blancs", "noirs"])
        ligne(6, "Couleur", self.couleur)

        self.variante = ttk.Combobox(droite, width=14, state="readonly",
                                     values=["standard", "chess960",
                                             "antichess", "atomic", "crazyhouse",
                                             "horde", "kingOfTheHill",
                                             "racingKings", "threeCheck"])
        ligne(7, "Variante", self.variante)

        bas = ttk.Frame(droite)
        bas.grid(row=8, column=0, columnspan=2, pady=(12, 0), sticky="e")
        ttk.Button(bas, text="Appliquer", command=self._apply).pack(side="left")
        ttk.Button(bas, text="Enregistrer et fermer",
                   command=self._save).pack(side="left", padx=6)

        self.info = ttk.Label(self, text="", padding=(10, 0, 10, 8))
        self.info.grid(row=1, column=0, columnspan=2, sticky="w")

        self._fill_list()
        if self.gestes:
            self.liste.selection_set(0)
            self._select()

    # -- liste ------------------------------------------------------------
    def _fill_list(self) -> None:
        self.liste.delete(0, "end")
        for g in self.gestes:
            cases = "+".join(g.get("cases", []))
            self.liste.insert("end", f"{cases}  {g.get('nom', '?')}")

    def _select(self, _event=None) -> None:
        sel = self.liste.curselection()
        if not sel:
            return
        self.index = sel[0]
        g = self.gestes[self.index]
        self.nom.delete(0, "end"); self.nom.insert(0, g.get("nom", ""))
        cases = list(g.get("cases", []))
        self.case1.set(cases[0] if cases else "a1")
        self.case2.set(cases[1] if len(cases) > 1 else "(aucune)")
        self.action.set("Jouer contre l'ordinateur"
                        if g.get("action") == "ai" else "Chercher un adversaire")
        self.minutes.delete(0, "end"); self.minutes.insert(0, g.get("minutes", 10))
        self.increment.delete(0, "end")
        self.increment.insert(0, g.get("increment", 0))
        self.classee.set(bool(g.get("classee", False)))
        self.niveau.delete(0, "end"); self.niveau.insert(0, g.get("niveau", 1))
        self.couleur.set({"white": "blancs", "black": "noirs"}.get(
            g.get("couleur", "random"), "au hasard"))
        self.variante.set(g.get("variante", "standard"))
        self._refresh_fields()

    def _refresh_fields(self) -> None:
        ordinateur = self.action.get().startswith("Jouer")
        self.niveau.configure(state="normal" if ordinateur else "disabled")
        self.case_classee.configure(state="disabled" if ordinateur else "normal")

    def _new(self) -> None:
        self.gestes.append({"nom": "Nouveau geste", "cases": ["a1", "h8"],
                            "action": "seek", "minutes": 10, "increment": 0,
                            "classee": False})
        self._fill_list()
        self.liste.selection_clear(0, "end")
        self.liste.selection_set("end")
        self._select()

    def _delete(self) -> None:
        if self.index is None:
            return
        del self.gestes[self.index]
        self.index = None
        self._fill_list()
        self.info.configure(text="Geste supprime. Pense a enregistrer.")

    # -- edition ----------------------------------------------------------
    def _collect(self) -> Optional[dict]:
        cases = [self.case1.get()]
        if self.case2.get() and self.case2.get() != "(aucune)":
            cases.append(self.case2.get())
        if len(set(cases)) != len(cases):
            self.info.configure(text="Les deux cases doivent etre differentes.")
            return None
        ordinateur = self.action.get().startswith("Jouer")
        try:
            minutes = float(self.minutes.get())
            increment = int(self.increment.get())
            niveau = int(self.niveau.get())
        except ValueError:
            self.info.configure(text="Cadence ou niveau illisible.")
            return None
        geste = {
            "nom": self.nom.get().strip() or "Sans nom",
            "cases": cases,
            "action": "ai" if ordinateur else "seek",
            "minutes": minutes,
            "increment": increment,
            "couleur": {"blancs": "white", "noirs": "black"}.get(
                self.couleur.get(), "random"),
            "variante": self.variante.get() or "standard",
        }
        if ordinateur:
            geste["niveau"] = niveau
        else:
            geste["classee"] = bool(self.classee.get())
        return geste

    def _apply(self) -> bool:
        geste = self._collect()
        if geste is None:
            return False
        if self.index is None:
            self.gestes.append(geste)
            self.index = len(self.gestes) - 1
        else:
            self.gestes[self.index] = geste
        self._fill_list()
        self.liste.selection_clear(0, "end")
        self.liste.selection_set(self.index)
        self.info.configure(text="Modifie. Enregistre pour que ce soit pris "
                                 "en compte.")
        return True

    def _save(self) -> None:
        if self.liste.curselection():
            self._apply()
        doublons = {}
        for g in self.gestes:
            cle = frozenset(g.get("cases", []))
            if cle in doublons:
                self.info.configure(
                    text=f"Deux gestes utilisent {'+'.join(sorted(cle))}.")
                return
            doublons[cle] = g
        try:
            os.makedirs(os.path.dirname(pb.GESTURES_PATH), exist_ok=True)
            with open(pb.GESTURES_PATH, "w", encoding="utf-8") as handle:
                json.dump({"gestes": self.gestes}, handle, indent=2,
                          ensure_ascii=False)
        except OSError as exc:
            self.info.configure(text=f"Enregistrement impossible : {exc}")
            return
        self.parent._log(f"gestes enregistres ({len(self.gestes)}) dans "
                         f"{pb.GESTURES_PATH}")
        self.parent._log("reconnecte-toi pour qu'ils soient relus.")
        self.destroy()


class LedEditor(tk.Toplevel):
    """Les jauges des lumieres, avec un bouton d'essai par ligne.

    Les jauges agissent sur le meme dictionnaire que la partie en cours : on
    deplace, on clique sur Essayer, on voit le resultat sur le plateau. Rien
    n'est fige tant qu'on n'a pas enregistre, et « Valeurs d'usine » remet
    tout d'aplomb.
    """

    # Pour chaque evenement : les champs a montrer, et ce que veut dire la
    # duree. Un etat qui dure (le coup adverse, la piece soulevee) n'a pas de
    # nombre de clignotements : il bat, ou il reste allume.
    # Les libelles du pont sont sans accents (il ne sert que la console) ;
    # dans une fenetre on les ecrit correctement.
    NOMS = {
        "debut": "Début de partie",
        "fin": "Fin de partie (victoire, nulle)",
        "adverse": "Coup de l'adversaire",
        "levee": "Pièce soulevée",
        "confirme": "Coup accepté",
        "refuse": "Coup refusé",
        "recherche": "Recherche d'un adversaire",
    }

    RANGEES = [
        ("debut", "durée d'allumage", True, [("Essayer", "debut"),
                                             ("côté noir", "debut-noir")]),
        ("fin", "durée d'un éclat", True, [("Victoire", "fin"),
                                           ("Nulle", "fin-nulle")]),
        ("adverse", "battement (0 = reste allumé)", False,
         [("Essayer", "adverse")]),
        ("levee", "battement (0 = reste allumé)", False,
         [("Essayer", "levee")]),
        ("confirme", "durée d'un clignotement", True, [("Essayer", "confirme")]),
        ("refuse", "durée d'un clignotement", True, [("Essayer", "refuse")]),
        ("recherche", "durée d'un tour de l'anneau", False,
         [("Essayer", "recherche"), ("Stop", "stop")]),
    ]

    def __init__(self, parent: "App") -> None:
        super().__init__(parent)
        self.parent = parent
        self.title("Réglages lumineux")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.cfg = parent.led_settings          # le dictionnaire vivant
        self.vars: dict = {}

        cadre = ttk.Frame(self, padding=12)
        cadre.grid(row=0, column=0)

        for col, titre in enumerate(("Événement", "Vitesse", "Durée",
                                     "Clignotements", "")):
            ttk.Label(cadre, text=titre, font=("", 9, "bold")).grid(
                row=0, column=col, sticky="w", padx=(0, 10), pady=(0, 6))

        for rang, (nom, legende, repetable, essais) in enumerate(self.RANGEES,
                                                                 start=1):
            ttk.Label(cadre, text=self.NOMS.get(nom, nom)).grid(
                row=rang, column=0, sticky="w", pady=5, padx=(0, 10))
            self.vars[nom] = {}
            self._jauge(cadre, rang, 1, nom, "vitesse", 1, 127,
                        "cadence du parcours : 64 allume tout d'un bloc, "
                        "8 fait un balayage visible")
            self._jauge(cadre, rang, 2, nom, "duree", 0.0, 5.0, legende)
            if repetable:
                self._compteur(cadre, rang, 3, nom)
            else:
                ttk.Label(cadre, text="—").grid(row=rang, column=3)

            boutons = ttk.Frame(cadre)
            boutons.grid(row=rang, column=4, sticky="w", padx=(10, 0))
            for texte, kind in essais:
                ttk.Button(boutons, text=texte, width=9,
                           command=lambda k=kind: self.parent.engine.test_led(k)
                           ).pack(side="left", padx=2)

        self.info = ttk.Label(cadre, text="Déplace, clique sur Essayer, "
                                          "recommence. Le plateau doit être "
                                          "connecté.",
                              wraplength=640, justify="left")
        self.info.grid(row=len(self.RANGEES) + 1, column=0, columnspan=5,
                       sticky="w", pady=(12, 0))

        bas = ttk.Frame(cadre)
        bas.grid(row=len(self.RANGEES) + 2, column=0, columnspan=5,
                 sticky="e", pady=(10, 0))
        ttk.Button(bas, text="Valeurs d'usine",
                   command=self._reset).pack(side="left")
        ttk.Button(bas, text="Enregistrer",
                   command=self._save).pack(side="left", padx=6)
        ttk.Button(bas, text="Fermer", command=self.destroy).pack(side="left")

    # -- construction des lignes -----------------------------------------
    def _jauge(self, cadre, rang, col, nom, champ, bas, haut, legende) -> None:
        """Une jauge, sa valeur chiffree, et le report dans les reglages.

        Le report passe par une surveillance de la variable plutot que par le
        « command » de l'echelle : celui-ci ne se declenche qu'au glissement
        de souris, alors que la surveillance attrape aussi les valeurs posees
        par le programme — c'est ce qui fait marcher « Valeurs d'usine ».
        """
        boite = ttk.Frame(cadre)
        boite.grid(row=rang, column=col, sticky="w", padx=(0, 10))
        valeur = tk.DoubleVar(value=float(self.cfg[nom][champ]))
        etiquette = ttk.Label(boite, width=5, font=("monospace", 9))

        def bouge(*_a):
            try:
                brut = valeur.get()
            except tk.TclError:
                return
            if champ == "vitesse":
                brut = int(round(brut))
                etiquette.configure(text=str(brut))
            else:
                brut = round(brut, 2)
                etiquette.configure(text=f"{brut:.2f}")
            self.cfg[nom][champ] = brut         # effet immediat sur la partie

        echelle = ttk.Scale(boite, from_=bas, to=haut, orient="horizontal",
                            length=130, variable=valeur)
        echelle.pack(side="left")
        etiquette.pack(side="left", padx=(6, 0))
        valeur.trace_add("write", bouge)
        bouge()
        self.vars[nom][champ] = valeur
        self._astuce(echelle, legende)

    def _compteur(self, cadre, rang, col, nom) -> None:
        valeur = tk.IntVar(value=int(self.cfg[nom].get("repetitions", 1)))

        def bouge(*_a):
            try:
                self.cfg[nom]["repetitions"] = max(1, min(10, valeur.get()))
            except tk.TclError:
                pass                             # champ vide pendant la saisie

        spin = ttk.Spinbox(cadre, from_=1, to=10, width=4, textvariable=valeur)
        spin.grid(row=rang, column=col, padx=(0, 10))
        valeur.trace_add("write", bouge)
        self.vars[nom]["repetitions"] = valeur

    def _astuce(self, widget, texte: str) -> None:
        widget.bind("<Enter>", lambda _e: self.info.configure(text=texte))

    # -- boutons du bas ---------------------------------------------------
    def _reset(self) -> None:
        usine = pb.normalize_led_settings(None)
        for nom, champs in usine.items():
            self.cfg[nom].update(champs)
            for champ, var in self.vars.get(nom, {}).items():
                var.set(champs[champ])
        self.info.configure(text="Valeurs d'usine rétablies (non enregistrées).")

    def _save(self) -> None:
        if pb.save_led_settings(self.cfg):
            self.info.configure(text=f"Enregistré dans {pb.LEDS_PATH}")
        else:
            self.info.configure(text="Enregistrement impossible — "
                                     "voir le journal.")


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Pegasus — lichess")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.outbox: "queue.Queue" = queue.Queue()
        self.engine = Engine(self.outbox)
        self.state_snapshot: dict = {}
        self.running = False
        # Charge une fois pour toutes : ce dictionnaire est ensuite partage
        # avec le pont, donc les jauges agissent en direct.
        self.led_settings = pb.load_led_settings()

        self._build()
        self._install_logging()
        self.after(100, self._pump)
        self.after(200, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._quit)

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TButton", padding=6)
        style.configure("TCheckbutton", background=BG, foreground=FG)

        # Mise en page horizontale : reglages en haut, plateau a gauche,
        # pendule et etat a droite, journal en bas sur toute la largeur.
        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.grid(row=0, column=0, columnspan=2, sticky="ew")

        ttk.Label(top, text="Plateau").grid(row=0, column=0, sticky="w")
        self.address = ttk.Combobox(top, width=24, values=[])
        self.address.grid(row=0, column=1, padx=6)
        self.scan_btn = ttk.Button(top, text="Chercher", command=self._scan)
        self.scan_btn.grid(row=0, column=2)
        ttk.Label(top, text="Jeton lichess").grid(row=0, column=3, sticky="e",
                                                  padx=(16, 0))
        self.token = ttk.Entry(top, width=22, show="\u2022")
        self.token.grid(row=0, column=4, padx=6)
        existing = pb.load_token()
        if existing:
            self.token.insert(0, existing)
        self.save_token = tk.BooleanVar(value=not existing)
        ttk.Checkbutton(top, text="retenir",
                        variable=self.save_token).grid(row=0, column=5)

        self.flip = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Plateau tourné (noirs devant moi)",
                        variable=self.flip).grid(row=1, column=0, columnspan=3,
                                                 sticky="w", pady=(6, 0))
        self.show_board = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Afficher le plateau",
                        variable=self.show_board,
                        command=self._toggle_board).grid(row=1, column=3,
                                                         columnspan=2,
                                                         sticky="w",
                                                         pady=(6, 0))
        self.start_btn = ttk.Button(top, text="Connecter", command=self._toggle)
        self.start_btn.grid(row=1, column=5, pady=(6, 0))

        # -- colonne de gauche : le plateau
        self.board_frame = ttk.Frame(self, padding=(10, 6))
        self.board_frame.grid(row=1, column=0, sticky="n")
        self.canvas = tk.Canvas(self.board_frame, width=CELL * 8,
                                height=CELL * 8, highlightthickness=0, bg=BG)
        self.canvas.pack()

        # -- colonne de droite : pendule, etat, gestes
        cote = ttk.Frame(self, padding=(4, 6, 12, 6))
        cote.grid(row=1, column=1, sticky="n")

        self.clock_top_name = ttk.Label(cote, text="adversaire", anchor="e")
        self.clock_top_name.pack(fill="x")
        self.clock_top = tk.Label(cote, text="--:--", bg=BG, fg="#8a8a8a",
                                  font=("monospace", 34, "bold"), anchor="e")
        self.clock_top.pack(fill="x")
        ttk.Separator(cote, orient="horizontal").pack(fill="x", pady=10)
        self.clock_bottom = tk.Label(cote, text="--:--", bg=BG, fg=FG,
                                     font=("monospace", 34, "bold"), anchor="e")
        self.clock_bottom.pack(fill="x")
        self.clock_bottom_name = ttk.Label(cote, text="moi", anchor="e")
        self.clock_bottom_name.pack(fill="x")

        # -- actions de partie : ce qui ne se fait pas en poussant une piece
        actions = ttk.Frame(cote)
        actions.pack(fill="x", pady=(12, 0), anchor="w")
        self.boutons_partie = []
        for texte, nom, rang, col in (
                ("Nulle", "nulle", 0, 0),
                ("Reprise", "reprise", 0, 1),
                ("+15 s", "temps", 0, 2),
                ("Abandonner", "abandon", 1, 0),
                ("Annuler", "annuler", 1, 1),
                ("Refuser", "refuser", 1, 2)):
            b = ttk.Button(actions, text=texte, width=10,
                           command=lambda n=nom: self._game_action(n))
            b.grid(row=rang, column=col, padx=2, pady=2)
            b.state(["disabled"])
            self.boutons_partie.append(b)

        self.status = ttk.Label(cote, text="Prêt.", font=("", 10, "bold"),
                                wraplength=260, justify="left")
        self.status.pack(fill="x", pady=(16, 0), anchor="w")
        self.detail = ttk.Label(cote, text="", wraplength=260,
                                justify="left")
        self.detail.pack(fill="x", anchor="w")

        self.gestures = ttk.Label(cote, text="", justify="left",
                                  font=("monospace", 8), wraplength=260)
        self.gestures.pack(fill="x", pady=(12, 4), anchor="w")
        reglages = ttk.Frame(cote)
        reglages.pack(fill="x", anchor="w")
        ttk.Button(reglages, text="Modifier les gestes",
                   command=self._open_gestures).pack(side="left")
        ttk.Button(reglages, text="Lumières",
                   command=self._open_leds).pack(side="left", padx=6)

        # -- bas : le journal, sur toute la largeur
        self.log = tk.Text(self, height=7, width=84, bg="#1e1e1e", fg="#cfcfcf",
                           relief="flat", font=("monospace", 9), wrap="word")
        self.log.grid(row=2, column=0, columnspan=2, padx=10, pady=(4, 10),
                      sticky="ew")
        self.log.configure(state="disabled")

        self._draw_board({})

    def _install_logging(self) -> None:
        handler = GuiLogHandler(self.outbox)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                               datefmt="%H:%M:%S"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        for noisy in ("bleak", "dbus_fast", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    # -- dessin -----------------------------------------------------------
    def _draw_board(self, snapshot: dict) -> None:
        self.canvas.delete("all")
        pieces = snapshot.get("pieces", {})
        lit = set(snapshot.get("lit", ()))
        wrong = set(snapshot.get("wrong", ()))
        flip = snapshot.get("flip", False)

        for rank in range(8):
            for file in range(8):
                sq_rank = rank if flip else 7 - rank
                sq_file = 7 - file if flip else file
                square = sq_rank * 8 + sq_file
                x, y = file * CELL, rank * CELL
                colour = LIGHT if (sq_rank + sq_file) % 2 else DARK
                if square in wrong:
                    colour = WRONG
                elif square in lit:
                    colour = LIT
                self.canvas.create_rectangle(x, y, x + CELL, y + CELL,
                                             fill=colour, outline="")
                symbol = pieces.get(square)
                if symbol:
                    glyph = GLYPHS.get(symbol.lower(), symbol)
                    white = symbol.isupper()
                    # Un lisere sombre derriere la piece blanche pour qu'elle
                    # se detache aussi des cases claires.
                    if white:
                        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                            self.canvas.create_text(
                                x + CELL / 2 + dx, y + CELL / 2 + 2 + dy,
                                text=glyph, font=("DejaVu Sans", int(CELL * 0.62)),
                                fill="#3a3a3a")
                    self.canvas.create_text(
                        x + CELL / 2, y + CELL / 2 + 2, text=glyph,
                        font=("DejaVu Sans", int(CELL * 0.62)),
                        fill=WHITE_PIECE if white else BLACK_PIECE)
                # Tk ne connait pas la transparence : on prend la couleur de
                # l'autre case pour que le repere reste lisible partout.
                ink = DARK if (sq_rank + sq_file) % 2 else LIGHT
                if file == 0:
                    self.canvas.create_text(x + 8, y + 10, text=str(sq_rank + 1),
                                            font=("", 7), fill=ink)
                if rank == 7:
                    self.canvas.create_text(x + CELL - 8, y + CELL - 9,
                                            text="abcdefgh"[sq_file],
                                            font=("", 7), fill=ink)

    # -- evenements -------------------------------------------------------
    def _toggle_board(self) -> None:
        if self.show_board.get():
            self.board_frame.grid()
        else:
            self.board_frame.grid_remove()

    @staticmethod
    def _mmss(secondes) -> str:
        if secondes is None:
            return "--:--"
        secondes = max(0, int(secondes))
        if secondes >= 3600:
            return f"{secondes // 3600}:{secondes % 3600 // 60:02d}:{secondes % 60:02d}"
        return f"{secondes // 60}:{secondes % 60:02d}"

    def _tick(self) -> None:
        self._update_clock()
        self.after(200, self._tick)

    def _update_clock(self) -> None:
        """Egrene la pendule entre deux nouvelles de lichess."""
        snap = self.state_snapshot
        if snap.get("playing"):
            moi = snap.get("moi")
            haut = pb.chess.BLACK if moi == pb.chess.WHITE else pb.chess.WHITE
            restant = {pb.chess.WHITE: snap.get("blanc"),
                       pb.chess.BLACK: snap.get("noir")}
            ecoule = time.monotonic() - snap.get("pris_a", time.monotonic())
            trait = snap.get("trait")
            for label, couleur in ((self.clock_top, haut),
                                   (self.clock_bottom, moi)):
                valeur = restant.get(couleur)
                if valeur is not None and couleur == trait:
                    valeur = max(0.0, valeur - ecoule)
                label.configure(text=self._mmss(valeur))
                # Le camp au trait ressort ; en dessous de 20 s, il rougit.
                if couleur == trait:
                    label.configure(fg="#e06a5a" if (valeur or 0) < 20 else "#ffffff")
                else:
                    label.configure(fg="#8a8a8a")

    def _scan(self) -> None:
        self.scan_btn.configure(state="disabled")
        self._log("Recherche en cours…")
        self.engine.scan()
        self.after(11000, lambda: self.scan_btn.configure(state="normal"))

    def _toggle(self) -> None:
        if self.running:
            self._log("Arrêt demandé.")
            self.engine.stop()
            return
        address = self.address.get().split(" ")[0].strip()
        token = self.token.get().strip()
        if not token:
            self._log("Il faut un jeton lichess (portée board:play). "
                      "Crée-le ici : " + pb.TOKEN_URL)
            return
        self.engine.start(address, token, self.save_token.get(),
                          self.flip.get(), led_settings=self.led_settings)

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.outbox.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "boards":
                    values = [f"{addr}  {name}" for addr, name in payload]
                    self.address.configure(values=values)
                    if values and not self.address.get():
                        self.address.set(values[0])
                elif kind == "state":
                    avant = self.state_snapshot.get("game")
                    if payload.get("game") and payload["game"] != avant \
                            and payload.get("playing"):
                        # Nouvelle partie : on masque le plateau virtuel, qui
                        # gacherait le plaisir. La case reste a cocher a la main.
                        self.show_board.set(False)
                        self._toggle_board()
                        noms = {pb.chess.WHITE: "blancs", pb.chess.BLACK: "noirs"}
                        self.clock_bottom_name.configure(
                            text="moi (" + noms.get(payload.get("moi"), "?") + ")")
                        self.clock_top_name.configure(
                            text=payload.get("opponent") or "adversaire")
                    self.state_snapshot = payload
                    self._update_clock()
                    # Les actions de partie n'ont de sens qu'en partie.
                    for b in self.boutons_partie:
                        b.state(["!disabled"] if payload.get("playing")
                                else ["disabled"])
                    self._draw_board(payload)
                    self.status.configure(text=payload.get("status", ""))
                    bits = []
                    if payload.get("account"):
                        bits.append(payload["account"])
                    if payload.get("opponent"):
                        bits.append("contre " + payload["opponent"])
                    if payload.get("color"):
                        bits.append("avec les " + payload["color"])
                    if payload.get("moves"):
                        bits.append(f"{payload['moves']} demi-coups")
                    if payload.get("battery"):
                        bits.append("batterie " + payload["battery"])
                    gestes = payload.get("gestures") or []
                    if gestes:
                        titre = ("Recherche en cours — repose les pièces pour "
                                 "annuler\n" if payload.get("seeking")
                                 else "Soulève ces pièces, plateau au départ, "
                                      "pour lancer une partie :\n")
                        self.gestures.configure(
                            text=titre + "\n".join("   " + g for g in gestes))
                    self.detail.configure(text="  •  ".join(bits))
                elif kind == "started":
                    self.running = True
                    self.start_btn.configure(text="Arrêter")
                    self.status.configure(text="Connexion au plateau…")
                elif kind == "stopped":
                    self.running = False
                    self.start_btn.configure(text="Connecter")
                    self.status.configure(text="Arrêté.")
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line.rstrip() + "\n")
        self.log.see("end")
        if float(self.log.index("end")) > 400:
            self.log.delete("1.0", "200.0")
        self.log.configure(state="disabled")

    def _game_action(self, nom: str) -> None:
        """Les actions qui engagent : on confirme celles qui ne reviennent pas."""
        if nom == "abandon" and not messagebox.askyesno(
                "Abandonner", "Abandonner la partie en cours ?", parent=self):
            return
        if nom == "refuser":
            # Un seul bouton pour les deux refus : lichess ignore celui qui
            # ne correspond a aucune proposition en cours.
            self.engine.game_action("refuser-nulle")
            self.engine.game_action("refuser-reprise")
            return
        self.engine.game_action(nom)

    def _open_gestures(self) -> None:
        GestureEditor(self).transient(self)

    def _open_leds(self) -> None:
        LedEditor(self).transient(self)

    def _quit(self) -> None:
        if self.running:
            self.engine.stop()
        self.after(200, self.destroy)


def main() -> int:
    if tk is None:
        print("Tk n'est pas disponible dans cet interpreteur Python : "
              "l'interface graphique ne peut pas demarrer.\n"
              "Sur Arch : sudo pacman -S tk\n"
              "En attendant, le pont fonctionne en ligne de commande :\n"
              "  pegasus_bridge.py play --address XX:XX:XX:XX:XX:XX",
              file=sys.stderr)
        return 1
    if chess is None:
        print("module `chess` manquant : pip install chess", file=sys.stderr)
        return 1
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
