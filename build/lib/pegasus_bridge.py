#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pegasus_bridge.py — pont DGT Pegasus (BLE) -> port serie virtuel parlant le
protocole DGT « carte USB », pour DGT LiveChess, PicoChess, etc.

Pourquoi ce pont existe
-----------------------
Le Pegasus n'est PAS une carte serie : il expose un service BLE Nordic UART
(6E400001-...) sur lequel il parle le protocole DGT. LiveChess, lui, cherche des
cartes sur les ports serie (/dev/ttyUSB*, /dev/ttyACM*...). Deuxieme probleme :
le Pegasus est une carte « occupancy only », il dit quelles cases sont occupees
mais JAMAIS par quelle piece. Un board dump brut donnerait donc 32 pions blancs.

Ce pont fait donc deux choses :
  1. transport      : BLE Nordic UART  <->  port serie virtuel (pty ou tty0tty)
  2. identification : il suit la partie (python-chess) et reconstitue l'identite
                      des pieces, puis genere des board dumps / field updates
                      DGT parfaitement legitimes.

Sous-commandes
--------------
  scan    liste les peripheriques BLE qui ressemblent a un Pegasus
  probe   se connecte, fait le handshake, affiche l'occupation en direct
  serve   le pont complet (BLE -> port serie)

Dependances : bleak, chess   (pip install bleak chess)

Licence : MIT. Protocole Pegasus d'apres l'implementation publique BoardKit
(fianchettochess/BoardKit, MIT) ; protocole serie DGT d'apres PicoChess (GPL)
et la documentation publique DGT.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import termios
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence, Set, Tuple

LOG = logging.getLogger("pegasus")

# ---------------------------------------------------------------------------
# Cote BLE : constantes Pegasus
# ---------------------------------------------------------------------------

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"  # Nordic UART Service
NUS_WRITE = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"    # hote -> carte
NUS_NOTIFY = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # carte -> hote (notify)

# Cle « developpeur » publiee par plusieurs implementations communautaires.
DEVKEY = bytes([0xBE, 0xF5, 0xAE, 0xDD, 0xA9, 0x5F])

CMD_RESET = 0x40
CMD_BOARD_DUMP = 0x42
CMD_FIELD_UPDATE_MODE = 0x44   # Pegasus : surtout PAS 0x43 ni 0x4B
CMD_SERIALNR = 0x45
CMD_TRADEMARK = 0x47
CMD_BATTERY = 0x4C
CMD_VERSION = 0x4D
CMD_LED = 0x60
CMD_DEVKEY = 0x63

# ---------------------------------------------------------------------------
# Protocole DGT commun (carte -> hote)
# ---------------------------------------------------------------------------

MSG_BOARD_DUMP = 0x86
MSG_BWTIME = 0x8D
MSG_FIELD_UPDATE = 0x8E
MSG_EE_MOVES = 0x8F
MSG_BUSADRES = 0x90
MSG_SERIALNR = 0x91
MSG_TRADEMARK = 0x92
MSG_VERSION = 0x93
MSG_HARDWARE_VERSION = 0x96
MSG_BATTERY = 0xA0
MSG_LONG_SERIALNR = 0xA2
MSG_UNKNOWN_A3 = 0xA3
MSG_LOCK_STATE = 0xA4
MSG_DEVKEY_STATE = 0xA5

# Commandes observees en capturant l'application officielle DGT
# (emulateur Pegasus de DGTCentaurMods). Ni BoardKit ni l'extension Chrome
# ne les utilisent.
CMD_BUSADRES = 0x46        # -> 0x90
CMD_HARDWARE_VERSION = 0x48  # -> 0x96
CMD_EE_MOVES = 0x49        # -> 0x8f
CMD_LONG_SERIALNR = 0x55   # -> 0xa2
CMD_UNKNOWN_56 = 0x56      # -> 0xa3
CMD_LOCK_STATE = 0x59      # -> 0xa4  etat de verrouillage de la carte
CMD_DEVKEY_STATE = 0x5A    # -> 0xa5  etat de l'autorisation

EE_EOF = 0x6B

# Commandes hote -> carte (ce que LiveChess nous envoie)
S_RESET = 0x40
S_SEND_CLK = 0x41
S_BUSMODE = 0x4A
S_SEND_BRD = 0x42
S_SEND_UPDATE = 0x43
S_SEND_UPDATE_BRD = 0x44
S_RETURN_SERIALNR = 0x45
S_RETURN_BUSADRES = 0x46
S_SEND_TRADEMARK = 0x47
S_SEND_EE_MOVES = 0x49
S_SEND_UPDATE_NICE = 0x4B
S_SEND_BATTERY = 0x4C
S_SEND_VERSION = 0x4D
S_STARTBOOTLOADER = 0x4E
S_BRD_50B = 0x50
S_BRD_50W = 0x52
S_SCAN_100 = 0x54
S_RETURN_LONG_SERIALNR = 0x55
S_SET_LEDS = 0x60
S_CLOCK_MESSAGE = 0x2B

# Mode bus (protocole DGT multi-cartes). Commandes PC -> carte : 4 octets
# [cmd|0x80, adresse MSB, adresse LSB, somme de controle]. Reponses carte -> PC :
# [id|0x80, len MSB, len LSB, adr MSB, adr LSB, donnees..., somme], ou len
# compte la somme de controle finale. Les octets de commande du mode simple ont
# toujours le bit 7 a zero : c'est ce qui permet de distinguer les deux.
BUS_SEND_CLK = 0x81
BUS_SEND_BRD = 0x82
BUS_SEND_CHANGES = 0x83
BUS_REPEAT_CHANGES = 0x84
BUS_SET_START_GAME = 0x85
BUS_SEND_FROM_START = 0x86
BUS_PING = 0x87
BUS_END_BUSMODE = 0x88
BUS_RESET = 0x89
BUS_IGNORE_NEXT_PING = 0x8A
BUS_SEND_VERSION = 0x8B
BUS_SEND_BRD_50B = 0x8C
BUS_SEND_ALL_D = 0x8D

MSG_BUS_BRD_DUMP = 0x83
MSG_BUS_BWTIME = 0x84
MSG_BUS_UPDATE = 0x85
MSG_BUS_FROM_START = 0x86
MSG_BUS_PING = 0x87
MSG_BUS_START_GAME_WRITTEN = 0x88
MSG_BUS_VERSION = 0x89

EE_START_TAG = 0x7B

KNOWN_SINGLE_COMMANDS = frozenset((
    S_RESET, S_SEND_CLK, S_BUSMODE, S_SEND_BRD, S_SEND_UPDATE,
    S_SEND_UPDATE_BRD, S_RETURN_SERIALNR, S_RETURN_BUSADRES, S_SEND_TRADEMARK,
    S_SEND_EE_MOVES, S_SEND_UPDATE_NICE, S_SEND_BATTERY, S_SEND_VERSION,
    S_BRD_50B, S_BRD_50W, S_SCAN_100, S_RETURN_LONG_SERIALNR,
))

MODE_IDLE = 0
MODE_UPDATE = 1        # 0x43 : field updates + BWTIME
MODE_UPDATE_BRD = 2    # 0x44 : field updates seuls
MODE_UPDATE_NICE = 3   # 0x4B : field updates + BWTIME

try:
    import chess
except ImportError:  # pragma: no cover
    chess = None  # type: ignore

if chess is not None:
    PIECE_CODE: Dict[Tuple[bool, int], int] = {
        (chess.WHITE, chess.PAWN): 0x01,
        (chess.WHITE, chess.ROOK): 0x02,
        (chess.WHITE, chess.KNIGHT): 0x03,
        (chess.WHITE, chess.BISHOP): 0x04,
        (chess.WHITE, chess.KING): 0x05,
        (chess.WHITE, chess.QUEEN): 0x06,
        (chess.BLACK, chess.PAWN): 0x07,
        (chess.BLACK, chess.ROOK): 0x08,
        (chess.BLACK, chess.KNIGHT): 0x09,
        (chess.BLACK, chess.BISHOP): 0x0A,
        (chess.BLACK, chess.KING): 0x0B,
        (chess.BLACK, chess.QUEEN): 0x0C,
    }


def led_frame(squares: Sequence[int], speed: int = 0x02, repeat: int = 0x00,
              brightness: int = 0x01, terminator: bool = True) -> bytes:
    """Trame LED du Pegasus : 60 <len> 05 <vitesse> <repet> <lumin> <cases> 00.

    `len` compte tout ce qui suit l'octet de longueur, terminateur compris.
    `squares` est en index protocole (0 = a8). Liste vide -> tout eteindre.

    Les trois octets vitesse/repetition/luminosite viennent de l'extension
    Chrome, qui ne les a jamais exerces avec plus de deux cases : d'ou les
    parametres, pour sonder leur vrai role.
    """
    if not squares:
        return bytes([CMD_LED, 0x02, 0x00, 0x00])
    body = bytes([0x05, speed, repeat, brightness]) + bytes(squares)
    if terminator:
        body += b"\x00"
    return bytes([CMD_LED, len(body)]) + body


def bus_message(msg_id: int, payload: bytes, address: int) -> bytes:
    """Trame carte -> PC en mode bus, somme de controle comprise."""
    total = 5 + len(payload) + 1
    head = bytes([msg_id, (total >> 7) & 0x7F, total & 0x7F,
                  (address >> 7) & 0x7F, address & 0x7F]) + payload
    return head + bytes([sum(head) & 0x7F])


def change_tag(square_index: int, piece_code: int) -> bytes:
    """Tag de changement sur 2 octets : 0t0r nnnn puis 00ii iiii."""
    return bytes([0x40 | (piece_code & 0x0F), square_index & 0x3F])


def bus_address_from_serial(serial: str) -> int:
    """Adresse de bus 14 bits : lecture hexadecimale du numero de serie.

    La spec DGT donne l'exemple « 01025 1.0 » -> adresse 0x1025.
    """
    digits = "".join(c for c in serial if c in "0123456789abcdefABCDEF")
    if not digits:
        return 0
    try:
        return int(digits[-4:], 16) & 0x3FFF
    except ValueError:
        return 0


def dgt_message(msg_id: int, payload: bytes) -> bytes:
    """Encadre un message carte -> hote : [id, lenHi, lenLo, payload...]."""
    total = len(payload) + 3
    return bytes([msg_id, (total >> 7) & 0x7F, total & 0x7F]) + payload


# ---------------------------------------------------------------------------
# Index des cases
# ---------------------------------------------------------------------------
# Index protocole DGT : 0 = a8, 1 = b8, ... 7 = h8, 8 = a7, ... 63 = h1.
# Index python-chess : 0 = a1, ... 63 = h8.

def dgt_to_square(idx: int) -> int:
    return chess.square(idx & 7, 7 - (idx >> 3))


def square_to_dgt(sq: int) -> int:
    return (7 - chess.square_rank(sq)) * 8 + chess.square_file(sq)


# ---------------------------------------------------------------------------
# Reassemblage des trames DGT (BLE : une trame peut tenir sur plusieurs notifs)
# ---------------------------------------------------------------------------

class FrameParser:
    """Parseur de trames [id|0x80, lenHi, lenLo, payload...] avec resynchro."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def reset(self) -> None:
        self.buf.clear()

    def feed(self, data: bytes) -> List[Tuple[int, bytes]]:
        self.buf.extend(data)
        out: List[Tuple[int, bytes]] = []
        while True:
            # Resynchro : seul l'octet d'ID a le bit 7 arme.
            skipped = 0
            while self.buf and not (self.buf[0] & 0x80):
                self.buf.pop(0)
                skipped += 1
            if skipped:
                LOG.debug("resynchro : %d octet(s) ignore(s)", skipped)
            if len(self.buf) < 3:
                return out
            len_hi, len_lo = self.buf[1], self.buf[2]
            if (len_hi & 0x80) or (len_lo & 0x80):
                self.buf.pop(0)
                continue
            total = (len_hi << 7) | len_lo
            if total < 3 or total > 4096:
                self.buf.pop(0)
                continue
            if len(self.buf) < total:
                return out
            frame = bytes(self.buf[:total])
            del self.buf[:total]
            out.append((frame[0], frame[3:]))


# ---------------------------------------------------------------------------
# Suivi de la partie : occupation -> identite des pieces
# ---------------------------------------------------------------------------

class BoardTracker:
    """Reconstitue l'identite des pieces a partir de la seule occupation.

    Deux couches :
      * `logic`  : un chess.Board tenu a jour quand une occupation stable
                   s'explique par un (ou deux) coup(s) legal(aux). C'est la
                   source de verite des que la synchro tient.
      * `pieces` : la carte case -> piece reellement publiee vers LiveChess.
                   Pendant qu'une piece est « en l'air », elle est mise a jour
                   par le suivi des levers/poses (`hand`), ce qui permet
                   d'envoyer des field updates fideles en temps reel.
    """

    def __init__(
        self,
        promotion: int = 0,
        max_depth: int = 2,
        hand_timeout: float = 20.0,
        auto_reset: bool = True,
    ) -> None:
        self.promotion = promotion or chess.QUEEN
        self.max_depth = max(1, max_depth)
        self.hand_timeout = hand_timeout
        self.auto_reset = auto_reset

        self.logic = chess.Board()
        self.in_sync = True
        self.pieces: Dict[int, "chess.Piece"] = dict(self.logic.piece_map())
        self.occ: Set[int] = set(self.pieces)
        self.hand: List[Tuple[int, "chess.Piece", float]] = []
        self.unresolved_since: Optional[float] = None
        # Gele : on refuse d'interpreter le moindre coup tant que le plateau
        # n'est pas revenu a la position de reference. Sert apres une reprise
        # de coup, ou le plateau a de l'avance sur la partie.
        self.frozen = False
        # Cases levees / posees depuis la derniere resolution. L'occupation
        # finale d'une prise ne dit pas sur quelle case la piece a atterri ;
        # l'historique des evenements, lui, le dit.
        self.recent_lifts: List[int] = []
        self.recent_places: List[int] = []
        # Coup que l'on sait devoir arriver (celui de l'adversaire, a rejouer).
        # Tant qu'il est arme, aucun autre coup n'est accepte : sans ca, un
        # roque commence par la tour ressemblait a un simple Rf8 legal, qui
        # partait dans la partie suivie et la desynchronisait pour de bon.
        self.expected: Optional["chess.Move"] = None
        self.start_occ: Set[int] = set(chess.Board().piece_map())
        self.on_change = lambda: None  # callback, arme par le pont

    # -- utilitaires ------------------------------------------------------
    @staticmethod
    def _occ_of(board: "chess.Board") -> Set[int]:
        return set(board.piece_map())

    def codes(self) -> bytes:
        """Les 64 octets d'un board dump DGT (index 0 = a8)."""
        out = bytearray(64)
        for sq, piece in self.pieces.items():
            out[square_to_dgt(sq)] = PIECE_CODE[(piece.color, piece.piece_type)]
        return bytes(out)

    def ascii(self) -> str:
        rows = []
        for rank in range(7, -1, -1):
            row = []
            for file in range(8):
                sq = chess.square(file, rank)
                piece = self.pieces.get(sq)
                if piece is not None:
                    row.append(piece.symbol())
                elif sq in self.occ:
                    row.append("?")
                else:
                    row.append(".")
            rows.append(" ".join(row))
        return "\n".join(rows)

    def fen(self) -> str:
        return self.logic.fen() if self.in_sync else "(desynchronise)"

    def rewind_to(self, board: "chess.Board") -> None:
        """Recale la partie suivie sans toucher a l'occupation reelle.

        Apres une reprise de coup, lichess fait foi sur la partie mais le
        plateau, lui, a toujours les pieces la ou elles etaient : il faut que
        la difference reste visible pour la signaler au joueur.
        """
        self.logic = board.copy()
        self.expected = None
        self.hand.clear()
        self.in_sync = False
        self.frozen = True
        self.unresolved_since = None

    def reset_to(self, board: "chess.Board") -> None:
        """Repart d'une position connue (debut de partie lichess, reprise)."""
        self.logic = board.copy()
        self.expected = None
        self.pieces = dict(self.logic.piece_map())
        # On declare que le plateau est dans cette position ; le prochain dump
        # corrigera si ce n'est pas le cas. Sans cette ligne l'occupation
        # gardait la position initiale standard et contredisait la partie.
        self.occ = set(self.pieces)
        self.hand.clear()
        self.in_sync = True
        self.frozen = False
        self.unresolved_since = None

    # -- entrees ----------------------------------------------------------
    def set_occupancy(self, occupied: Set[int]) -> None:
        """Dump complet venant du Pegasus."""
        for sq in sorted(self.occ - occupied):
            self._lift(sq)
        for sq in sorted(occupied - self.occ):
            self._place(sq)

    def set_square(self, sq: int, occupied: bool) -> None:
        if occupied and sq not in self.occ:
            self._place(sq)
        elif not occupied and sq in self.occ:
            self._lift(sq)

    def _lift(self, sq: int) -> None:
        self.occ.discard(sq)
        self.recent_lifts.append(sq)
        piece = self.pieces.pop(sq, None)
        if piece is not None:
            self.hand.append((sq, piece, time.monotonic()))
            LOG.debug("leve %s sur %s", piece.symbol(), chess.square_name(sq))
        else:
            LOG.debug("leve une piece inconnue sur %s", chess.square_name(sq))
        self.on_change()

    def _place(self, sq: int) -> None:
        self.occ.add(sq)
        self.recent_places.append(sq)
        piece = self._pick_from_hand(sq)
        if piece is None:
            piece = chess.Piece(chess.PAWN, chess.WHITE)
            LOG.warning(
                "piece posee sur %s sans origine connue -> suppose pion blanc "
                "(reposez la position de depart pour resynchroniser)",
                chess.square_name(sq),
            )
        self.pieces[sq] = piece
        LOG.debug("pose %s sur %s", piece.symbol(), chess.square_name(sq))
        self.on_change()

    def _pick_from_hand(self, sq: int) -> Optional["chess.Piece"]:
        """Choisit, parmi les pieces en main, celle qui vient d'etre posee."""
        self._prune_hand()
        if not self.hand:
            return None
        if len(self.hand) == 1:
            return self.hand.pop()[1]

        # 1) un coup legal explique-t-il origine -> sq ?
        if self.in_sync:
            for i, (origin, piece, _) in enumerate(self.hand):
                for mv in self.logic.legal_moves:
                    if mv.from_square == origin and mv.to_square == sq:
                        return self.hand.pop(i)[1]

        # 2) roque : deux pieces en main, une case d'arrivee caracteristique.
        king_dest = {chess.G1, chess.C1, chess.G8, chess.C8}
        rook_dest = {chess.F1, chess.D1, chess.F8, chess.D8}
        wanted = None
        if sq in king_dest:
            wanted = chess.KING
        elif sq in rook_dest:
            wanted = chess.ROOK
        if wanted is not None:
            for i, (_, piece, _) in enumerate(self.hand):
                if piece.piece_type == wanted:
                    return self.hand.pop(i)[1]

        # 3) geometrie : la piece peut-elle atteindre cette case ?
        for i, (origin, piece, _) in enumerate(self.hand):
            if self._can_reach(piece, origin, sq):
                return self.hand.pop(i)[1]

        # 4) defaut : la derniere levee (cas typique d'une prise).
        return self.hand.pop()[1]

    @staticmethod
    def _can_reach(piece: "chess.Piece", origin: int, dest: int) -> bool:
        probe = chess.Board(None)
        probe.set_piece_at(origin, piece)
        probe.turn = piece.color
        for mv in probe.pseudo_legal_moves:
            if mv.from_square == origin and mv.to_square == dest:
                return True
        if piece.piece_type == chess.PAWN:  # prise en diagonale, case vide ici
            df = abs(chess.square_file(dest) - chess.square_file(origin))
            dr = chess.square_rank(dest) - chess.square_rank(origin)
            if piece.color == chess.BLACK:
                dr = -dr
            return df == 1 and dr == 1
        return False

    def _prune_hand(self) -> None:
        now = time.monotonic()
        kept = [h for h in self.hand if now - h[2] <= self.hand_timeout]
        if len(kept) != len(self.hand):
            LOG.debug("%d piece(s) en main oubliee(s) (prises)", len(self.hand) - len(kept))
            self.hand = kept

    # -- resolution -------------------------------------------------------
    def resolve(self) -> bool:
        """Appele quand l'occupation est stable. True si la partie est en phase."""
        target = frozenset(self.occ)

        # Volontairement sans condition sur in_sync : si l'occupation reelle
        # redevient celle de la partie suivie, on est reconcilie, meme apres
        # une position aberrante. C'est la porte de sortie d'un coup illegal
        # annule a la main.
        if self._occ_of(self.logic) == target:
            # L'occupation reelle correspond a la partie suivie : la partie fait
            # foi sur l'identite des pieces. Sans ce recalage, une identite mal
            # devinee pendant la mise en place (pieces posees une a une, sans
            # origine connue) survivait indefiniment, et LiveChess affichait un
            # fou ou un cavalier au milieu des pions.
            expected = dict(self.logic.piece_map())
            if self.pieces != expected or self.hand or not self.in_sync:
                if not self.in_sync:
                    LOG.info("position retablie, la partie reprend")
                else:
                    LOG.info("identites recalees sur la partie suivie")
                self.pieces = expected
                self.hand.clear()
                self.in_sync = True
                self.on_change()
            self.frozen = False
            self.unresolved_since = None
            self.recent_lifts.clear()
            self.recent_places.clear()
            return True

        if self.frozen:
            return False

        # Une piece du joueur au trait est encore en l'air : le coup n'est pas
        # termine, on ne devine rien.
        #
        # Sans ce garde-fou, lever un cavalier en f3 suffisait a declencher la
        # recherche : l'occupation « logic moins f3 » est exactement celle que
        # produirait une prise depuis f3. Si une seule prise etait possible
        # (Nxg5), elle etait retenue et envoyee a lichess avant meme que la
        # piece n'atterrisse en e5. La piece prise, elle, appartient a
        # l'adversaire et peut rester en main : c'est justement ce qui distingue
        # une prise terminee d'un coup en cours.
        if self.holding_mover_piece():
            return False

        # Un coup precis est attendu : soit le plateau le realise, soit on
        # patiente. Hors de question de retenir un autre coup legal.
        if self.expected is not None:
            probe = self.logic.copy(stack=False)
            if self.expected in probe.legal_moves:
                probe.push(self.expected)
                if self._occ_of(probe) == target:
                    LOG.info("coup adverse rejoue : %s",
                             self.logic.san(self.expected))
                    self.logic.push(self.expected)
                    self.pieces = dict(self.logic.piece_map())
                    self.hand.clear()
                    self.in_sync = True
                    self.expected = None
                    self.unresolved_since = None
                    self.recent_lifts.clear()
                    self.recent_places.clear()
                    self.on_change()
                    return True
            else:
                self.expected = None      # plus d'actualite
            if self.expected is not None:
                now = time.monotonic()
                if self.unresolved_since is None:
                    self.unresolved_since = now
                elif now - self.unresolved_since > 4.0 and self.in_sync:
                    extra, missing = self.mismatch()
                    LOG.warning(
                        "le coup attendu n'est pas encore realise. En trop : "
                        "%s. Manquant : %s.",
                        ", ".join(chess.square_name(s) for s in extra) or "rien",
                        ", ".join(chess.square_name(s) for s in missing) or "rien")
                    self.in_sync = False
                return False

        seq = self._search(target)
        if seq is not None:
            for mv in seq:
                LOG.info("coup detecte : %s", self.logic.san(mv))
                self.logic.push(mv)
            self.pieces = dict(self.logic.piece_map())
            self.hand.clear()
            self.in_sync = True
            self.unresolved_since = None
            self.recent_lifts.clear()
            self.recent_places.clear()
            self.on_change()
            return True

        # Resynchronisation sur la position de depart.
        if self.auto_reset and target == frozenset(self.start_occ):
            fresh = chess.Board()
            if (self.logic.fen() != fresh.fen() or not self.in_sync
                    or self.pieces != dict(fresh.piece_map())):
                LOG.info("position de depart detectee -> nouvelle partie")
                self.logic = chess.Board()
                self.pieces = dict(self.logic.piece_map())
                self.hand.clear()
                self.in_sync = True
                self.unresolved_since = None
                self.on_change()
                return True

        if self.hand:
            return False  # piece(s) en l'air : etat transitoire normal
        now = time.monotonic()
        if self.unresolved_since is None:
            self.unresolved_since = now
        elif now - self.unresolved_since > 3.0:
            if self.in_sync:
                extra, missing = self.mismatch()
                LOG.warning(
                    "position impossible a expliquer par un coup legal. "
                    "En trop : %s. Manquant : %s. Remettez ces pieces en place, "
                    "la partie reprendra toute seule.",
                    ", ".join(chess.square_name(s) for s in extra) or "rien",
                    ", ".join(chess.square_name(s) for s in missing) or "rien")
            self.in_sync = False
        return False

    def holding_mover_piece(self) -> bool:
        """Une piece de la couleur au trait est-elle encore en main ?"""
        self._prune_hand()
        turn = self.logic.turn
        return any(piece.color == turn for _, piece, _ in self.hand)

    def expect(self, move: Optional["chess.Move"]) -> None:
        self.expected = move

    def lifted(self) -> List[int]:
        """Cases qui devraient porter une piece et n'en portent plus."""
        return sorted(set(self.logic.piece_map()) - self.occ)

    def mismatch(self) -> Tuple[List[int], List[int]]:
        """(cases occupees a tort, cases vides a tort) face a la partie suivie."""
        expected = set(self.logic.piece_map())
        return sorted(self.occ - expected), sorted(expected - self.occ)

    def _search(self, target: frozenset) -> Optional[List["chess.Move"]]:
        # Meme desynchronise, la partie suivie reste valide : aucun coup
        # aberrant n'y a ete pousse. Si un coup legal explique l'occupation,
        # on le prend et la synchro revient.
        board = self.logic.copy(stack=False)

        # profondeur 1
        found: List["chess.Move"] = []
        for mv in board.legal_moves:
            board.push(mv)
            same = self._occ_of(board) == target
            board.pop()
            if same:
                found.append(mv)
        if found:
            promos = [m for m in found if m.promotion]
            if promos:
                chosen = next((m for m in promos if m.promotion == self.promotion), promos[0])
                LOG.info("promotion supposee en %s", chess.piece_name(chosen.promotion))
                return [chosen]
            if len(found) == 1:
                return [found[0]]
            # Plusieurs prises depuis la meme case donnent la meme occupation
            # (Bg3xc7 et Bg3xh4 vident toutes deux g3 et laissent l'arrivee
            # occupee). L'occupation ne tranche pas ; les evenements, si : on
            # sait quelle case a ete videe puis regarnie.
            narrowed = [m for m in found
                        if m.from_square in self.recent_lifts
                        and m.to_square in self.recent_places]
            if len(narrowed) == 1:
                return [narrowed[0]]
            narrowed = [m for m in found if m.to_square in self.recent_places]
            if len(narrowed) == 1:
                return [narrowed[0]]
            LOG.warning(
                "plusieurs coups donnent cette position : %s. Levez puis "
                "reposez la piece d'arrivee pour lever le doute.",
                ", ".join(self.logic.san(m) for m in found))
            return None

        if self.max_depth < 2:
            return None

        # profondeur 2 (deux coups joues avant que l'on regarde, notif perdue...)
        pairs: List[List["chess.Move"]] = []
        for mv1 in list(board.legal_moves):
            board.push(mv1)
            for mv2 in list(board.legal_moves):
                board.push(mv2)
                same = self._occ_of(board) == target
                board.pop()
                if same:
                    pairs.append([mv1, mv2])
            board.pop()
        if len(pairs) == 1:
            return pairs[0]
        if len(pairs) > 1:
            LOG.debug("%d sequences de 2 coups possibles : ambigu", len(pairs))
        return None


# ---------------------------------------------------------------------------
# Cote serie : on se fait passer pour une carte DGT
# ---------------------------------------------------------------------------

class SerialFrontend:
    def __init__(
        self,
        fd: int,
        tracker: BoardTracker,
        serial_nr: str = "10001",
        trademark: Optional[str] = None,
        version: Tuple[int, int] = (1, 6),
        dump_on_update: bool = True,
        debug: bool = False,
        bus_address: Optional[int] = None,
    ) -> None:
        self.fd = fd
        self.tracker = tracker
        self.serial_nr = (serial_nr + "     ")[:5]
        # L'entete est verifiee : l'emulateur Pegasus de DGTCentaurMods note que
        # la reponse « doit contenir Digital Game Technology\r\nCopyright (c) ».
        # Un texte fantaisiste ici et la carte est rejetee sans un mot.
        self.trademark = trademark or (
            "Digital Game Technology\r\n"
            "Copyright (c) 2021 DGT\r\n"
            "software version: 1.06, build: 220307\r\n"
            f"hardware version: 1.00, serial no: {self.serial_nr}"
        )
        self.version = version
        self.dump_on_update = dump_on_update
        self.debug = debug
        self.mode = MODE_IDLE
        self.last_codes = tracker.codes()
        self.seen_client = False
        self.bus_mode = False
        self.bus_address = (bus_address if bus_address is not None
                            else bus_address_from_serial(self.serial_nr))
        self.changes = bytearray()      # tags depuis le dernier SEND_CHANGES
        self.last_update = b""          # pour REPEAT_CHANGES
        self.from_start = bytearray()   # depuis SET_START_GAME
        self.from_start_active = False
        self.ignore_next_ping = False
        self.rx = bytearray()
        self._pending: Optional[Tuple[int, int]] = None  # (cmd, octets restants)
        self._await_len: Optional[int] = None

    # -- sortie -----------------------------------------------------------
    def _write(self, data: bytes) -> None:
        try:
            os.write(self.fd, data)
        except BlockingIOError:
            LOG.warning("port serie sature, message perdu")
        except OSError as exc:
            LOG.error("ecriture sur le port serie impossible : %s", exc)
        if self.debug:
            LOG.info("serie <- %s", data.hex(" "))

    def send_board_dump(self) -> None:
        codes = self.tracker.codes()
        self.last_codes = codes
        self._write(dgt_message(MSG_BOARD_DUMP, codes))

    def send_bwtime_none(self) -> None:
        # Aucune pendule branchee : 7 octets a zero.
        self._write(dgt_message(MSG_BWTIME, bytes(7)))

    def on_board_changed(self) -> None:
        """Field updates en mode simple, journal de changements en mode bus."""
        codes = self.tracker.codes()
        if codes == self.last_codes:
            return
        changed = [i for i in range(64) if codes[i] != self.last_codes[i]]
        for idx in changed:
            tag = change_tag(idx, codes[idx])
            self.changes.extend(tag)
            if self.from_start_active:
                self.from_start.extend(tag)
        # En mode bus la carte ne parle que lorsqu'elle est interrogee.
        if not self.bus_mode and self.mode in (MODE_UPDATE, MODE_UPDATE_BRD,
                                               MODE_UPDATE_NICE):
            for idx in changed:
                self._write(dgt_message(MSG_FIELD_UPDATE, bytes([idx, codes[idx]])))
        self.last_codes = codes

    # -- entree -----------------------------------------------------------
    def feed(self, data: bytes) -> None:
        if data and not self.seen_client:
            self.seen_client = True
            LOG.info("LiveChess a ouvert le port et commence a parler")
        if self.debug and data:
            LOG.info("serie -> %s", data.hex(" "))
        self.rx.extend(data)
        self._drain()

    def _drain(self) -> None:
        while self.rx:
            # Au milieu d'une commande multi-octets du mode simple : on continue.
            if self._await_len is not None or self._pending is not None:
                self._byte(self.rx.pop(0))
                continue
            first = self.rx[0]
            if first & 0x80:  # commande de mode bus
                if len(self.rx) < 4:
                    return  # trame incomplete, on attend la suite
                frame = bytes(self.rx[:4])
                if (sum(frame[:3]) & 0x7F) != frame[3]:
                    # Somme fausse : ce n'est pas pour nous. LiveChess sonde
                    # aussi d'autres marques (Caissa) sur le meme port ; on
                    # avance d'un octet au lieu de repondre n'importe quoi.
                    LOG.debug("octet hors protocole DGT ignore : 0x%02x", first)
                    del self.rx[:1]
                    continue
                del self.rx[:4]
                self._bus_command(frame)
                continue
            self._byte(self.rx.pop(0))

    # -- mode bus ---------------------------------------------------------
    def _bus_reply(self, msg_id: int, payload: bytes = b"") -> None:
        self._write(bus_message(msg_id, payload, self.bus_address))

    def _bus_command(self, frame: bytes) -> None:
        cmd = frame[0]
        address = (frame[1] << 7) | frame[2]
        if not self.bus_mode:
            LOG.info("LiveChess passe en mode bus (adresse %d)", self.bus_address)
            self.bus_mode = True
        broadcast = address == 0
        if not broadcast and address != self.bus_address:
            LOG.debug("commande bus 0x%02x pour l'adresse %d : pas nous",
                      cmd, address)
            return
        LOG.debug("commande bus 0x%02x (adresse %d)", cmd, address)

        if cmd == BUS_SEND_VERSION:
            LOG.info("LiveChess demande la version en mode bus -> on repond")
            self._bus_reply(MSG_BUS_VERSION, bytes(self.version))
        elif cmd == BUS_PING:
            if broadcast and self.ignore_next_ping:
                self.ignore_next_ping = False
                LOG.debug("ping general ignore comme demande")
                return
            self._bus_reply(MSG_BUS_PING)
        elif cmd == BUS_IGNORE_NEXT_PING:
            self.ignore_next_ping = True
            self._bus_reply(MSG_BUS_PING)
        elif cmd == BUS_SEND_BRD:
            codes = self.tracker.codes()
            self.last_codes = codes
            self._bus_reply(MSG_BUS_BRD_DUMP, codes)
        elif cmd == BUS_SEND_CHANGES:
            payload = bytes(self.changes) + bytes([EE_EOF])
            self.changes.clear()
            self.last_update = payload
            self._bus_reply(MSG_BUS_UPDATE, payload)
        elif cmd == BUS_REPEAT_CHANGES:
            self._bus_reply(MSG_BUS_UPDATE, self.last_update or bytes([EE_EOF]))
        elif cmd == BUS_SET_START_GAME:
            # Un tag de debut, puis la position complete, comme le ferait
            # l'EEPROM d'une vraie carte.
            self.from_start = bytearray([EE_START_TAG])
            codes = self.tracker.codes()
            for idx in range(64):
                if codes[idx]:
                    self.from_start.extend(change_tag(idx, codes[idx]))
            self.from_start_active = True
            self.changes.clear()
            LOG.info("LiveChess marque le debut de partie")
            self._bus_reply(MSG_BUS_START_GAME_WRITTEN)
        elif cmd == BUS_SEND_FROM_START:
            self._bus_reply(MSG_BUS_FROM_START,
                            bytes(self.from_start) + bytes([EE_EOF]))
        elif cmd == BUS_SEND_CLK:
            self._bus_reply(MSG_BUS_BWTIME, bytes(7))
        elif cmd == BUS_SEND_ALL_D:
            payload = bytes(self.changes) + bytes([EE_EOF])
            self.changes.clear()
            self.last_update = payload
            self._bus_reply(MSG_BUS_UPDATE, payload)
            self._bus_reply(MSG_BUS_BWTIME, bytes(7))
            codes = self.tracker.codes()
            self.last_codes = codes
            self._bus_reply(MSG_BUS_BRD_DUMP, codes)
        elif cmd == BUS_END_BUSMODE:
            LOG.info("retour au mode simple")
            self.bus_mode = False
        elif cmd == BUS_RESET:
            LOG.debug("reset bus demande")
        elif cmd == BUS_SEND_BRD_50B:
            LOG.debug("commande dames (0x8c) ignoree")
        else:
            LOG.warning("commande bus inconnue : 0x%02x", cmd)

    def _byte(self, b: int) -> None:
        if self._await_len is not None:
            cmd = self._await_len
            self._await_len = None
            if b > 0:
                self._pending = (cmd, b)
            else:
                self._command_body(cmd, b"")
            return
        if self._pending is not None:
            cmd, left = self._pending
            left -= 1
            if left <= 0:
                self._pending = None
                self._command_body(cmd, b"")
            else:
                self._pending = (cmd, left)
            return

        if b in (S_CLOCK_MESSAGE, S_SET_LEDS):
            self._await_len = b
            return
        self._command(b)

    def _command_body(self, cmd: int, _body: bytes) -> None:
        if cmd == S_CLOCK_MESSAGE:
            # Pas de pendule : on repond un BWTIME neutre.
            self.send_bwtime_none()
        elif cmd == S_SET_LEDS:
            LOG.debug("commande LED recue (ignoree pour l'instant)")

    def _command(self, cmd: int) -> None:
        # Spec DGT : toute commande du mode simple ramene la carte en mode
        # simple ; DGT_TO_BUSMODE fait l'inverse.
        if cmd == S_BUSMODE:
            if not self.bus_mode:
                LOG.info("LiveChess demande le mode bus (0x4a)")
            self.bus_mode = True
            return
        # Les sondes d'autres marques (Caissa) laissent trainer des octets qui
        # ne sont pas des commandes DGT : ils ne doivent pas nous faire quitter
        # le mode bus. Seule une commande reconnue le fait.
        if self.bus_mode and cmd in KNOWN_SINGLE_COMMANDS:
            LOG.debug("commande simple 0x%02x -> retour au mode simple", cmd)
            self.bus_mode = False
        if cmd == S_RESET:
            LOG.debug("LiveChess : reset -> mode idle")
            self.mode = MODE_IDLE
        elif cmd == S_SEND_BRD:
            self.send_board_dump()
        elif cmd == S_SEND_UPDATE:
            self.mode = MODE_UPDATE
            LOG.info("LiveChess : mode update (0x43)")
            if self.dump_on_update:
                self.send_board_dump()
        elif cmd == S_SEND_UPDATE_BRD:
            self.mode = MODE_UPDATE_BRD
            LOG.info("LiveChess : mode update board (0x44)")
            if self.dump_on_update:
                self.send_board_dump()
        elif cmd == S_SEND_UPDATE_NICE:
            self.mode = MODE_UPDATE_NICE
            LOG.info("LiveChess : mode update nice (0x4b)")
            if self.dump_on_update:
                self.send_board_dump()
        elif cmd == S_RETURN_SERIALNR:
            self._write(dgt_message(MSG_SERIALNR, self.serial_nr.encode("ascii")))
        elif cmd == S_RETURN_LONG_SERIALNR:
            long_nr = (self.serial_nr + "00000")[:10]
            self._write(dgt_message(MSG_LONG_SERIALNR, long_nr.encode("ascii")))
        elif cmd == S_RETURN_BUSADRES:
            self._write(dgt_message(MSG_BUSADRES, bytes([
                (self.bus_address >> 7) & 0x7F, self.bus_address & 0x7F])))
        elif cmd == S_SEND_TRADEMARK:
            self._write(dgt_message(MSG_TRADEMARK, self.trademark.encode("ascii")))
        elif cmd == S_SEND_VERSION:
            self._write(dgt_message(MSG_VERSION, bytes(self.version)))
        elif cmd == S_SEND_EE_MOVES:
            self._write(dgt_message(MSG_EE_MOVES, bytes([EE_EOF])))
        elif cmd == S_SEND_BATTERY:
            self._write(dgt_message(MSG_BATTERY, bytes([100, 0, 0, 0])))
        elif cmd == S_SEND_CLK:
            self.send_bwtime_none()
        elif cmd in (S_SCAN_100, S_BRD_50B, S_BRD_50W):
            LOG.debug("commande 0x%02x ignoree", cmd)
        elif cmd == S_STARTBOOTLOADER:
            LOG.warning("commande bootloader (0x4e) ignoree")
        elif cmd == 0x00:
            pass
        else:
            # Souvent du bruit : LiveChess sonde d'autres protocoles sur le
            # meme port. On ne repond pas et on ne s'en alarme pas.
            LOG.debug("octet hors protocole DGT ignore : 0x%02x", cmd)


def open_pty(link: Optional[str], mode: int = 0o666,
             owner: Optional[str] = None) -> Tuple[int, int, str]:
    """Cree une paire pty. Renvoie (fd maitre, fd esclave, chemin esclave).

    Le pty herite de l'utilisateur qui lance le pont. Comme `--link` impose
    souvent de tourner en root alors que LiveChess tourne en utilisateur, on
    ouvre les droits sur l'esclave, sinon LiveChess voit le port mais ne peut
    pas l'ouvrir.
    """
    master, slave = os.openpty()
    path = os.ttyname(slave)
    for fd in (master, slave):
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0  # iflag
        attrs[1] = 0  # oflag
        attrs[3] = 0  # lflag : pas d'echo, pas de canonique
        attrs[2] = (attrs[2] & ~termios.CSIZE) | termios.CS8
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[2] &= ~termios.PARENB
        attrs[4] = termios.B9600
        attrs[5] = termios.B9600
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    os.set_blocking(master, False)

    if owner is None:
        owner = os.environ.get("SUDO_USER")  # lance via sudo : rendre la main
    try:
        os.chmod(path, mode)
    except OSError as exc:
        LOG.warning("impossible de changer les droits de %s : %s", path, exc)
    if owner:
        try:
            import pwd
            entry = pwd.getpwnam(owner)
            os.chown(path, entry.pw_uid, entry.pw_gid)
            LOG.info("%s appartient maintenant a %s", path, owner)
        except (KeyError, OSError) as exc:
            LOG.warning("impossible de donner %s a %s : %s", path, owner, exc)
    try:
        info = os.stat(path)
        LOG.info("droits sur %s : %s uid=%d gid=%d",
                 path, oct(info.st_mode & 0o777), info.st_uid, info.st_gid)
    except OSError:
        pass

    if link:
        try:
            if os.path.islink(link) or os.path.exists(link):
                os.remove(link)
            os.symlink(path, link)
            LOG.info("lien symbolique %s -> %s", link, path)
        except OSError as exc:
            LOG.error("impossible de creer %s : %s", link, exc)
    return master, slave, path


def tty0tty_peer(path: str) -> Optional[str]:
    """/dev/tnt1 -> /dev/tnt0 : les paires tty0tty vont 0<->1, 2<->3, etc."""
    base = os.path.basename(path)
    if not base.startswith("tnt") or not base[3:].isdigit():
        return None
    return os.path.join(os.path.dirname(path), f"tnt{int(base[3:]) ^ 1}")


def open_device(path: str) -> int:
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[3] = 0
        attrs[2] = (attrs[2] & ~termios.CSIZE) | termios.CS8
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[2] &= ~termios.PARENB
        attrs[4] = termios.B9600
        attrs[5] = termios.B9600
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error as exc:
        LOG.warning("%s n'est pas un tty configurable (%s), on continue", path, exc)
    return fd


# ---------------------------------------------------------------------------
# Cote BLE
# ---------------------------------------------------------------------------

async def find_board(address: Optional[str], name: Optional[str], timeout: float):
    from bleak import BleakScanner

    if address:
        LOG.info("recherche de %s ...", address)
        dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if dev is None:
            raise SystemExit(f"aucun peripherique BLE a l'adresse {address}")
        return dev

    LOG.info("scan BLE (%.0f s) ...", timeout)
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    best = None
    for dev, adv in found.values():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        dev_name = dev.name or ""
        by_uuid = NUS_SERVICE in uuids
        by_name = "pegas" in dev_name.lower() or "dgt" in dev_name.lower()
        if name and name.lower() not in dev_name.lower():
            continue
        if by_uuid or by_name:
            LOG.info("candidat : %s  %s  rssi=%s  uuid=%s",
                     dev.address, dev_name or "(sans nom)", adv.rssi, by_uuid)
            if best is None or by_uuid:
                best = dev
    if best is None:
        raise SystemExit(
            "aucun Pegasus trouve. Verifiez qu'il est allume, pas deja connecte "
            "a un telephone, et lancez `pegasus_bridge.py scan` pour voir ce que "
            "l'adaptateur percoit."
        )
    return best


class PegasusLink:
    """Connexion BLE + handshake + decodage des trames Pegasus."""

    def __init__(self, on_dump, on_field, flip: bool = False,
                 trace: bool = False, devkey: bytes = DEVKEY,
                 accept_weird_dumps: bool = False,
                 invert: bool = False, on_raw=None) -> None:
        self.invert = invert
        self.on_raw = on_raw
        self._warned_suspect = False
        self.on_dump = on_dump
        self.on_field = on_field
        self.flip = flip
        self.trace = trace
        self.devkey = devkey
        self.accept_weird_dumps = accept_weird_dumps
        self.parser = FrameParser()
        self.client = None
        self.field_updates = 0
        self.dumps = 0
        self.battery: dict = {}

    def _square(self, idx: int) -> int:
        """Index protocole (0 = a8) -> case python-chess (0 = a1).

        La conversion etait le bug d'orientation historique : passer l'index
        brut au suivi de partie revenait a lire le plateau en miroir de rangees
        (les blancs apparaissaient en bas en minuscules), et `--flip` corrigeait
        les rangees en introduisant un miroir de colonnes.
        """
        if self.flip:
            idx = 63 - idx
        return dgt_to_square(idx)

    def _index(self, square: int) -> int:
        """Case python-chess -> index protocole, pour les LED."""
        idx = square_to_dgt(square)
        return 63 - idx if self.flip else idx

    def _busy(self, value: int) -> bool:
        """Une case est-elle occupee ? (--invert si la carte code a l'envers)"""
        return (value == 0) if self.invert else (value != 0)

    def _notify(self, _sender, data: bytearray) -> None:
        if self.trace:
            LOG.info("BLE <- %s", bytes(data).hex(" "))
        for msg_id, payload in self.parser.feed(bytes(data)):
            self._frame(msg_id, payload)

    def _frame(self, msg_id: int, payload: bytes) -> None:
        if self.trace:
            LOG.info("trame 0x%02x  len=%d  %s", msg_id, len(payload) + 3,
                     payload.hex(" "))
        if msg_id == MSG_BOARD_DUMP:
            self.dumps += 1
            if len(payload) < 64:
                LOG.warning("board dump tronque (%d octets)", len(payload))
                return
            values = payload[:64]
            if self.on_raw is not None:
                self.on_raw(values)
            distinct = sorted(set(values))
            nonzero = sum(1 for v in values if self._busy(v))
            LOG.info("board dump : %d/64 cases occupees, valeurs presentes : %s",
                     nonzero, " ".join(f"0x{v:02x}" for v in distinct[:12]))
            if nonzero == 64 and not self.accept_weird_dumps:
                if self._warned_suspect:
                    return
                self._warned_suspect = True
                LOG.warning(
                    "dump suspect (64 cases occupees, aucun zero) : la carte ne "
                    "renvoie pas d'occupation reelle -> ignore. Lancez "
                    "`raw` pour voir les octets bruts, ou --accept-weird-dumps "
                    "pour le prendre quand meme."
                )
                return
            occupied = {self._square(i) for i in range(64) if self._busy(values[i])}
            self.on_dump(occupied)
        elif msg_id == MSG_FIELD_UPDATE:
            if len(payload) < 2:
                return
            self.field_updates += 1
            LOG.debug("field update : case %d -> 0x%02x", payload[0], payload[1])
            self.on_field(self._square(payload[0]), self._busy(payload[1]))
        elif msg_id == MSG_SERIALNR:
            LOG.info("numero de serie : %s", payload.decode("ascii", "replace"))
        elif msg_id == MSG_VERSION:
            if len(payload) >= 2:
                LOG.info("firmware %d.%d%s", payload[0], payload[1],
                         "  (major=1 -> Pegasus confirme)" if payload[0] == 1 else "")
        elif msg_id == MSG_TRADEMARK:
            LOG.info("trademark : %s", payload.decode("ascii", "replace").strip())
        elif msg_id == MSG_BATTERY:
            self._battery(payload)
        elif msg_id == MSG_LOCK_STATE:
            state = payload[0] if payload else None
            LOG.warning("ETAT DE VERROUILLAGE (0xa4) : %s%s",
                        payload.hex(" ") or "(vide)",
                        "  -> 0 = deverrouillee" if state == 0 else
                        "  -> NON NULLE : la carte se considere verrouillee")
        elif msg_id == MSG_HARDWARE_VERSION:
            if len(payload) >= 2:
                LOG.info("version materielle %d.%d", payload[0], payload[1])
        elif msg_id == MSG_UNKNOWN_A3:
            LOG.info("reponse 0xa3 : %s", payload.hex(" ") or "(vide)")
        elif msg_id == MSG_LONG_SERIALNR:
            LOG.info("numero de serie long : %s", payload.decode("ascii", "replace"))
        elif msg_id == MSG_DEVKEY_STATE:
            state = payload[0] if payload else None
            if state == 0x01:
                LOG.info("cle developpeur acceptee (0xa5 01)")
            else:
                LOG.warning(
                    "reponse a la cle developpeur : %s  <- si la carte refuse la "
                    "cle, elle repond aux questions d'identite mais ne livre pas "
                    "l'occupation reelle", payload.hex(" ") or "(vide)")
        else:
            LOG.debug("trame 0x%02x : %s", msg_id, payload.hex(" "))

    async def connect(self, device, handshake: bool = True,
                      kind: str = "ext") -> None:
        from bleak import BleakClient

        LOG.info("connexion a %s ...", getattr(device, "address", device))
        self.client = BleakClient(device, timeout=20.0)
        await self.client.connect()
        LOG.info("connecte")
        self.parser.reset()
        if self.trace:
            for service in self.client.services:
                LOG.info("service %s", service.uuid)
                for ch in service.characteristics:
                    LOG.info("   caracteristique %s  %s", ch.uuid, ",".join(ch.properties))
        await self.client.start_notify(NUS_NOTIFY, self._notify)
        if handshake:
            await self.handshake(kind)

    async def write(self, data: bytes, response: bool = True) -> None:
        await self.client.write_gatt_char(NUS_WRITE, data, response=response)

    def devkey_frame(self) -> bytes:
        return bytes([CMD_DEVKEY, len(self.devkey) + 1]) + self.devkey + b"\x00"

    def _sequence(self, kind: str) -> Sequence[Tuple[float, bytes, str]]:
        key = self.devkey_frame()
        info = (
            (0.10, bytes([CMD_SERIALNR]), "numero de serie"),
            (0.05, bytes([CMD_VERSION]), "version"),
            (0.05, bytes([CMD_TRADEMARK]), "trademark"),
            (0.05, bytes([CMD_BATTERY]), "batterie"),
        )
        if kind == "ext":
            # Ordre de l'extension Chrome PegasusChessComChromeExtension :
            # la cle developpeur est la toute premiere ecriture.
            return (
                (0.30, key, "cle developpeur (en premier)"),
                (0.05, bytes([CMD_RESET]), "reset"),
                (0.05, bytes([CMD_BOARD_DUMP]), "board dump"),
                (0.05, bytes([CMD_FIELD_UPDATE_MODE]), "mode field update"),
            ) + info
        if kind == "dgt":
            # Ordre du pilote Dart mono424/dgtdriver (repris par BoardKit).
            return (
                (0.30, bytes([CMD_RESET]), "reset"),
                (0.05, bytes([CMD_SERIALNR]), "numero de serie"),
                (0.05, bytes([CMD_VERSION]), "version"),
                (0.05, key, "cle developpeur"),
                (0.05, bytes([CMD_TRADEMARK]), "trademark"),
                (0.05, bytes([CMD_RESET]), "reset"),
                (0.05, bytes([CMD_BOARD_DUMP]), "board dump"),
                (0.05, bytes([CMD_FIELD_UPDATE_MODE]), "mode field update"),
                (0.05, bytes([CMD_BATTERY]), "batterie"),
            )
        return ()

    async def handshake(self, kind: str = "ext") -> None:
        if kind == "none":
            LOG.info("handshake desactive")
            return
        kinds = ["ext", "dgt"] if kind == "both" else [kind]
        for one in kinds:
            LOG.info("handshake : sequence « %s »", one)
            for delay, frame, label in self._sequence(one):
                await asyncio.sleep(delay)
                LOG.debug("handshake : %s (%s)", label, frame.hex(" "))
                await self.write(frame)
            await asyncio.sleep(0.4)
        LOG.info("handshake termine")

    def _battery(self, payload: bytes) -> None:
        """DGT_MSG_BATTERY_STATUS, format documente dans dgtbrd13.h.

        octet 0 : capacite restante en %        octets 3-4 : temps allume
        octets 1-2 : autonomie restante (0x7f = inconnue)
        octets 5-7 : temps en veille            octet 8 : bits d'etat
                                                  bit 0 charge, bit 1 decharge
        """
        if len(payload) < 9:
            LOG.debug("trame batterie courte : %s", payload.hex(" "))
            return
        status = payload[8]
        left = None
        if payload[1] != 0x7F and payload[2] != 0x7F and (payload[1] or payload[2]):
            left = (payload[1], payload[2])
        before = self.battery_text()
        self.battery = {
            "percent": payload[0],
            "charging": bool(status & 0x01),
            "discharging": bool(status & 0x02),
            "left": left,
        }
        now = self.battery_text()
        if now != before:
            LOG.info("batterie : %s", now)

    def battery_text(self) -> str:
        if not self.battery:
            return ""
        parts = [f"{self.battery['percent']} %"]
        if self.battery["charging"]:
            parts.append("en charge")
        elif self.battery["left"]:
            hours, minutes = self.battery["left"]
            parts.append(f"{hours} h {minutes:02d} restantes")
        return " ".join(parts)

    async def request_battery(self) -> None:
        await self.write(bytes([CMD_BATTERY]))

    async def request_dump(self) -> None:
        await self.write(bytes([CMD_BOARD_DUMP]))

    async def leds(self, squares: Sequence[int], **kwargs) -> None:
        frame = led_frame([self._index(s) for s in squares], **kwargs)
        LOG.debug("LED -> %s", frame.hex(" "))
        await self.write(frame)

    async def leds_raw(self, frame: bytes) -> None:
        LOG.info("LED -> %s", frame.hex(" "))
        await self.write(frame)

    async def disconnect(self) -> None:
        if self.client is not None and self.client.is_connected:
            try:
                await self.client.stop_notify(NUS_NOTIFY)
            except Exception:
                pass
            await self.client.disconnect()


# ---------------------------------------------------------------------------
# Sous-commandes
# ---------------------------------------------------------------------------

async def cmd_scan(args) -> int:
    from bleak import BleakScanner

    LOG.info("scan BLE pendant %.0f s ...", args.timeout)
    found = await BleakScanner.discover(timeout=args.timeout, return_adv=True)
    if not found:
        print("Aucun peripherique BLE vu. bluetooth.service tourne-t-il "
              "(systemctl status bluetooth) ? L'adaptateur est-il bloque "
              "(rfkill list) ?")
        return 1
    print(f"{'adresse':<20} {'rssi':>5}  nom / services")
    for dev, adv in sorted(found.values(), key=lambda p: -(p[1].rssi or -999)):
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        tag = "  <-- Nordic UART (Pegasus ?)" if NUS_SERVICE in uuids else ""
        print(f"{dev.address:<20} {adv.rssi!s:>5}  {dev.name or '(sans nom)'}{tag}")
        if args.show_uuids and uuids:
            for u in uuids:
                print(f"{'':<20} {'':>5}    {u}")
    return 0


def parse_devkey(text: Optional[str]) -> bytes:
    if not text:
        return DEVKEY
    cleaned = text.replace(":", " ").replace(",", " ").replace("0x", " ")
    try:
        return bytes.fromhex("".join(cleaned.split()))
    except ValueError:
        raise SystemExit(f"cle developpeur illisible : {text!r}")


async def warn_if_silent(frontend: "SerialFrontend", port: str,
                         delay: float = 20.0) -> None:
    await asyncio.sleep(delay)
    if frontend.seen_client:
        return
    LOG.warning(
        "aucun octet recu apres %.0f s : LiveChess n'a pas ouvert %s. "
        "Le pty est recree a chaque demarrage du pont, donc LiveChess doit "
        "etre lance APRES lui, et le port doit etre selectionne dans son "
        "interface.", delay, port)


async def poll_dumps(link: "PegasusLink", period: float, grace: float = 5.0) -> None:
    """Redemande des board dumps si la carte ne pousse pas de field updates."""
    await asyncio.sleep(grace)
    if getattr(link, "field_updates", 0):
        LOG.debug("field updates recus, pas besoin de scrutation")
        return
    LOG.warning("aucun field update apres %.0f s -> scrutation des dumps "
                "toutes les %.2f s", grace, period)
    while True:
        try:
            await link.request_dump()
        except Exception as exc:  # lien coupe
            LOG.debug("scrutation interrompue : %s", exc)
            return
        await asyncio.sleep(period)


async def watch_battery(link: "PegasusLink", period: float = 120.0) -> None:
    """Redemande l'etat de la batterie de temps en temps."""
    while True:
        await asyncio.sleep(period)
        try:
            await link.request_battery()
        except Exception as exc:
            LOG.debug("lecture batterie impossible : %s", exc)
            return


async def cmd_raw(args) -> int:
    """Console brute : envoyer des octets a la carte, voir ce qui revient."""
    device = await find_board(args.address, args.name, args.timeout)
    link = PegasusLink(lambda occ: None, lambda sq, o: None,
                       flip=args.flip, trace=True,
                       devkey=parse_devkey(args.devkey),
                       accept_weird_dumps=True)
    await link.connect(device, handshake=not args.no_handshake,
                       kind=args.handshake)

    if args.led_test:
        print("\nTest LED : la rangee du haut doit clignoter pendant 3 s.")
        await link.leds([0, 1, 2, 3, 4, 5, 6, 7])
        await asyncio.sleep(3.0)
        await link.leds([])

    async def send(text: str) -> None:
        cleaned = text.replace(":", " ").replace(",", " ").replace("0x", " ")
        try:
            data = bytes.fromhex("".join(cleaned.split()))
        except ValueError:
            print(f"  ! hexadecimal illisible : {text!r}")
            return
        if not data:
            return
        print(f">>> {data.hex(' ')}")
        await link.write(data, response=not args.no_response)

    try:
        for item in args.send or []:
            await send(item)
            await asyncio.sleep(args.wait)
        if not args.send or args.interactive:
            print("\nTapez des octets en hexa (ex. `42` pour un board dump, `44` "
                  "pour le mode streaming, `40` reset).")
            print("`q` pour quitter. Les reponses s'affichent au fur et a mesure.\n")
            loop = asyncio.get_running_loop()
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line or line.strip().lower() in ("q", "quit", "exit"):
                    break
                await send(line.strip())
                await asyncio.sleep(args.wait)
        else:
            await asyncio.sleep(args.wait)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await link.disconnect()
    return 0


async def cmd_diag(args) -> int:
    """Interroge la carte sur tout ce qu'elle sait dire, cle avant / apres."""
    device = await find_board(args.address, args.name, args.timeout)
    link = PegasusLink(lambda occ: None, lambda sq, o: None,
                       trace=True, devkey=parse_devkey(args.devkey),
                       accept_weird_dumps=True)
    await link.connect(device, handshake=False)

    async def ask(frame: bytes, label: str, pause: float = 0.6) -> None:
        print(f"\n--- {label}  ({frame.hex(' ')})")
        await link.write(frame)
        await asyncio.sleep(pause)

    print("\n===== AVANT la cle developpeur =====")
    await ask(bytes([CMD_LOCK_STATE]), "etat de verrouillage")
    await ask(bytes([CMD_DEVKEY_STATE]), "etat de l'autorisation")
    await ask(bytes([CMD_BOARD_DUMP]), "board dump")

    print("\n===== envoi de la cle developpeur =====")
    await ask(link.devkey_frame(), "cle developpeur")
    await ask(bytes([CMD_LOCK_STATE]), "etat de verrouillage")
    await ask(bytes([CMD_DEVKEY_STATE]), "etat de l'autorisation")
    await ask(bytes([CMD_BOARD_DUMP]), "board dump")

    print("\n===== autres informations =====")
    await ask(bytes([CMD_SERIALNR]), "numero de serie")
    await ask(bytes([CMD_LONG_SERIALNR]), "numero de serie long")
    await ask(bytes([CMD_VERSION]), "version logicielle")
    await ask(bytes([CMD_HARDWARE_VERSION]), "version materielle")
    await ask(bytes([CMD_BUSADRES]), "adresse de bus (0x46)")
    await ask(bytes([CMD_EE_MOVES]), "memoire de coups (0x49)")
    await ask(bytes([CMD_UNKNOWN_56]), "commande inconnue 0x56")
    await ask(bytes([CMD_BATTERY]), "batterie")

    print("\n===== mode streaming puis dumps =====")
    await ask(bytes([CMD_RESET]), "reset")
    await ask(bytes([CMD_FIELD_UPDATE_MODE]), "mode field update")
    print("\nBougez une piece maintenant : 10 s d'ecoute, puis 3 dumps.\n")
    await asyncio.sleep(10.0)
    for i in range(3):
        await ask(bytes([CMD_BOARD_DUMP]), f"board dump {i + 1}/3", 0.8)

    print(f"\nBilan : {link.dumps} dump(s), {link.field_updates} field update(s).")
    await link.disconnect()
    return 0


async def cmd_watch(args) -> int:
    """Scrute les dumps et n'affiche QUE les changements. Silencieux sinon."""
    device = await find_board(args.address, args.name, args.timeout)
    state = {"last": None, "changes": 0, "dumps": 0, "since": time.monotonic()}

    def grid(values: bytes) -> str:
        rows = []
        for r in range(8):
            row = " ".join(f"{values[r * 8 + c]:02x}" for c in range(8))
            rows.append(f"  {8 - r}  {row}")
        rows.append("     a  b  c  d  e  f  g  h")
        return "\n".join(rows)

    def on_raw(values: bytes) -> None:
        state["dumps"] += 1
        if values == state["last"]:
            return
        state["changes"] += 1
        previous = state["last"]
        state["last"] = values
        now = time.strftime("%H:%M:%S")
        distinct = sorted(set(values))
        print(f"\n[{now}] changement #{state['changes']}  "
              f"(dump n°{state['dumps']})  valeurs : "
              + " ".join(f"0x{v:02x}" for v in distinct))
        if previous is not None:
            moved = [i for i in range(64) if previous[i] != values[i]]
            names = ", ".join(
                f"{chess.square_name(dgt_to_square(i))}"
                f" {previous[i]:02x}->{values[i]:02x}" for i in moved[:12])
            print(f"  cases modifiees ({len(moved)}) : {names}")
        print(grid(values))

    link = PegasusLink(lambda occ: None, lambda sq, o: None,
                       trace=False, devkey=parse_devkey(args.devkey),
                       accept_weird_dumps=True, on_raw=on_raw)
    await link.connect(device, kind=args.handshake)

    # La detection du Pegasus est inductive : seules ses propres pieces
    # (insert metallique) declenchent les capteurs, pas un aimant.
    LOG.setLevel(logging.WARNING)  # silence : seuls les changements comptent
    print("\n" + "=" * 62)
    print("  Surveillance : seuls les CHANGEMENTS s'affichent.")
    print("  Posez une piece du plateau sur une case, puis retirez-la.")
    print("  Si rien ne s'affiche, aucun capteur ne reagit. Ctrl+C pour sortir.")
    print("  (detection inductive : un aimant ne suffit pas, il faut les")
    print("   pieces du Pegasus. Le plateau doit avoir ete calibre.)")
    print("=" * 62)

    try:
        while True:
            await link.request_dump()
            await asyncio.sleep(args.period)
            if link.client is not None and not link.client.is_connected:
                print("lien BLE perdu")
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        elapsed = time.monotonic() - state["since"]
        print(f"\n{state['dumps']} dump(s) en {elapsed:.0f} s, "
              f"{state['changes']} changement(s), "
              f"{link.field_updates} field update(s).")
        await link.disconnect()
    return 0


async def cmd_probe(args) -> int:
    if chess is None:
        raise SystemExit("module `chess` manquant : pip install chess")
    device = await find_board(args.address, args.name, args.timeout)
    tracker = BoardTracker(max_depth=args.max_depth, auto_reset=not args.no_auto_reset)
    dirty = asyncio.Event()

    def on_dump(occupied):
        tracker.set_occupancy(occupied)
        dirty.set()

    def on_field(sq, occupied):
        tracker.set_square(sq, occupied)
        dirty.set()

    link = PegasusLink(on_dump, on_field, flip=args.flip, trace=args.trace,
                       devkey=parse_devkey(args.devkey),
                       accept_weird_dumps=args.accept_weird_dumps,
                       invert=args.invert)
    await link.connect(device, kind=args.handshake)
    poller = asyncio.create_task(poll_dumps(link, args.poll or 0.3))
    print("\nBougez des pieces : l'occupation doit changer. Ctrl+C pour quitter.\n")
    try:
        while True:
            try:
                await asyncio.wait_for(dirty.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            dirty.clear()
            await asyncio.sleep(args.settle)
            tracker.resolve()
            print("\033[2J\033[H", end="")
            print(tracker.ascii())
            print(f"\ncases occupees : {len(tracker.occ)}   synchro : "
                  f"{'oui' if tracker.in_sync else 'non'}")
            print(f"FEN : {tracker.fen()}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        poller.cancel()
        await link.disconnect()
    return 0


async def cmd_serve(args) -> int:
    if chess is None:
        raise SystemExit("module `chess` manquant : pip install chess")

    tracker = BoardTracker(
        promotion={"q": chess.QUEEN, "r": chess.ROOK, "b": chess.BISHOP,
                   "n": chess.KNIGHT}[args.promotion],
        max_depth=args.max_depth,
        auto_reset=not args.no_auto_reset,
    )

    slave_fd = None
    if args.device:
        fd = open_device(args.device)
        port_path = args.device
        # Cote LiveChess, c'est l'autre bout de la paire qu'il faut ouvrir.
        peer = args.peer or tty0tty_peer(args.device)
        for target in (args.device, peer):
            if not target:
                continue
            try:
                os.chmod(target, int(args.chmod, 8))
            except OSError as exc:
                LOG.warning("droits sur %s : %s", target, exc)
        if args.link:
            if not peer:
                LOG.error("--link avec --device demande --peer (l'autre bout "
                          "de la paire, que LiveChess doit ouvrir)")
            else:
                try:
                    if os.path.islink(args.link) or os.path.exists(args.link):
                        os.remove(args.link)
                    os.symlink(peer, args.link)
                    LOG.info("lien symbolique %s -> %s (a ouvrir dans LiveChess)",
                             args.link, peer)
                except OSError as exc:
                    LOG.error("impossible de creer %s : %s", args.link, exc)
        elif peer:
            LOG.info("LiveChess doit ouvrir l'autre bout de la paire : %s", peer)
    else:
        fd, slave_fd, port_path = open_pty(
            args.link, mode=int(args.chmod, 8), owner=args.owner)

    frontend = SerialFrontend(
        fd, tracker,
        serial_nr=args.serial_nr,
        trademark=args.trademark,
        bus_address=args.bus_address,
        dump_on_update=not args.no_dump_on_update,
        debug=args.debug,
    )
    tracker.on_change = frontend.on_board_changed

    loop = asyncio.get_running_loop()
    dirty = asyncio.Event()

    def on_readable():
        try:
            data = os.read(fd, 512)
        except BlockingIOError:
            return
        except OSError as exc:
            LOG.debug("lecture serie : %s", exc)
            return
        if data:
            frontend.feed(data)

    loop.add_reader(fd, on_readable)

    def on_dump(occupied):
        tracker.set_occupancy(occupied)
        dirty.set()

    def on_field(sq, occupied):
        tracker.set_square(sq, occupied)
        dirty.set()

    device = await find_board(args.address, args.name, args.timeout)
    link = PegasusLink(on_dump, on_field, flip=args.flip,
                       trace=getattr(args, "trace", False),
                       devkey=parse_devkey(getattr(args, "devkey", None)),
                       accept_weird_dumps=getattr(args, "accept_weird_dumps", False),
                       invert=getattr(args, "invert", False))
    await link.connect(device, kind=getattr(args, "handshake", "ext"))
    poller = asyncio.create_task(poll_dumps(link, args.poll or 0.3))
    battery = asyncio.create_task(watch_battery(link))
    silence = asyncio.create_task(warn_if_silent(frontend, port_path))

    print()
    print("=" * 62)
    print(f"  Carte DGT virtuelle prete sur : {port_path}")
    if args.link:
        print(f"  (alias : {args.link})")
    print("  Pointez LiveChess sur ce port. Ctrl+C pour arreter.")
    print("=" * 62)
    print()

    try:
        while True:
            try:
                await asyncio.wait_for(dirty.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                if link.client is not None and not link.client.is_connected:
                    LOG.error("lien BLE perdu")
                    break
                continue
            dirty.clear()
            await asyncio.sleep(args.settle)
            dirty.clear()
            tracker.resolve()
            frontend.on_board_changed()
            if args.show_board:
                print("\033[2J\033[H", end="")
                print(tracker.ascii())
                print(f"\n{tracker.fen()}\nport : {port_path}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        poller.cancel()
        battery.cancel()
        silence.cancel()
        loop.remove_reader(fd)
        await link.disconnect()
        os.close(fd)
        if slave_fd is not None:
            os.close(slave_fd)
        if args.link and os.path.islink(args.link):
            try:
                os.remove(args.link)
            except OSError:
                pass
    return 0


# ---------------------------------------------------------------------------

def add_global_flags(sp) -> None:
    """Accepter -v / --debug aussi bien avant qu'apres la sous-commande.

    Sans SUPPRESS, le sous-parseur reecrirait a False la valeur donnee avant
    la sous-commande : `--debug serve` serait silencieusement ignore.
    """
    sp.add_argument("-v", "--verbose", action="store_true",
                    default=argparse.SUPPRESS, help="logs detailles")
    sp.add_argument("--debug", action="store_true",
                    default=argparse.SUPPRESS,
                    help="tracer tous les octets echanges avec LiveChess")
    sp.add_argument("--debug-ble", action="store_true",
                    default=argparse.SUPPRESS,
                    help="ne pas taire les journaux bleak / dbus")


# ---------------------------------------------------------------------------
# Lichess : API Board (mode direct, sans LiveChess)
# ---------------------------------------------------------------------------

LICHESS = "https://lichess.org"
TOKEN_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "pegasus-bridge", "token")
TOKEN_URL = (LICHESS + "/account/oauth/token/create"
             "?scopes[]=board:play&description=Pegasus%20bridge")


def load_token(explicit: Optional[str] = None) -> Optional[str]:
    """Jeton : option, puis $LICHESS_TOKEN, puis le fichier de configuration."""
    if explicit:
        return explicit.strip()
    env = os.environ.get("LICHESS_TOKEN")
    if env:
        return env.strip()
    try:
        with open(TOKEN_PATH, encoding="ascii") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def save_token(token: str) -> None:
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(token.strip() + "\n")
    LOG.info("jeton enregistre dans %s (lisible par vous seul)", TOKEN_PATH)


class Lichess:
    """Le strict necessaire de l'API Board, sur la bibliotheque standard."""

    def __init__(self, token: str, base: str = LICHESS) -> None:
        self.token = token
        self.base = base

    def _open(self, path: str, method: str = "GET",
              data: Optional[bytes] = None, timeout: Optional[float] = 30.0):
        req = urllib.request.Request(self.base + path, method=method, data=data)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("User-Agent", "pegasus-bridge")
        return urllib.request.urlopen(req, timeout=timeout)

    def get_json(self, path: str) -> dict:
        with self._open(path) as resp:
            return json.load(resp)

    def post(self, path: str) -> dict:
        with self._open(path, method="POST", data=b"") as resp:
            body = resp.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else {}

    def form(self, path: str, fields: dict) -> dict:
        """POST classique en formulaire (defi contre l'ordinateur...)."""
        import urllib.parse
        data = urllib.parse.urlencode(
            {k: ("true" if v is True else "false" if v is False else v)
             for k, v in fields.items() if v is not None}).encode()
        req = urllib.request.Request(self.base + path, method="POST", data=data)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("User-Agent", "pegasus-bridge")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else {}

    def seek(self, fields: dict, keep) -> None:
        """Cree une recherche d'adversaire et MAINTIENT la connexion ouverte.

        L'API est explicite : tant que la connexion vit, la recherche est
        active ; la fermer l'annule. C'est ce qui permet d'annuler en reposant
        simplement les pieces. A lancer dans un executeur.
        """
        import urllib.parse
        data = urllib.parse.urlencode(
            {k: ("true" if v is True else "false" if v is False else v)
             for k, v in fields.items() if v is not None}).encode()
        req = urllib.request.Request(self.base + "/api/board/seek",
                                     method="POST", data=data)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("User-Agent", "pegasus-bridge")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=None) as resp:
                keep(resp)              # l'appelant garde la main pour annuler
                for _ in resp:
                    pass                # le flux ne dit rien, il occupe la place
        except Exception as exc:
            LOG.debug("recherche d'adversaire terminee (%s)", exc)
        finally:
            keep(None)

    def stream(self, path: str, emit, stop: threading.Event,
               reconnect_on_eof: bool = True) -> None:
        """Lit un flux NDJSON jusqu'a l'arret. A lancer dans un executeur.

        `reconnect_on_eof` distingue les deux usages. Le flux d'evenements est
        ferme periodiquement par lichess et doit etre repris. Un flux de partie,
        lui, se ferme quand la partie se termine : le reprendre rejouerait sans
        fin l'annonce d'une partie finie, qui ecrasait la partie suivante.
        """
        while not stop.is_set():
            try:
                with self._open(path, timeout=None) as resp:
                    for raw in resp:
                        if stop.is_set():
                            return
                        raw = raw.strip()
                        if raw:  # les lignes vides sont des battements de coeur
                            emit(json.loads(raw))
                if not reconnect_on_eof:
                    LOG.debug("flux %s ferme par lichess", path)
                    return
            except Exception as exc:  # coupure reseau, 429...
                if stop.is_set():
                    return
                LOG.warning("flux %s interrompu (%s), reprise dans 3 s", path, exc)
                time.sleep(3.0)


class LedDirector:
    """Gere les LED : un etat permanent, et des animations par-dessus.

    Decouverte importante : le plateau ne se contente pas d'allumer la liste
    qu'on lui envoie, il la PARCOURT, case apres case, a la vitesse donnee par
    le troisieme octet de la trame. C'est lui qui anime, pas nous.

    D'ou l'echec des premieres animations : on envoyait la rangee puis on
    l'eteignait toutes les 130 ms pour la faire clignoter, ce qui relancait le
    parcours depuis le debut a chaque fois — seule la premiere case avait le
    temps d'apparaitre. Avec la vitesse 0x02 d'origine, le parcours etait en
    plus si lent qu'on n'en voyait que trois cases en deux secondes et demie.

    La bonne facon : envoyer la liste UNE fois, a la bonne cadence, et laisser
    le plateau derouler sans y toucher. A 0x40 le parcours est si rapide que
    seize cases paraissent s'allumer d'un bloc.

    Chaque evenement a ses propres reglages (cadence, duree, repetitions),
    modifiables en direct depuis l'interface : le dictionnaire est partage,
    donc bouger une jauge change l'animation suivante sans rien relancer.
    """

    SPEED_BASE = 0x40        # etat permanent : guidage, cases levees
    SPEED_ANIM = 0x40        # animations : rangees, clins d'oeil

    def __init__(self, link: "PegasusLink", enabled: bool = True,
                 style: str = "row", speed: Optional[int] = None,
                 settings: Optional[dict] = None) -> None:
        self.link = link
        self.enabled = enabled
        self.style = style
        self.cfg = settings if settings is not None else \
            normalize_led_settings(None)
        # --led-speed impose la meme cadence partout, quoi que dise le fichier.
        self.speed_override = speed
        self.base: Tuple[int, ...] = ()
        self.base_kind = "adverse"
        self.shown: Optional[Tuple] = None
        self.task: Optional[asyncio.Task] = None
        self.pulse: Optional[asyncio.Task] = None

    # -- lecture des reglages --------------------------------------------
    def speed_of(self, kind: str) -> int:
        if self.speed_override is not None:
            return self.speed_override
        return int(self.cfg.get(kind, {}).get("vitesse", self.SPEED_ANIM))

    def hold_of(self, kind: str) -> float:
        return float(self.cfg.get(kind, {}).get("duree", 0.0))

    def times_of(self, kind: str) -> int:
        return max(1, int(self.cfg.get(kind, {}).get("repetitions", 1)))

    @property
    def speed(self) -> int:          # garde l'ancien nom, pratique et lisible
        return self.speed_of("debut")

    # -- envoi ------------------------------------------------------------
    async def _write(self, squares: Sequence[int],
                     speed: Optional[int] = None) -> None:
        vitesse = self.SPEED_BASE if speed is None else speed
        wanted = (tuple(squares), vitesse)
        if wanted == self.shown or not self.enabled:
            return
        self.shown = wanted
        try:
            await self.link.leds(list(squares), speed=vitesse)
        except Exception as exc:
            LOG.debug("commande LED ignoree : %s", exc)

    # -- etat permanent ---------------------------------------------------
    async def set_base(self, squares: Sequence[int],
                       kind: str = "adverse") -> None:
        """L'etat de fond : guidage, cases levees, cases a corriger.

        Si la duree reglee pour cet evenement est nulle, les cases restent
        simplement allumees ; sinon elles battent a ce rythme jusqu'a ce que
        le fond change.
        """
        nouveau = tuple(sorted(set(squares)))
        change = nouveau != self.base or kind != self.base_kind
        self.base, self.base_kind = nouveau, kind
        if change and self.task is not None and not self.task.done():
            # Le fond a change (l'adversaire a joue) : l'animation en cours
            # n'a plus lieu d'etre et ne doit pas rester a briller.
            self.task.cancel()
        if self.task is None or self.task.done() or change:
            await self._apply_base()

    def _stop_pulse(self) -> None:
        if self.pulse is not None and not self.pulse.done():
            self.pulse.cancel()
        self.pulse = None

    async def _apply_base(self) -> None:
        self._stop_pulse()
        periode = self.hold_of(self.base_kind)
        vitesse = self.speed_of(self.base_kind)
        if self.base and periode > 0 and self._in_loop():
            self.pulse = asyncio.create_task(self._pulse_loop(periode, vitesse))
        else:
            await self._write(self.base, vitesse)

    async def _pulse_loop(self, periode: float, vitesse: int,
                          gap: Optional[float] = None) -> None:
        """Allume, attend, eteint, recommence.

        L'extinction n'est pas qu'un temps mort : c'est elle qui fait
        repartir le parcours du plateau depuis la premiere case. Pour un
        battement, elle dure aussi longtemps que l'allumage ; pour une
        animation qui tourne, juste le temps de relancer le tour.
        """
        noir = periode if gap is None else gap
        try:
            while True:
                await self._write(self.base, vitesse)
                await asyncio.sleep(periode)
                await self._write((), vitesse)
                await asyncio.sleep(noir)
        except asyncio.CancelledError:
            pass

    # -- animations -------------------------------------------------------
    @staticmethod
    def _in_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    def play(self, steps) -> None:
        """Joue une animation, puis rend la main a l'etat permanent.

        Chaque etape est (cases, duree) ou (cases, duree, vitesse).
        """
        if not self.enabled or not self._in_loop():
            return      # hors boucle (tests, interface seule) : on s'abstient
        self._stop_pulse()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = asyncio.create_task(self._run(list(steps)))

    async def _run(self, steps) -> None:
        try:
            for etape in steps:
                squares, duree = etape[0], etape[1]
                vitesse = etape[2] if len(etape) > 2 else None
                await self._write(squares, vitesse)
                await asyncio.sleep(duree)
        except asyncio.CancelledError:
            pass
        finally:
            with contextlib.suppress(Exception):
                await self._apply_base()

    def blink(self, cases: Sequence[int], kind: str) -> None:
        """Le motif commun : N clignotements d'une meme liste de cases."""
        vitesse, duree = self.speed_of(kind), self.hold_of(kind)
        steps = []
        for _ in range(self.times_of(kind)):
            steps.append((list(cases), duree, vitesse))
            steps.append(((), min(duree, 0.2)))
        self.play(steps)

    # -- animations toutes faites ----------------------------------------
    @staticmethod
    def rank_of(color: bool) -> List[int]:
        rank = 0 if color == chess.WHITE else 7
        return [chess.square(f, rank) for f in range(8)]

    def sweep_of(self, color: bool) -> List[int]:
        """La rangee d'une couleur, parcourue depuis la gauche du joueur.

        Les blancs voient a1 a leur gauche, les noirs h8 : on inverse pour les
        noirs, le balayage est alors symetrique vu de chaque siege.
        """
        rang = self.rank_of(color)
        return rang if color == chess.WHITE else list(reversed(rang))

    # Le tour du carre central, dans le sens des aiguilles : le plateau le
    # parcourt tout seul, ce qui donne une lumiere qui tourne. Utilise pendant
    # la recherche d'un adversaire, ou l'attente peut durer.
    RING = ["c3", "c4", "c5", "c6", "d6", "e6",
            "f6", "f5", "f4", "f3", "e3", "d3"]

    @classmethod
    def ring(cls) -> List[int]:
        return [chess.parse_square(c) for c in cls.RING]

    def searching(self) -> None:
        """Recherche en cours : une lumiere tourne au centre, sans fin."""
        if not self.enabled or not self._in_loop():
            return
        self._stop_pulse()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None
        self.base = tuple(self.ring())
        self.base_kind = "recherche"
        self.pulse = asyncio.create_task(
            self._pulse_loop(self.hold_of("recherche"),
                             self.speed_of("recherche"), gap=0.08))

    def stop_searching(self) -> None:
        """Fin de la recherche : on rend le plateau au noir."""
        self._stop_pulse()
        if self.base_kind == "recherche":
            self.base, self.base_kind = (), "adverse"
        if self._in_loop():
            asyncio.create_task(self._write((), self.speed_of("adverse")))

    def draw_squares(self) -> List[int]:
        """Les deux rangees entrelacees : deux lumieres avancent de front."""
        return [c for paire in zip(self.sweep_of(chess.WHITE),
                                   self.sweep_of(chess.BLACK)) for c in paire]

    def welcome(self, color: bool) -> None:
        """Debut de partie : ta rangee s'allume, pour dire ou t'installer."""
        if self.style == "chase":
            steps = []
            for _ in range(3):
                steps += [([c], 0.05) for c in self.sweep_of(color)]
            steps.append(((), 0.0))
            self.play(steps)
        else:
            self.blink(self.sweep_of(color), "debut")

    def farewell(self, winner: Optional[bool]) -> None:
        """Fin de partie : la rangee du gagnant, ou les deux si c'est nulle."""
        cases = self.draw_squares() if winner is None else self.sweep_of(winner)
        if self.style == "chase":
            steps = []
            for _ in range(2):
                steps += [([c], 0.05) for c in cases]
            steps.append(((), 0.0))
            self.play(steps)
        else:
            self.blink(cases, "fin")

    def confirm(self, frm: int, to: int) -> None:
        """Coup accepte par lichess : un clin d'oeil sur le trajet."""
        self.blink([frm, to], "confirme")

    def reject(self, frm: int, to: int) -> None:
        """Coup refuse : un battement rapide et insistant."""
        self.blink([frm, to], "refuse")

    def demo(self, kind: str) -> None:
        """Rejoue une animation a la demande, pour regler les jauges.

        Les deux etats permanents (coup adverse, piece levee) n'ont pas de fin
        propre : on les montre quelques secondes puis on rend la main.
        """
        if kind == "debut":
            self.welcome(chess.WHITE)
        elif kind == "debut-noir":
            self.welcome(chess.BLACK)
        elif kind == "fin":
            self.farewell(chess.WHITE)
        elif kind == "fin-nulle":
            self.farewell(None)
        elif kind == "confirme":
            self.confirm(chess.E2, chess.E4)
        elif kind == "refuse":
            self.reject(chess.E2, chess.E4)
        elif kind == "recherche":
            self.searching()
        elif kind == "stop":
            self.stop_searching()
        elif kind in ("adverse", "levee"):
            cases = [chess.E7, chess.E5] if kind == "adverse" else [chess.E2]
            duree, vitesse = self.hold_of(kind), self.speed_of(kind)
            if duree <= 0:
                steps = [(cases, 2.5, vitesse), ((), 0.0)]
            else:
                steps = []
                while sum(e[1] for e in steps) < 2.5:
                    steps += [(cases, duree, vitesse), ((), duree)]
            self.play(steps)


class PlaySession:
    """Accorde le plateau physique et la partie lichess.

    Deux listes de coups : celle de lichess et celle deduite du plateau. Tant
    que le plateau est en retard, les cases du coup a rejouer sont allumees ;
    des qu'il prend un coup d'avance et que c'est notre tour, ce coup part.
    """

    def __init__(self, tracker: BoardTracker, link: "PegasusLink",
                 lichess: Lichess, use_leds: bool = True,
                 led_style: str = "row", led_speed: Optional[int] = None,
                 led_settings: Optional[dict] = None) -> None:
        self.tracker = tracker
        self.link = link
        self.lichess = lichess
        self.use_leds = use_leds
        self.leds = LedDirector(link, enabled=use_leds, style=led_style,
                                speed=led_speed, settings=led_settings)
        self.clock = {"wtime": None, "btime": None, "winc": 0, "binc": 0,
                      "at": None, "running": None}
        self.game_id: Optional[str] = None
        self.color: Optional[bool] = None
        self.initial_fen = "startpos"
        self.moves: List[str] = []
        self.finished = False
        self.lit: Tuple[int, ...] = ()
        self.last_mismatch: Optional[Tuple[int, ...]] = None
        self.sending = False
        self.account = ""
        self.opponent = ""
        self.speed = ""
        self.launcher = None

    def status_line(self) -> str:
        """Une phrase decrivant l'etat courant, pour l'interface."""
        if self.game_id is None:
            return "En attente d'une partie sur lichess"
        if self.finished:
            return "Partie terminee — range les pieces, la suivante viendra"
        if not self.tracker.in_sync:
            extra, missing = self.tracker.mismatch()
            return f"Plateau a corriger : {len(extra) + len(missing)} case(s)"
        if self.lit:
            return "Rejoue le coup adverse (cases allumees)"
        mine = self.tracker.logic.turn == self.color
        return "A toi de jouer" if mine else "En attente de l'adversaire"

    def start_board(self) -> "chess.Board":
        if self.initial_fen in ("startpos", "", None):
            return chess.Board()
        return chess.Board(self.initial_fen)

    def begin(self, game_id: str, color: bool, initial_fen: str,
              moves: Sequence[str]) -> None:
        self.game_id = game_id
        self.color = color
        self.initial_fen = initial_fen or "startpos"
        self.moves = list(moves)
        self.finished = False
        self.last_mismatch = None
        # On se cale sur la position ACTUELLE, pas sur le debut de la partie :
        # brancher le pont au 30e coup ne doit pas obliger a rejouer les 29
        # premiers. Les coups sont rejoues dans la partie suivie pour que les
        # deux listes concordent, mais le plateau, lui, reste ou il est.
        current = self.start_board()
        for uci in self.moves:
            try:
                current.push_uci(uci)
            except ValueError:
                LOG.warning("coup %s illisible dans l'historique lichess", uci)
                break
        self.tracker.reset_to(current)
        self.leds.welcome(color)
        LOG.info("partie %s : vous jouez les %s, reprise au demi-coup %d",
                 game_id, "blancs" if color == chess.WHITE else "noirs",
                 len(self.moves))

    def update_moves(self, moves: Sequence[str]) -> bool:
        """Enregistre la liste de coups de lichess. True si elle a raccourci.

        Une reprise d'un seul demi-coup est indiscernable, par simple
        comparaison de listes, du cas normal « le plateau a un coup d'avance ».
        Seule la diminution effective de la liste tranche.
        """
        shrunk = len(moves) < len(self.moves)
        self.moves = list(moves)
        self.clock["running"] = self.game_turn()
        return shrunk

    def end_game(self, reason: str = "", winner: Optional[str] = None) -> None:
        """Partie finie : on arrete d'ecouter le plateau jusqu'a la suivante.

        Sans ca, ranger les pieces apres un mat ressemblait a une suite de
        coups aberrants, et le pont passait son temps a reclamer une position
        qui n'avait plus lieu d'etre.
        """
        if self.finished and self.game_id is None:
            return
        self.finished = True
        self.tracker.frozen = True
        self.tracker.expect(None)
        self.last_mismatch = None
        self.clock["running"] = None
        gagnant = ({"white": chess.WHITE, "black": chess.BLACK}.get(winner)
                   if winner else None)
        self.leds.farewell(gagnant)
        LOG.info("partie terminee%s. Le plateau est ignore jusqu'a la "
                 "prochaine partie : range tes pieces tranquillement.",
                 f" ({reason})" if reason else "")

    # -- actions de partie, depuis l'interface ----------------------------
    # Les quatre premieres sont couvertes par le droit « board:play » du
    # jeton. L'ajout de temps passe par une autre route (/api/round/) qui
    # releve d'un autre droit : si lichess refuse, on le dit franchement au
    # lieu de laisser croire que ca a marche.
    ACTIONS = {
        "abandon":       ("/api/board/game/{id}/resign", "abandon"),
        "annuler":       ("/api/board/game/{id}/abort", "partie annulee"),
        "nulle":         ("/api/board/game/{id}/draw/yes",
                          "nulle proposee ou acceptee"),
        "refuser-nulle": ("/api/board/game/{id}/draw/no", "nulle refusee"),
        "reprise":       ("/api/board/game/{id}/takeback/yes",
                          "reprise de coup proposee ou acceptee"),
        "refuser-reprise": ("/api/board/game/{id}/takeback/no",
                            "reprise refusee"),
        "victoire":      ("/api/board/game/{id}/claim-victory",
                          "victoire reclamee"),
    }

    async def action(self, nom: str, seconds: int = 15) -> bool:
        """Joue une action de partie (abandon, nulle, reprise, temps...)."""
        if self.game_id is None or self.finished:
            LOG.warning("aucune partie en cours : « %s » sans effet", nom)
            return False
        if nom == "temps":
            chemin = f"/api/round/{self.game_id}/add-time/{int(seconds)}"
            dit = f"{int(seconds)} s ajoutees a la pendule de l'adversaire"
        elif nom in self.ACTIONS:
            gabarit, dit = self.ACTIONS[nom]
            chemin = gabarit.format(id=self.game_id)
        else:
            LOG.warning("action inconnue : %s", nom)
            return False
        try:
            await asyncio.to_thread(self.lichess.post, chemin)
        except Exception as exc:
            detail = str(exc)
            if nom == "temps" and ("401" in detail or "403" in detail):
                LOG.error("lichess refuse l'ajout de temps : ce jeton n'a "
                          "que le droit « board:play ». Ajoute « challenge:"
                          "write » a ton jeton pour ce bouton.")
            else:
                LOG.error("« %s » refuse par lichess : %s", nom, detail)
            return False
        LOG.info("%s", dit)
        return True

    def board_moves(self) -> List[str]:
        return [m.uci() for m in self.tracker.logic.move_stack]

    async def set_leds(self, squares: Sequence[int],
                       kind: str = "adverse") -> None:
        self.lit = tuple(sorted(set(squares)))
        await self.leds.set_base(self.lit, kind)

    def game_turn(self) -> Optional[bool]:
        """A qui le trait DANS LA PARTIE lichess — pas sur le plateau.

        Les deux different pendant toute ta reflexion : lichess a deja
        enregistre le coup adverse alors que le plateau, lui, attend que tu le
        rejoues. Se fier au plateau figeait ta pendule pendant que tu
        reflechissais, ce qui est exactement le moment ou elle doit tourner.
        """
        if self.game_id is None or self.finished:
            return None
        board = self.start_board()
        return chess.WHITE if (len(self.moves) % 2 == 0) != (not board.turn) \
            else chess.BLACK

    def update_clock(self, state: dict) -> None:
        """Retient les pendules et l'instant de lecture, pour l'egrener."""
        if state.get("wtime") is None:
            return
        self.clock = {
            "wtime": state.get("wtime"),
            "btime": state.get("btime"),
            "winc": state.get("winc", 0),
            "binc": state.get("binc", 0),
            "at": time.monotonic(),
            "running": self.game_turn(),
        }

    def remaining(self, color: bool) -> Optional[float]:
        """Temps restant en secondes, extrapole depuis la derniere lecture."""
        base = self.clock["wtime" if color == chess.WHITE else "btime"]
        if base is None:
            return None
        secondes = base / 1000.0
        if (not self.finished and self.clock["at"] is not None
                and self.clock["running"] == color):
            secondes -= time.monotonic() - self.clock["at"]
        return max(0.0, secondes)

    async def refresh(self, takeback: bool = False) -> None:
        """Compare plateau et partie, puis agit : LED, envoi, ou rien."""
        if self.game_id is None or self.finished:
            return

        # Priorite absolue : si le plateau ne correspond plus a la partie
        # suivie, on eclaire les cases fautives plutot que quoi que ce soit
        # d'autre. C'est la seule facon de se sortir d'un coup illegal.
        leves = tuple(self.tracker.lifted())

        if not self.tracker.in_sync:
            extra, missing = self.tracker.mismatch()
            wrong = tuple(extra) + tuple(missing)
            if wrong != self.last_mismatch:
                self.last_mismatch = wrong
                LOG.warning(
                    "le plateau ne correspond pas a la partie : %d case(s) a "
                    "corriger. En trop : %s. Manquant : %s.",
                    len(wrong),
                    ", ".join(chess.square_name(s) for s in extra) or "rien",
                    ", ".join(chess.square_name(s) for s in missing) or "rien")
            await self.set_leds(wrong[:12])
            return
        self.last_mismatch = None

        game = self.moves
        board = self.board_moves()

        if board == game:
            self.tracker.expect(None)
            # On montre la piece qu'on souleve, avec ses propres reglages.
            await self.set_leds(leves, "levee")
            return

        # Le plateau est en retard : montrer le coup a rejouer.
        if len(game) > len(board) and game[:len(board)] == board:
            nxt = game[len(board)]
            try:
                move = chess.Move.from_uci(nxt)
            except ValueError:
                return
            # On sait exactement quel coup doit arriver : on l'arme, ce qui
            # interdit d'en retenir un autre en chemin (roque, prise en
            # passant, tout ce qui deplace deux pieces).
            self.tracker.expect(move)
            await self.set_leds((move.from_square, move.to_square) + leves,
                                "adverse")
            return

        if len(board) > len(game) and board[:len(game)] == game:
            extra = len(board) - len(game)
            replay = self.start_board()
            for mv in game:
                replay.push_uci(mv)

            # Reprise de coup acceptee sur lichess : la partie recule, le
            # plateau non. Lichess fait foi ; on recale la partie suivie et on
            # demande au joueur de remettre les pieces, cases allumees.
            # Une reprise d'un seul demi-coup ressemble trait pour trait a
            # « le plateau a un coup d'avance » : seul le raccourcissement
            # effectif de la liste, signale par l'appelant, les distingue.
            if takeback or extra > 1:
                LOG.info("reprise de coup sur lichess : %d demi-coup(s) "
                         "annule(s), remettez les pieces comme avant", extra)
                self.tracker.rewind_to(replay)
                await self.refresh()
                return

            uci = board[-1]
            if replay.turn != self.color:
                LOG.warning("coup joue sur le plateau alors que ce n'est pas "
                            "votre tour : %s", uci)
                return
            await self.send(uci)
            return

        LOG.warning("plateau et partie ne se recoupent plus (plateau %d "
                    "demi-coups, partie %d) -> reposez la position de la partie "
                    "telle qu'elle est affichee sur lichess", len(board), len(game))

    async def send(self, uci: str) -> None:
        if self.sending:
            return
        self.sending = True
        try:
            LOG.info("envoi du coup %s a lichess", uci)
            await asyncio.to_thread(
                self.lichess.post, f"/api/board/game/{self.game_id}/move/{uci}")
            await self.set_leds(())
            move = chess.Move.from_uci(uci)
            self.leds.confirm(move.from_square, move.to_square)   # accepte
        except urllib.error.HTTPError as exc:
            LOG.error("lichess refuse %s (%s) : rejouez le coup sur le plateau",
                      uci, exc.code)
            try:
                move = chess.Move.from_uci(uci)
                self.leds.reject(move.from_square, move.to_square)
            except ValueError:
                pass
        except Exception as exc:
            LOG.error("envoi de %s impossible : %s", uci, exc)
        finally:
            self.sending = False

LEDS_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "pegasus-bridge", "leds.json")

# Chaque evenement lumineux a sa cadence (l'octet 3 de la trame, qui regle la
# vitesse a laquelle le plateau parcourt la liste), sa duree, et pour certains
# un nombre de repetitions. Une duree nulle sur « adverse » ou « levee »
# signifie : ca reste allume, sans clignoter.
DEFAULT_LEDS = {
    "debut":    {"vitesse": 64, "duree": 1.8, "repetitions": 1},
    "fin":      {"vitesse": 64, "duree": 1.9, "repetitions": 2},
    "adverse":  {"vitesse": 64, "duree": 0.0, "repetitions": 1},
    "levee":    {"vitesse": 64, "duree": 0.0, "repetitions": 1},
    "confirme": {"vitesse": 64, "duree": 0.22, "repetitions": 1},
    "refuse":   {"vitesse": 64, "duree": 0.06, "repetitions": 6},
    "recherche": {"vitesse": 10, "duree": 1.2, "repetitions": 1},
}

LED_LABELS = {
    "debut": "Debut de partie",
    "fin": "Fin de partie (victoire et nulle)",
    "adverse": "Coup de l'adversaire",
    "levee": "Piece soulevee",
    "confirme": "Coup accepte",
    "refuse": "Coup refuse",
    "recherche": "Recherche d'un adversaire",
}

LED_BOUNDS = {"vitesse": (1, 127), "duree": (0.0, 5.0), "repetitions": (1, 10)}


def normalize_led_settings(data: Optional[dict]) -> dict:
    """Complete et borne des reglages venus d'un fichier ou de l'interface."""
    out = {k: dict(v) for k, v in DEFAULT_LEDS.items()}
    for nom, champs in (data or {}).items():
        if nom not in out or not isinstance(champs, dict):
            continue
        for champ, valeur in champs.items():
            if champ not in out[nom]:
                continue
            bas, haut = LED_BOUNDS[champ]
            try:
                valeur = float(valeur)
            except (TypeError, ValueError):
                continue
            valeur = max(bas, min(haut, valeur))
            out[nom][champ] = valeur if champ == "duree" else int(round(valeur))
    return out


def load_led_settings(path: Optional[str] = None) -> dict:
    """Lit les reglages lumineux ; les valeurs d'usine si le fichier manque."""
    path = path or LEDS_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            return normalize_led_settings(json.load(handle))
    except FileNotFoundError:
        return normalize_led_settings(None)
    except (OSError, ValueError) as exc:
        LOG.warning("%s illisible (%s), reglages lumineux par defaut",
                    path, exc)
        return normalize_led_settings(None)


def save_led_settings(settings: dict, path: Optional[str] = None) -> bool:
    path = path or LEDS_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(normalize_led_settings(settings), handle,
                      indent=2, ensure_ascii=False)
        return True
    except OSError as exc:
        LOG.warning("impossible d'ecrire %s : %s", path, exc)
        return False


GESTURES_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "pegasus-bridge", "gestures.json")

DEFAULT_GESTURES = [
    {
        "nom": "Rapide 10+15 classee",
        "cases": ["a1", "h8"],
        "action": "seek",
        "minutes": 10, "increment": 15, "classee": True,
    },
    {
        "nom": "Blitz 5+0 amicale",
        "cases": ["b1", "g8"],
        "action": "seek",
        "minutes": 5, "increment": 0, "classee": False,
    },
    {
        "nom": "Ordinateur niveau 4, 10+0",
        "cases": ["c1", "f8"],
        "action": "ai",
        "niveau": 4, "minutes": 10, "increment": 0, "couleur": "white",
    },
]


def load_gestures(path: Optional[str] = None) -> List[dict]:
    """Lit les gestes de lancement ; cree le fichier d'exemple au besoin."""
    path = path or GESTURES_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"gestes": DEFAULT_GESTURES}, handle,
                          indent=2, ensure_ascii=False)
            LOG.info("gestes de lancement par defaut ecrits dans %s", path)
        except OSError as exc:
            LOG.debug("impossible d'ecrire %s : %s", path, exc)
        return list(DEFAULT_GESTURES)
    except (OSError, ValueError) as exc:
        LOG.warning("%s illisible (%s), gestes par defaut", path, exc)
        return list(DEFAULT_GESTURES)
    gestes = data.get("gestes", data if isinstance(data, list) else [])
    return [g for g in gestes if g.get("cases")]


def gesture_squares(gesture: dict) -> Optional[frozenset]:
    try:
        return frozenset(chess.parse_square(c.strip().lower())
                         for c in gesture["cases"])
    except (ValueError, KeyError, AttributeError):
        LOG.warning("geste « %s » : cases illisibles %r",
                    gesture.get("nom", "?"), gesture.get("cases"))
        return None


class Launcher:
    """Lance une partie quand on souleve les bonnes pieces au repos.

    Principe : hors partie, si le plateau porte la position initiale PRIVEE
    exactement des cases d'un geste, et que cet etat tient quelques instants,
    on declenche l'action. Remettre les pieces annule une recherche en cours.
    """

    def __init__(self, tracker: BoardTracker, link: "PegasusLink",
                 lichess: "Lichess", gestures: Sequence[dict],
                 delay: float = 1.2, use_leds: bool = True,
                 leds: Optional["LedDirector"] = None) -> None:
        self.tracker = tracker
        self.link = link
        self.lichess = lichess
        self.delay = delay
        self.use_leds = use_leds
        self.enabled = True
        self.start_occ = frozenset(chess.Board().piece_map())
        self.gestures = []
        for g in gestures:
            squares = gesture_squares(g)
            if squares and squares <= self.start_occ:
                self.gestures.append((squares, g))
            elif squares:
                LOG.warning("geste « %s » : ces cases sont vides en position "
                            "initiale, il ne pourra jamais se declencher",
                            g.get("nom", "?"))
        self.leds = leds
        self.candidate: Optional[Tuple[frozenset, float]] = None
        self.pending: Optional[dict] = None       # geste en cours de recherche
        self._told: Optional[tuple] = None        # dernier diagnostic annonce
        self._seek_response = None
        self._seek_lock = threading.Lock()

    def describe(self) -> List[str]:
        return [f"{'+'.join(chess.square_name(s) for s in sorted(sq))} : "
                f"{g.get('nom', '?')}" for sq, g in self.gestures]

    # -- detection --------------------------------------------------------
    def match(self) -> Optional[dict]:
        """Le geste reconnu a l'instant, ou None."""
        missing = self.start_occ - self.tracker.occ
        extra = self.tracker.occ - self.start_occ
        if extra or not missing:
            return None
        for squares, gesture in self.gestures:
            if squares == missing:
                return gesture
        return None

    def diagnose(self) -> Optional[str]:
        """Pourquoi aucun geste ne part, en une phrase.

        Sans ca, un geste qui ne se declenche pas ne laisse aucune trace : on
        ne sait pas si c'est le plateau qui n'est pas en position, les cases
        levees qui ne correspondent a rien, ou les gestes qui dorment.
        """
        if not self.enabled:
            return "les gestes sont desactives (--no-gestures)"
        if not self.gestures:
            return "aucun geste n'est defini"
        extra = self.tracker.occ - self.start_occ
        missing = self.start_occ - self.tracker.occ
        if extra:
            return ("le plateau n'est pas en position de depart : "
                    + ", ".join(chess.square_name(s) for s in sorted(extra))
                    + " ne devrai(en)t pas etre occupee(s)")
        if not missing:
            return None                  # position complete : rien a signaler
        leve = ", ".join(chess.square_name(s) for s in sorted(missing))
        if self.match() is None:
            return (f"cases levees : {leve} — aucun geste ne correspond "
                    f"(connus : {'; '.join(self.describe())})")
        return None

    def _report(self) -> None:
        """Annonce le diagnostic, mais une seule fois par situation."""
        message = self.diagnose()
        etat = (message, self.pending is not None)
        if etat == self._told:
            return
        self._told = etat
        if message:
            LOG.info("geste : %s", message)

    async def poll(self) -> None:
        """Appele a chaque changement du plateau, hors partie."""
        if not self.enabled or not self.gestures:
            self._report()
            return
        now = time.monotonic()
        missing = frozenset(self.start_occ - self.tracker.occ)

        if self.pending is not None:
            # Une recherche tourne : reposer les pieces l'annule.
            if not missing:
                await self.cancel("pieces reposees")
            return

        gesture = self.match()
        if gesture is None:
            self.candidate = None
            self._report()
            return
        self._told = None
        # On laisse le temps de soulever les deux pieces sans declencher un
        # geste plus court en chemin.
        if self.candidate is None or self.candidate[0] != missing:
            self.candidate = (missing, now)
            return
        if now - self.candidate[1] < self.delay:
            return
        self.candidate = None
        await self.fire(gesture)

    # -- action -----------------------------------------------------------
    async def fire(self, gesture: dict) -> None:
        nom = gesture.get("nom", "?")
        action = gesture.get("action", "seek")
        minutes = gesture.get("minutes", 10)
        increment = int(gesture.get("increment", 0))
        couleur = gesture.get("couleur") or "random"
        variante = gesture.get("variante", "standard")
        LOG.info("geste reconnu : %s", nom)
        if self.use_leds:
            squares = self.start_occ - self.tracker.occ
            try:
                await self.link.leds(sorted(squares))
            except Exception:
                pass

        if action == "ai":
            fields = {
                "level": int(gesture.get("niveau", 1)),
                "clock.limit": int(float(minutes) * 60),
                "clock.increment": increment,
                "color": couleur,
                "variant": variante,
            }
            try:
                await asyncio.to_thread(self.lichess.form,
                                        "/api/challenge/ai", fields)
                LOG.info("partie contre l'ordinateur demandee")
            except Exception as exc:
                LOG.error("impossible de lancer la partie : %s", exc)
                await self.clear_leds()
            return

        fields = {
            "rated": bool(gesture.get("classee", False)),
            "time": float(minutes),
            "increment": increment,
            "variant": variante,
        }
        if couleur != "random":
            fields["color"] = couleur
        if gesture.get("fourchette"):
            fields["ratingRange"] = gesture["fourchette"]
        self.pending = gesture
        if self.use_leds and self.leds is not None:
            self.leds.searching()      # une lumiere tourne pendant l'attente
        LOG.info("recherche d'un adversaire : %s %s+%s%s",
                 "classee" if fields["rated"] else "amicale",
                 minutes, increment,
                 " — repose les pieces pour annuler")

        def keep(resp):
            with self._seek_lock:
                self._seek_response = resp

        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, self.lichess.seek, fields, keep)

    async def cancel(self, raison: str = "") -> None:
        if self.pending is None:
            return
        self.pending = None
        with self._seek_lock:
            resp, self._seek_response = self._seek_response, None
        if resp is not None:
            try:
                resp.close()          # fermer la connexion annule la recherche
            except Exception:
                pass
        LOG.info("recherche annulee%s", f" ({raison})" if raison else "")
        await self.clear_leds()

    async def clear_leds(self) -> None:
        if not self.use_leds:
            return
        if self.leds is not None:
            self.leds.stop_searching()
            return
        try:
            await self.link.leds([])
        except Exception:
            pass

    async def game_started(self) -> None:
        self.pending = None
        self.candidate = None
        self._told = None
        await self.clear_leds()
        with self._seek_lock:
            self._seek_response = None


# ---------------------------------------------------------------------------

async def cmd_led(args) -> int:
    """Sonde les LED, maintenant qu'on sait que le plateau anime lui-meme.

    Il ne reste qu'une question ouverte : la nulle. Peut-on faire avancer deux
    lumieres de front, une par rangee ? Les essais entrelacent les deux rangees
    dans une seule liste ; si le plateau les parcourt dans l'ordre donne, ca
    doit ressembler a deux balayages simultanes.
    """
    device = await find_board(args.address, args.name, args.timeout)
    link = PegasusLink(lambda occ: None, lambda sq, o: None,
                       flip=args.flip, devkey=parse_devkey(args.devkey),
                       accept_weird_dumps=True)
    await link.connect(device, kind=args.handshake)

    V = LedDirector.SPEED_ANIM                              # 64, la retenue
    blanc = [chess.square(f, 0) for f in range(8)]           # a1..h1
    noir = [chess.square(f, 7) for f in reversed(range(8))]  # h8..a8
    idx = lambda cases: [link._index(c) for c in cases]
    entrelace = [c for paire in zip(blanc, noir) for c in paire]

    async def essai(titre: str, frame: bytes, attente: float = None) -> None:
        print(f"\n>>> {titre}")
        print(f"    trame : {frame.hex(' ')}")
        await link.leds_raw(frame)
        await asyncio.sleep(attente if attente is not None else args.hold)
        await link.leds_raw(led_frame([]))
        await asyncio.sleep(0.5)

    print("\n" + "=" * 68)
    print(f"  Chaque essai dure {args.hold:.0f} s, puis tout s'eteint.")
    print("  Les essais 1 a 3 sont les animations retenues : verifie-les.")
    print("  Les essais 4 a 8 cherchent la bonne animation de nulle.")
    print("=" * 68)

    await essai("1. debut de partie, cote blanc (a1 vers h1)",
                led_frame(idx(blanc), speed=V))
    await essai("2. debut de partie, cote noir (h8 vers a8)",
                led_frame(idx(noir), speed=V))
    await essai("3. victoire des noirs (meme animation)",
                led_frame(idx(noir), speed=V))

    await essai("4. NULLE : les deux rangees entrelacees, une seule trame",
                led_frame(idx(entrelace), speed=V))
    await essai("5. NULLE : idem, mais deux fois plus vite (vitesse 16)",
                led_frame(idx(entrelace), speed=V * 2))
    await essai("6. NULLE : les deux rangees bout a bout (blancs puis noirs)",
                led_frame(idx(blanc + noir), speed=V))
    await essai("7. NULLE : les seize cases, vitesse tres elevee (64)",
                led_frame(idx(entrelace), speed=64))
    await essai("8. les quatre coins d'un coup (a1 h1 a8 h8)",
                led_frame(idx([chess.A1, chess.H1, chess.A8, chess.H8]),
                          speed=V))

    print("\n>>> 9. reglage de la vitesse : la meme rangee a six cadences")
    for v in (4, 6, 8, 12, 16, 24):
        print(f"    vitesse {v:>2} : {led_frame(idx(blanc), speed=v).hex(' ')}")
        await link.leds_raw(led_frame(idx(blanc), speed=v))
        await asyncio.sleep(args.hold)
        await link.leds_raw(led_frame([]))
        await asyncio.sleep(0.5)

    print("\n>>> 10. luminosite : la meme rangee a six intensites")
    print("    (le 6e octet de la trame ; on n'a jamais su s'il servait)")
    for lum in (1, 2, 4, 16, 64, 255):
        trame = led_frame(idx(blanc), speed=V, brightness=lum)
        print(f"    luminosite {lum:>3} : {trame.hex(' ')}")
        await link.leds_raw(trame)
        await asyncio.sleep(args.hold)
        await link.leds_raw(led_frame([]))
        await asyncio.sleep(0.5)

    await essai("11. l'anneau de recherche, vitesse lente (10)",
                led_frame(idx(LedDirector.ring()), speed=10), attente=4.0)

    if args.raw:
        await essai("12. ta trame", bytes.fromhex(args.raw.replace(":", " ")))

    print("\n" + "=" * 68)
    print("  Essai 10 : est-ce que l'intensite change vraiment, ou est-ce")
    print("  toujours pareil ? C'est la seule chose qu'on ignore encore.")
    print("  Essai 11 : l'anneau de recherche tourne-t-il bien a cette")
    print("  vitesse, ou faut-il la regler autrement ?")
    print("=" * 68 + "\n")
    await link.disconnect()
    return 0


async def cmd_play(args, on_update=None) -> int:
    """Mode direct : plateau <-> lichess, sans LiveChess ni port serie.

    `on_update(session, tracker, event)` est appele a chaque changement d'etat :
    c'est par la que l'interface graphique se tient au courant.
    """
    def notify(event: str = "") -> None:
        if on_update is not None:
            try:
                on_update(session, tracker, event)
            except Exception as exc:  # une IHM qui plante ne doit rien casser
                LOG.debug("rafraichissement de l'interface : %s", exc)
    if chess is None:
        raise SystemExit("module `chess` manquant : pip install chess")

    token = load_token(args.token)
    if not token:
        raise SystemExit(
            "aucun jeton lichess.\n"
            "  1. cree-en un ici (porte « board:play » deja cochee) :\n"
            f"     {TOKEN_URL}\n"
            "  2. relance avec :  --token <jeton> --save-token\n"
            f"     (il sera range dans {TOKEN_PATH}, lisible par toi seul)")
    if args.save_token:
        save_token(token)

    api = Lichess(token)
    try:
        me = await asyncio.to_thread(api.get_json, "/api/account")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SystemExit("jeton refuse par lichess (401). Il a peut-etre "
                             "expire, ou il lui manque la porte « board:play ».")
        raise SystemExit(f"lichess repond {exc.code}")
    except Exception as exc:
        raise SystemExit(f"lichess injoignable : {exc}")
    my_id = me.get("id", "")
    LOG.info("connecte a lichess en tant que %s", me.get("username", my_id))

    tracker = BoardTracker(
        promotion={"q": chess.QUEEN, "r": chess.ROOK, "b": chess.BISHOP,
                   "n": chess.KNIGHT}[args.promotion],
        max_depth=args.max_depth,
        # C'est lichess qui decide quand une partie commence, pas la position
        # initiale posee sur le plateau : sans ca, reposer les pieces au depart
        # en pleine partie repartirait de zero et demanderait de tout rejouer.
        auto_reset=False,
    )
    dirty = asyncio.Event()

    def on_dump(occupied):
        tracker.set_occupancy(occupied)
        dirty.set()

    def on_field(sq, occupied):
        tracker.set_square(sq, occupied)
        dirty.set()

    device = await find_board(args.address, args.name, args.timeout)
    link = PegasusLink(on_dump, on_field, flip=args.flip, trace=args.trace,
                       devkey=parse_devkey(args.devkey),
                       accept_weird_dumps=args.accept_weird_dumps,
                       invert=args.invert)
    await link.connect(device, kind=args.handshake)
    poller = asyncio.create_task(poll_dumps(link, args.poll or 0.3))
    battery = asyncio.create_task(watch_battery(link))
    session = PlaySession(tracker, link, api, use_leds=not args.no_leds,
                          led_style=args.led_style, led_speed=args.led_speed,
                          led_settings=(
                              getattr(args, "led_settings", None)
                              or load_led_settings(
                                  getattr(args, "leds_file", None))))
    session.account = me.get("username", my_id)
    launcher = Launcher(tracker, link, api, load_gestures(args.gestures),
                        delay=args.gesture_delay,
                        use_leds=not args.no_leds, leds=session.leds)
    launcher.enabled = not args.no_gestures
    session.launcher = launcher
    notify("connecte")

    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue" = asyncio.Queue()
    stop = threading.Event()

    def emit(kind):
        return lambda obj: loop.call_soon_threadsafe(queue.put_nowait, (kind, obj))

    loop.run_in_executor(
        None, api.stream, "/api/stream/event", emit("event"), stop, True)
    followed = {"id": None, "stop": None}

    def unfollow() -> None:
        if followed["stop"] is not None:
            followed["stop"].set()
        followed["id"] = None
        followed["stop"] = None

    def follow(game_id: Optional[str]) -> None:
        if not game_id or followed["id"] == game_id:
            return
        unfollow()
        own_stop = threading.Event()
        followed["id"] = game_id
        followed["stop"] = own_stop

        def tagged(obj):
            # Les messages d'une partie qu'on ne suit plus sont jetes : c'est
            # ce qui empechait la partie suivante de prendre la main.
            loop.call_soon_threadsafe(queue.put_nowait, ("game", (game_id, obj)))

        loop.run_in_executor(
            None, api.stream, f"/api/board/game/stream/{game_id}",
            tagged, own_stop, False)
        LOG.info("suivi de la partie %s", game_id)

    async def board_watcher() -> None:
        """Ecoute le plateau, sans jamais s'arreter sur une erreur.

        Une exception ici passait inapercue : la tache mourait, et avec elle
        le suivi du plateau ET les gestes de lancement, sans une ligne dans le
        journal. On attrape, on dit, on continue.
        """
        while True:
            try:
                await dirty.wait()
                dirty.clear()
                await asyncio.sleep(args.settle)
                dirty.clear()
                tracker.resolve()
                await queue.put(("board", None))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.exception("erreur en lisant le plateau (%s) — on continue",
                              exc)
                await asyncio.sleep(0.2)

    watcher = asyncio.create_task(board_watcher())

    # Reprendre une partie deja en cours, le cas echeant.
    if args.game:
        follow(args.game)
    else:
        try:
            playing = await asyncio.to_thread(api.get_json, "/api/account/playing")
            for game in playing.get("nowPlaying", []):
                follow(game["gameId"])
                break
        except Exception as exc:
            LOG.debug("pas de partie en cours (%s)", exc)

    print()
    print("=" * 62)
    print(f"  Pegasus relie a lichess : {me.get('username', my_id)}")
    if link.battery_text():
        print(f"  Batterie du plateau : {link.battery_text()}")
    print("  Lance une partie sur lichess.org, elle sera suivie ici.")
    if launcher.enabled and launcher.gestures:
        print("  Ou souleve les pieces d'un geste, plateau en position de depart :")
        for ligne in launcher.describe():
            print(f"    {ligne}")
    print("  Les cases du coup adverse s'allument ; rejoue-le sur le plateau.")
    print("  Ctrl+C pour arreter.")
    print("=" * 62)
    print()

    takeback = False
    try:
        while True:
            kind, obj = await queue.get()
            if kind == "event":
                etype = obj.get("type")
                if etype == "gameStart":
                    game = obj.get("game", {})
                    await launcher.game_started()
                    await launcher.clear_leds()
                    follow(game.get("gameId") or game.get("id"))
                elif etype == "gameFinish":
                    game = obj.get("game", {})
                    ended = game.get("gameId") or game.get("id")
                    if ended is None or ended == followed["id"]:
                        unfollow()
                        session.end_game()
                        await session.set_leds(())
            elif kind == "game":
                game_id, obj = obj
                if game_id != followed["id"]:
                    continue  # message d'une partie qu'on ne suit plus
                gtype = obj.get("type")
                if gtype == "gameFull":
                    white = (obj.get("white") or {}).get("id")
                    color = chess.WHITE if white == my_id else chess.BLACK
                    state = obj.get("state") or {}
                    session.begin(obj.get("id") or followed["id"], color,
                                  obj.get("initialFen", "startpos"),
                                  (state.get("moves") or "").split())
                    if state.get("status") not in ("created", "started"):
                        session.end_game(state.get("status", ""),
                                         state.get("winner"))
                        unfollow()
                    else:
                        # L'occupation supposee vient d'etre ecrite : on
                        # redemande la vraie pour la confronter tout de suite.
                        await link.request_dump()
                elif gtype == "gameState":
                    takeback = session.update_moves(
                        (obj.get("moves") or "").split())
                    session.update_clock(obj)
                    if obj.get("status") not in ("created", "started"):
                        session.end_game(obj.get("status", ""),
                                         obj.get("winner"))
                        unfollow()
                        await session.set_leds(())
            await session.refresh(takeback=takeback)
            takeback = False
            if session.game_id is None or session.finished:
                try:
                    await launcher.poll()
                except Exception as exc:
                    LOG.exception("erreur dans les gestes (%s) — on continue",
                                  exc)
            notify(kind)
            if args.show_board:
                print("\033[2J\033[H", end="")
                print(tracker.ascii())
                print(f"\n{tracker.fen()}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop.set()
        watcher.cancel()
        poller.cancel()
        battery.cancel()
        await launcher.cancel("arret du pont")
        try:
            await session.set_leds(())
        except Exception:
            pass
        await link.disconnect()
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Pont DGT Pegasus (BLE) -> port serie DGT virtuel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""exemples :
  %(prog)s scan
  %(prog)s probe --address XX:XX:XX:XX:XX:XX
  %(prog)s serve --address XX:XX:XX:XX:XX:XX --show-board
  sudo %(prog)s serve --link /dev/ttyUSB0
  %(prog)s serve --device /dev/tnt1        # paire tty0tty
""",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="logs detailles")
    p.add_argument("--debug", action="store_true", help="trace tous les octets serie")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--address", help="adresse BLE du Pegasus")
        sp.add_argument("--name", help="filtre sur le nom annonce")
        sp.add_argument("--timeout", type=float, default=10.0, help="duree du scan")
        sp.add_argument("--flip", action="store_true",
                        help="plateau tourne de 180 degres (noirs devant vous)")
        sp.add_argument("--settle", type=float, default=0.15,
                        help="temps de stabilisation avant analyse (s)")
        sp.add_argument("--max-depth", type=int, default=2,
                        help="profondeur de recherche des coups (1 ou 2)")
        sp.add_argument("--no-auto-reset", action="store_true",
                        help="ne pas repartir automatiquement sur la position initiale")
        sp.add_argument("--trace", action="store_true",
                        help="afficher toutes les notifications BLE en hexa")
        sp.add_argument("--devkey", help="cle developpeur en hexa (6 octets)")
        sp.add_argument("--accept-weird-dumps", action="store_true",
                        help="accepter un dump ou les 64 cases sont occupees")
        sp.add_argument("--invert", action="store_true",
                        help="inverser le codage de l'occupation (0 = occupee)")
        sp.add_argument("--handshake", default="ext",
                        choices=["ext", "dgt", "both", "none"],
                        help="sequence d'initialisation : ext (cle developpeur "
                             "en premier, defaut), dgt (ordre du pilote Dart), "
                             "both, none")
        sp.add_argument("--poll", type=float, default=0.0,
                        help="periode de scrutation des dumps si la carte ne "
                             "pousse pas de field updates (defaut : 0.3 s, "
                             "declenche automatiquement apres 5 s de silence)")

    sp = sub.add_parser("scan", help="lister les peripheriques BLE")
    sp.add_argument("--timeout", type=float, default=10.0)
    sp.add_argument("--show-uuids", action="store_true",
                    help="lister les services annonces par chaque peripherique")
    add_global_flags(sp)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("watch", help="afficher uniquement les changements "
                                      "de capteurs (test a l'aimant)")
    sp.add_argument("--address", help="adresse BLE du Pegasus")
    sp.add_argument("--name", help="filtre sur le nom annonce")
    sp.add_argument("--timeout", type=float, default=10.0)
    sp.add_argument("--devkey", help="cle developpeur en hexa (6 octets)")
    sp.add_argument("--period", type=float, default=0.3,
                    help="intervalle entre deux dumps")
    sp.add_argument("--handshake", default="ext",
                    choices=["ext", "dgt", "both", "none"])
    add_global_flags(sp)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("led", help="sonder les LED : quelle forme de trame "
                                    "allume vraiment plusieurs cases")
    sp.add_argument("--address", help="adresse BLE du Pegasus")
    sp.add_argument("--name", help="filtre sur le nom annonce")
    sp.add_argument("--timeout", type=float, default=10.0)
    sp.add_argument("--devkey", help="cle developpeur en hexa")
    sp.add_argument("--flip", action="store_true",
                    help="plateau tourne de 180 degres")
    sp.add_argument("--handshake", default="ext",
                    choices=["ext", "dgt", "both", "none"])
    sp.add_argument("--hold", type=float, default=2.5,
                    help="duree d'affichage de chaque essai")
    sp.add_argument("--raw", help="une trame de ton cru, en hexa, testee en fin")
    add_global_flags(sp)
    sp.set_defaults(func=cmd_led)

    sp = sub.add_parser("diag", help="interroger la carte sur tout ce qu'elle "
                                     "sait dire (verrouillage, autorisation...)")
    sp.add_argument("--address", help="adresse BLE du Pegasus")
    sp.add_argument("--name", help="filtre sur le nom annonce")
    sp.add_argument("--timeout", type=float, default=10.0)
    sp.add_argument("--devkey", help="cle developpeur en hexa (6 octets)")
    add_global_flags(sp)
    sp.set_defaults(func=cmd_diag)

    sp = sub.add_parser("raw", help="console brute : envoyer des octets a la carte")
    sp.add_argument("--address", help="adresse BLE du Pegasus")
    sp.add_argument("--name", help="filtre sur le nom annonce")
    sp.add_argument("--timeout", type=float, default=10.0)
    sp.add_argument("--send", action="append",
                    help="octets a envoyer en hexa, repetable (ex. --send 42)")
    sp.add_argument("--wait", type=float, default=1.0,
                    help="attente apres chaque envoi")
    sp.add_argument("--no-handshake", action="store_true",
                    help="ne pas jouer la sequence d'initialisation")
    sp.add_argument("--no-response", action="store_true",
                    help="ecrire sans acquittement (write without response)")
    sp.add_argument("--devkey", help="cle developpeur en hexa (6 octets)")
    sp.add_argument("--interactive", action="store_true",
                    help="rester en console apres les --send")
    sp.add_argument("--handshake", default="ext",
                    choices=["ext", "dgt", "both", "none"],
                    help="sequence d'initialisation a jouer")
    sp.add_argument("--led-test", action="store_true",
                    help="allumer la rangee du haut 3 s : preuve physique que "
                         "la carte obeit")
    sp.add_argument("--flip", action="store_true",
                    help="plateau tourne de 180 degres")
    add_global_flags(sp)
    sp.set_defaults(func=cmd_raw)

    sp = sub.add_parser("probe", help="tester la liaison BLE et voir l'occupation")
    common(sp)
    add_global_flags(sp)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("play", help="jouer sur lichess directement, avec les "
                                     "LED (aucun sudo, aucun LiveChess)")
    common(sp)
    sp.add_argument("--token", help="jeton lichess (sinon $LICHESS_TOKEN, "
                                    "sinon le fichier de configuration)")
    sp.add_argument("--save-token", action="store_true",
                    help="enregistrer le jeton pour les prochaines fois")
    sp.add_argument("--game", help="rejoindre une partie precise par son id")
    sp.add_argument("--no-leds", action="store_true",
                    help="ne pas allumer les cases du coup adverse")
    sp.add_argument("--led-speed", type=int, default=None,
                    help="impose la meme cadence a toutes les LED (octet 3 de "
                         "la trame ; 64 allume tout quasi d'un bloc, 8 donne "
                         "un balayage visible). Sans lui, chaque evenement "
                         "garde la cadence reglee dans l'interface.")
    sp.add_argument("--leds-file", default=None,
                    help="fichier des reglages lumineux (defaut : "
                         "~/.config/pegasus-bridge/leds.json)")
    sp.add_argument("--led-style", default="row", choices=["row", "chase"],
                    help="animations : « row » allume la rangee entiere, "
                         "« chase » fait courir une seule case (a utiliser si "
                         "ton plateau n'allume qu'une case sur huit)")
    sp.add_argument("--gestures",
                    help="fichier des gestes de lancement (defaut : "
                         "~/.config/pegasus-bridge/gestures.json)")
    sp.add_argument("--no-gestures", action="store_true",
                    help="ne pas lancer de partie depuis le plateau")
    sp.add_argument("--gesture-delay", type=float, default=1.2,
                    help="duree pendant laquelle les pieces doivent rester "
                         "levees avant de declencher")
    sp.add_argument("--promotion", default="q", choices=["q", "r", "b", "n"],
                    help="piece de promotion supposee")
    sp.add_argument("--show-board", action="store_true",
                    help="afficher le plateau en continu")
    add_global_flags(sp)
    sp.set_defaults(func=cmd_play)

    sp = sub.add_parser("serve", help="lancer le pont vers un port serie "
                                      "(LiveChess ; demande sudo)")
    common(sp)
    sp.add_argument("--link", help="creer ce lien symbolique vers le pty "
                                   "(ex. /dev/ttyUSB0, demande les droits root)")
    sp.add_argument("--device", help="servir sur un tty existant (ex. /dev/tnt1)")
    sp.add_argument("--bus-address", type=int,
                    help="adresse de bus annoncee (deduite du numero de serie)")
    sp.add_argument("--peer",
                    help="avec --device : l'autre bout de la paire, celui que "
                         "LiveChess ouvre (deduit tout seul pour /dev/tnt*)")
    sp.add_argument("--chmod", default="666",
                    help="droits a poser sur le pty (octal, defaut 666)")
    sp.add_argument("--owner",
                    help="utilisateur a qui donner le pty (defaut : $SUDO_USER)")
    sp.add_argument("--serial-nr", default="10001",
                    help="numero de serie annonce (5 caracteres)")
    sp.add_argument("--trademark",
                    help="chaine d'identification annoncee ; doit commencer par "
                         "« Digital Game Technology\\r\\nCopyright (c) »")
    sp.add_argument("--promotion", default="q", choices=["q", "r", "b", "n"],
                    help="piece de promotion supposee")
    sp.add_argument("--no-dump-on-update", action="store_true",
                    help="ne pas envoyer un board dump en entrant en mode update")
    sp.add_argument("--show-board", action="store_true",
                    help="afficher le plateau en continu")
    add_global_flags(sp)
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.verbose = getattr(args, "verbose", False)
    args.debug = getattr(args, "debug", False)
    logging.basicConfig(
        level=logging.DEBUG if (args.verbose or args.debug) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # bleak et dbus_fast sont assourdissants en DEBUG et noient la trace serie,
    # qui est justement ce qu'on veut lire. --debug-ble les rallume.
    if not getattr(args, "debug_ble", False):
        for noisy in ("bleak", "dbus_fast", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
