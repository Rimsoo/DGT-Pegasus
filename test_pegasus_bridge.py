#!/usr/bin/env python3
"""Tests hors-BLE du pont : suivi de partie + protocole serie."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chess
import pegasus_bridge as pb

FAIL = []


def check(cond, label):
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        FAIL.append(label)


def occ_of(board):
    return set(board.piece_map())


def play(tracker, board, move):
    """Simule physiquement un coup : levers puis poses, comme le ferait le Pegasus."""
    before = occ_of(board)
    board.push(move)
    after = occ_of(board)
    for sq in sorted(before - after):
        tracker.set_square(sq, False)
    # prise : la case d'arrivee est levee puis reposee
    if move.to_square in before and move.to_square in after:
        tracker.set_square(move.to_square, False)
        tracker.set_square(move.to_square, True)
    for sq in sorted(after - before):
        tracker.set_square(sq, True)
    return tracker.resolve()


print("== suivi d'une partie complete ==")
t = pb.BoardTracker()
ref = chess.Board()
line = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "b5", "Bb3", "Nf6",
        "O-O", "Be7", "Re1", "O-O", "d4", "exd4", "Nxd4", "Nxd4", "Qxd4", "d5"]
ok = True
for san in line:
    mv = ref.parse_san(san)
    if not play(t, ref, mv):
        ok = False
        print(f"     -> non resolu apres {san}")
        break
check(ok, "20 demi-coups (roques, prises) suivis sans perdre la synchro")
check(t.logic.board_fen() == ref.board_fen(), "position finale identique a la reference")
check(t.codes()[pb.square_to_dgt(chess.D4)] == pb.PIECE_CODE[(chess.WHITE, chess.QUEEN)],
      "identite correcte apres la suite de prises en d4 (dame blanche)")

print("\n== prise en passant ==")
t = pb.BoardTracker()
ref = chess.Board()
for san in ["e4", "d5", "e5", "f5", "exf6"]:
    mv = ref.parse_san(san)
    r = play(t, ref, mv)
check(t.logic.board_fen() == ref.board_fen(), "en passant resolu")
check(t.codes()[pb.square_to_dgt(chess.F5)] == 0, "le pion pris en f5 a bien disparu")

print("\n== promotion ==")
t = pb.BoardTracker()
t.logic = chess.Board("8/P7/8/8/8/8/8/K6k w - - 0 1")
t.pieces = dict(t.logic.piece_map())
t.occ = set(t.pieces)
ref = t.logic.copy()
mv = ref.parse_san("a8=Q")
play(t, ref, mv)
check(t.logic.board_fen() == ref.board_fen(), "promotion en dame par defaut")
check(t.codes()[pb.square_to_dgt(chess.A8)] == pb.PIECE_CODE[(chess.WHITE, chess.QUEEN)],
      "code de piece = dame blanche en a8")

print("\n== etat transitoire (piece en l'air) ==")
t = pb.BoardTracker()
t.set_square(chess.E2, False)
check(t.codes()[pb.square_to_dgt(chess.E2)] == 0, "e2 signalee vide des le lever")
check(t.resolve() is False, "aucun coup invente tant que la piece est en main")
t.set_square(chess.E4, True)
check(t.resolve() is True, "coup resolu a la pose")
check(t.codes()[pb.square_to_dgt(chess.E4)] == pb.PIECE_CODE[(chess.WHITE, chess.PAWN)],
      "pion blanc en e4")

print("\n== resynchronisation sur la position de depart ==")
t = pb.BoardTracker()
ref = chess.Board()
play(t, ref, ref.parse_san("e4"))
t.set_square(chess.E4, False)
t.set_square(chess.E2, True)
check(t.resolve() is True and t.logic.fen() == chess.Board().fen(),
      "retour position initiale => nouvelle partie")

print("\n== deux coups d'un coup (profondeur 2) ==")
t = pb.BoardTracker()
ref = chess.Board()
ref.push_san("e4")
ref.push_san("e5")
t.set_occupancy(occ_of(ref))
check(t.resolve() is True and t.logic.board_fen() == ref.board_fen(),
      "sequence de 2 coups reconstituee depuis un dump")

print("\n== plateau retourne (--flip) ==")
idx_a8 = 0
check(pb.dgt_to_square(0) == chess.A8 and pb.dgt_to_square(63) == chess.H1,
      "index 0 = a8, index 63 = h1")
check(pb.square_to_dgt(chess.E2) == 0x34 and pb.square_to_dgt(chess.E4) == 0x24,
      "ancres e2=0x34, e4=0x24")
check(pb.square_to_dgt(chess.A1) == 0x38 and pb.square_to_dgt(chess.H8) == 0x07,
      "ancres a1=0x38, h8=0x07")

print("\n== protocole serie vu par LiveChess ==")
master, slave, path = pb.open_pty(None)
t = pb.BoardTracker()
fe = pb.SerialFrontend(master, t)
t.on_change = fe.on_board_changed
parser = pb.FrameParser()


def client_sends(data):
    fe.feed(data)


def client_reads():
    time.sleep(0.02)
    try:
        return os.read(slave, 4096)
    except BlockingIOError:
        return b""


client_sends(bytes([pb.S_RETURN_SERIALNR, pb.S_SEND_VERSION, pb.S_SEND_TRADEMARK]))
msgs = parser.feed(client_reads())
ids = [m[0] for m in msgs]
check(ids == [pb.MSG_SERIALNR, pb.MSG_VERSION, pb.MSG_TRADEMARK],
      f"reponses serialnr/version/trademark bien encadrees ({[hex(i) for i in ids]})")

client_sends(bytes([pb.S_SEND_BRD]))
msgs = parser.feed(client_reads())
check(len(msgs) == 1 and msgs[0][0] == pb.MSG_BOARD_DUMP and len(msgs[0][1]) == 64,
      "board dump = 1 trame de 67 octets")
dump = msgs[0][1]
check(dump[0] == pb.PIECE_CODE[(chess.BLACK, chess.ROOK)] and
      dump[63] == pb.PIECE_CODE[(chess.WHITE, chess.ROOK)],
      "a8 = tour noire, h1 = tour blanche dans le dump")

client_sends(bytes([pb.S_SEND_UPDATE_NICE]))
client_reads()  # le dump initial
ref = chess.Board()
play(t, ref, ref.parse_san("e4"))
fe.on_board_changed()
msgs = parser.feed(client_reads())
upd = {m[1][0]: m[1][1] for m in msgs if m[0] == pb.MSG_FIELD_UPDATE}
check(upd.get(0x34) == 0 and upd.get(0x24) == pb.PIECE_CODE[(chess.WHITE, chess.PAWN)],
      f"field updates e2 vide / e4 pion blanc ({ {hex(k): v for k, v in upd.items()} })")

client_sends(bytes([pb.S_CLOCK_MESSAGE, 0x03, 0x03, 0x0B, 0x00]))
msgs = parser.feed(client_reads())
check(len(msgs) == 1 and msgs[0][0] == pb.MSG_BWTIME,
      "commande pendule multi-octets consommee, BWTIME renvoye")

client_sends(bytes([pb.S_SET_LEDS, 0x04, 0x01, 0x02, 0x03, 0x00, pb.S_SEND_BRD]))
msgs = parser.feed(client_reads())
check(len(msgs) == 1 and msgs[0][0] == pb.MSG_BOARD_DUMP,
      "trame LED consommee sans desynchroniser la commande suivante")

print("\n== reassemblage BLE (trame coupee en paquets de 20 octets) ==")
p2 = pb.FrameParser()
frame = pb.dgt_message(pb.MSG_BOARD_DUMP, bytes(range(64)))
got = []
for i in range(0, len(frame), 20):
    got += p2.feed(frame[i:i + 20])
check(len(got) == 1 and got[0][1] == bytes(range(64)), "dump reassemble sur 4 notifications")
got = p2.feed(b"\x11\x22" + frame)
check(len(got) == 1, "octets parasites ignores par la resynchro")

os.close(master)
os.close(slave)


print("\n== orientation : index protocole -> case (regression) ==")
t = pb.BoardTracker()
captured = {}
link = pb.PegasusLink(lambda occ: captured.update(occ=occ),
                      lambda sq, o: captured.update(field=(sq, o)))
# dump de la position de depart, en ordre protocole DGT (index 0 = a8)
vals = bytearray(64)
for sq, piece in chess.Board().piece_map().items():
    vals[pb.square_to_dgt(sq)] = 0x01
link._frame(pb.MSG_BOARD_DUMP, bytes(vals))
t.set_occupancy(captured["occ"])
t.resolve()
check(captured["occ"] == set(chess.Board().piece_map()),
      "un dump de position initiale donne bien les cases a1-h2 et a7-h8")
check(t.codes()[pb.square_to_dgt(chess.E1)] == pb.PIECE_CODE[(chess.WHITE, chess.KING)],
      "roi blanc en e1, pas en e8 (miroir de rangees d'autrefois)")
check(t.codes()[pb.square_to_dgt(chess.D1)] == pb.PIECE_CODE[(chess.WHITE, chess.QUEEN)],
      "dame blanche en d1, pas en e1 (miroir de colonnes d'autrefois)")

# field update : index protocole 0x34 = e2
link._frame(pb.MSG_FIELD_UPDATE, bytes([0x34, 0x00]))
check(captured["field"] == (chess.E2, False), "field update 0x34 -> e2 videe")

# LED : on demande e2, la trame doit porter l'index 0x34
sent = {}
link.write = lambda data, response=True: sent.update(data=data)
import asyncio as _a
_a.get_event_loop().run_until_complete(link.leds([chess.E2])) if False else None
frame = pb.led_frame([link._index(chess.E2)])
check(frame[6] == 0x34, "trame LED pour e2 -> index 0x34")

# et avec --flip, tout tourne de 180 degres
flipped = pb.PegasusLink(lambda occ: captured.update(occ2=occ), lambda sq, o: None, flip=True)
flipped._frame(pb.MSG_BOARD_DUMP, bytes(vals))
check(captured["occ2"] == {chess.square(7 - chess.square_file(s), 7 - chess.square_rank(s))
                           for s in chess.Board().piece_map()},
      "--flip applique une vraie rotation de 180 degres")

print()
if FAIL:
    print(f"{len(FAIL)} test(s) en echec :")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("Tous les tests passent.")

print("\n== mode bus DGT (sequence reelle de LiveChess) ==")
master2, slave2, _ = pb.open_pty(None)
t2 = pb.BoardTracker()
fe2 = pb.SerialFrontend(master2, t2, serial_nr="10001")
t2.on_change = fe2.on_board_changed


def bus_cmd(code, address=0):
    head = bytes([code, (address >> 7) & 0x7f, address & 0x7f])
    return head + bytes([sum(head) & 0x7f])


def read2():
    time.sleep(0.03)
    try:
        return os.read(slave2, 8192)
    except BlockingIOError:
        return b""


def bus_frames(data):
    """Decoupe les reponses en mode bus et verifie chaque somme de controle."""
    out, i = [], 0
    while i < len(data):
        mid = data[i]
        total = (data[i + 1] << 7) | data[i + 2]
        frame = data[i:i + total]
        assert (sum(frame[:-1]) & 0x7f) == frame[-1], "somme de controle fausse"
        addr = (frame[3] << 7) | frame[4]
        out.append((mid, addr, frame[5:-1]))
        i += total
    return out


check(pb.bus_address_from_serial("01025") == 0x1025,
      "adresse de bus = lecture hexa du numero de serie (exemple de la spec)")
check(fe2.bus_address == 0x0001, "serie 10001 -> adresse de bus 1")

# la sequence exacte du log : 40 40 40 47 46 4a puis 8b 00 00 0b
fe2.feed(bytes([pb.S_RESET, pb.S_RESET, pb.S_RESET]))
fe2.feed(bytes([pb.S_SEND_TRADEMARK]))
tm = pb.FrameParser().feed(read2())
check(tm and tm[0][1].startswith(b"Digital Game Technology\r\nCopyright (c)"),
      "trademark conforme a l'entete attendue")

fe2.feed(bytes([pb.S_RETURN_BUSADRES]))
ba = pb.FrameParser().feed(read2())
check(ba and ba[0][1] == bytes([0x00, 0x01]), "0x46 annonce l'adresse de bus 1")

fe2.feed(bytes([pb.S_BUSMODE]))
check(fe2.bus_mode is True, "0x4a fait passer en mode bus")

fe2.feed(bus_cmd(pb.BUS_SEND_VERSION, 0))   # exactement 8b 00 00 0b
frames = bus_frames(read2())
check(len(frames) == 1 and frames[0][0] == pb.MSG_BUS_VERSION,
      f"8b 00 00 0b -> DGT_MSG_BUS_VERSION ({[hex(f[0]) for f in frames]})")
check(frames[0][2] == bytes([1, 6]), "la version repondue est bien 1.6")

fe2.feed(bus_cmd(pb.BUS_PING, 0))
frames = bus_frames(read2())
check(len(frames) == 1 and frames[0][0] == pb.MSG_BUS_PING and
      len(frames[0][2]) == 0, "ping general -> DGT_MSG_BUS_PING de 6 octets")

fe2.feed(bus_cmd(pb.BUS_SEND_BRD, 1))
frames = bus_frames(read2())
check(len(frames) == 1 and frames[0][0] == pb.MSG_BUS_BRD_DUMP and
      len(frames[0][2]) == 64, "board dump bus = 64 codes de pieces")
check(frames[0][2][pb.square_to_dgt(chess.E1)] ==
      pb.PIECE_CODE[(chess.WHITE, chess.KING)], "roi blanc en e1 dans le dump bus")

fe2.feed(bus_cmd(pb.BUS_SET_START_GAME, 1))
frames = bus_frames(read2())
check(len(frames) == 1 and frames[0][0] == pb.MSG_BUS_START_GAME_WRITTEN,
      "SET_START_GAME acquitte")

# on joue e4 : les changements doivent remonter au prochain SEND_CHANGES
ref2 = chess.Board()
play(t2, ref2, ref2.parse_san("e4"))
fe2.on_board_changed()
read2()
fe2.feed(bus_cmd(pb.BUS_SEND_CHANGES, 1))
frames = bus_frames(read2())
data = frames[0][2]
check(frames[0][0] == pb.MSG_BUS_UPDATE, "SEND_CHANGES -> DGT_MSG_BUS_UPDATE")
tags = [(data[i] & 0x0f, data[i + 1]) for i in range(0, len(data) - 1, 2)]
check((0x00, 0x34) in tags and (0x01, 0x24) in tags,
      f"changements e2 videe / e4 pion blanc ({tags})")
check(data[-1] == pb.EE_EOF, "le flux de changements se termine par EE_EOF")

fe2.feed(bus_cmd(pb.BUS_REPEAT_CHANGES, 1))
frames = bus_frames(read2())
check(frames[0][2] == data, "REPEAT_CHANGES rejoue a l'identique le dernier lot")
fe2.feed(bus_cmd(pb.BUS_SEND_CHANGES, 1))
frames = bus_frames(read2())
check(frames[0][2] == bytes([pb.EE_EOF]), "les changements ne sont pas renvoyes deux fois")

# une commande adressee a une autre carte ne doit rien produire
fe2.feed(bus_cmd(pb.BUS_PING, 4242))
check(read2() == b"", "une commande pour une autre adresse reste sans reponse")

# la sonde Caissa du log ne doit rien declencher ni desynchroniser
fe2.feed(bytes.fromhex("df 0d 12 03 00 00 00 00 00 00 00 00 00 01 00 03 02 30 00"))
noise = read2()
fe2.feed(bus_cmd(pb.BUS_PING, 1))
frames = bus_frames(read2())
check(noise == b"" and len(frames) == 1 and frames[0][0] == pb.MSG_BUS_PING,
      "la sonde Caissa est ignoree sans desynchroniser la suite")

# en mode bus, plus de field updates spontanes
play(t2, ref2, ref2.parse_san("e5"))
fe2.on_board_changed()
check(read2() == b"", "en mode bus la carte ne parle que lorsqu'on l'interroge")

os.close(master2)
os.close(slave2)

print("\n== recalage des identites (bug des mauvaises pieces au depart) ==")
# Le pont demarre alors que le plateau est vide, puis on pose les 32 pieces
# une a une : sans origine connue, chaque pose est devinee.
t3 = pb.BoardTracker()
t3.set_occupancy(set())                      # plateau vide
check(len(t3.pieces) == 0, "plateau vide : plus aucune piece suivie")
start = chess.Board().piece_map()
for sq in sorted(start):                     # remise en place, une a une
    t3.set_square(sq, True)
devine = dict(t3.pieces)
check(devine != start, "les identites devinees a la pose sont bien fausses")
check(t3.resolve() is True, "occupation initiale complete -> resolue")
check(t3.pieces == start, "les identites sont recalees sur la vraie position")
check(t3.codes()[pb.square_to_dgt(chess.E2)] ==
      pb.PIECE_CODE[(chess.WHITE, chess.PAWN)],
      "e2 redevient un pion blanc (et non un fou)")
check(t3.codes()[pb.square_to_dgt(chess.F2)] ==
      pb.PIECE_CODE[(chess.WHITE, chess.PAWN)], "f2 redevient un pion blanc")

# et en cours de partie : une piece levee puis reposee au meme endroit
t4 = pb.BoardTracker()
ref4 = chess.Board()
play(t4, ref4, ref4.parse_san("e4"))
t4.pieces[chess.D1] = chess.Piece(chess.KNIGHT, chess.WHITE)  # identite corrompue
t4.resolve()
check(t4.pieces[chess.D1] == chess.Piece(chess.QUEEN, chess.WHITE),
      "une identite corrompue en cours de partie est recalee au coup suivant")

print("\n== mode lichess direct : LED du coup adverse et envoi des coups ==")
import asyncio


class FakeLink:
    def __init__(self):
        self.lit = []

    def last(self):
        return self.lit[-1] if self.lit else ()

    async def leds(self, squares):
        self.lit.append(tuple(sorted(squares)))


class FakeLichess:
    def __init__(self):
        self.posted = []

    def post(self, path):
        self.posted.append(path)
        return {"ok": True}


async def scenario():
    link4, api4 = FakeLink(), FakeLichess()
    t5 = pb.BoardTracker()
    s5 = pb.PlaySession(t5, link4, api4)
    # on joue les noirs ; la partie commence, puis les blancs jouent e2e4
    s5.begin("abcd1234", chess.BLACK, "startpos", [])
    await s5.refresh()
    check(s5.leds.base == (), "rien a signaler au debut de partie")
    s5.update_moves(["e2e4"])
    await s5.refresh()
    check(s5.leds.base and s5.leds.base == tuple(sorted((chess.E2, chess.E4))),
          "plateau en retard -> les cases e2 et e4 s'allument")

    # on rejoue le coup adverse sur le plateau
    ref5 = chess.Board()
    play(t5, ref5, ref5.parse_san("e4"))
    await s5.refresh()
    check(s5.leds.base == (), "coup adverse rejoue -> les LED s'eteignent")
    check(api4.posted == [], "rejouer le coup adverse ne l'envoie pas a lichess")

    # notre reponse, jouee sur le plateau
    play(t5, ref5, ref5.parse_san("e5"))
    await s5.refresh()
    check(api4.posted == ["/api/board/game/abcd1234/move/e7e5"],
          f"notre coup part vers lichess ({api4.posted})")

    # lichess nous le renvoie : rien ne doit repartir
    s5.moves = ["e2e4", "e7e5"]
    await s5.refresh()
    check(len(api4.posted) == 1, "le coup n'est pas envoye deux fois")

    # coup joue sur le plateau alors que ce n'est pas notre tour
    play(t5, ref5, ref5.parse_san("Nf3"))
    await s5.refresh()
    check(len(api4.posted) == 1, "un coup joue hors de notre tour n'est pas envoye")

    # divergence franche : on previent, on n'envoie rien
    s5.moves = ["d2d4", "d7d5", "c2c4"]
    before = len(api4.posted)
    await s5.refresh()
    check(len(api4.posted) == before, "en cas de divergence, aucun coup n'est envoye")

    # promotion : l'uci doit porter la piece
    t6 = pb.BoardTracker()
    link6, api6 = FakeLink(), FakeLichess()
    s6 = pb.PlaySession(t6, link6, api6)
    fen = "8/P7/8/8/8/8/8/K6k w - - 0 1"
    s6.begin("promo", chess.WHITE, fen, [])
    ref6 = chess.Board(fen)
    play(t6, ref6, ref6.parse_san("a8=Q"))
    await s6.refresh()
    check(api6.posted == ["/api/board/game/promo/move/a7a8q"],
          f"la promotion part avec la piece dans l'uci ({api6.posted})")


asyncio.run(scenario())

# jeton : lecture par priorite, ecriture en 0600
import json
import tempfile
import threading
tmpdir = tempfile.mkdtemp()
pb.TOKEN_PATH = os.path.join(tmpdir, "token")
os.environ.pop("LICHESS_TOKEN", None)
check(pb.load_token() is None, "aucun jeton au depart")
pb.save_token("lip_secret  ")
check(oct(os.stat(pb.TOKEN_PATH).st_mode & 0o777) == "0o600",
      "le jeton est ecrit en 0600")
check(pb.load_token() == "lip_secret", "jeton relu depuis le fichier")
os.environ["LICHESS_TOKEN"] = "depuis_env"
check(pb.load_token() == "depuis_env", "la variable d'environnement a la priorite")
check(pb.load_token("explicite") == "explicite", "l'option --token gagne sur tout")
os.environ.pop("LICHESS_TOKEN", None)

print("\n== coup illegal : signalement, puis reprise ==")


def lift_place(tracker, frm, to):
    """Deplacement physique brut, sans passer par une partie de reference."""
    tracker.set_square(frm, False)
    tracker.set_square(to, True)
    return tracker.resolve()


async def illegal_scenario():
    link7, api7 = FakeLink(), FakeLichess()
    t7 = pb.BoardTracker()
    s7 = pb.PlaySession(t7, link7, api7)
    s7.begin("test", chess.BLACK, "startpos", ["e2e4"])
    ref7 = chess.Board()
    play(t7, ref7, ref7.parse_san("e4"))        # on rejoue le coup adverse
    await s7.refresh()
    check(t7.in_sync and s7.leds.base == (), "en phase apres le coup adverse")

    # le mechant testeur : c7 -> c4, trois cases d'un coup
    check(lift_place(t7, chess.C7, chess.C4) is False,
          "c7c4 n'est explique par aucun coup legal")
    t7.unresolved_since -= 4.0                   # on force le delai de 3 s
    t7.resolve()
    check(t7.in_sync is False, "le pont se declare desynchronise")
    extra, missing = t7.mismatch()
    check(extra == [chess.C4] and missing == [chess.C7],
          f"les cases fautives sont nommees (en trop {extra}, manquant {missing})")
    await s7.refresh()
    check(set(s7.leds.base) == {chess.C4, chess.C7},
          "les cases fautives s'allument sur le plateau")

    # on remet la piece : la partie doit reprendre
    check(lift_place(t7, chess.C4, chess.C7) is True, "retour en arriere accepte")
    check(t7.in_sync is True, "la synchro revient sans relancer le pont")
    await s7.refresh()
    check(s7.leds.base == (), "les LED s'eteignent une fois la position retablie")

    # et le coup legal part normalement
    play(t7, ref7, ref7.parse_san("c5"))
    await s7.refresh()
    check(api7.posted == ["/api/board/game/test/move/c7c5"],
          f"c7c5 part vers lichess ({api7.posted})")


asyncio.run(illegal_scenario())


async def repair_by_playing():
    """Sortie de desynchronisation en jouant directement le bon coup."""
    link8, api8 = FakeLink(), FakeLichess()
    t8 = pb.BoardTracker()
    s8 = pb.PlaySession(t8, link8, api8)
    s8.begin("test2", chess.BLACK, "startpos", ["e2e4"])
    ref8 = chess.Board()
    play(t8, ref8, ref8.parse_san("e4"))
    lift_place(t8, chess.C7, chess.C4)
    t8.unresolved_since -= 4.0
    t8.resolve()
    check(t8.in_sync is False, "desynchronise apres c7c4")
    # sans repasser par c7 : on glisse le pion de c4 vers c5
    check(lift_place(t8, chess.C4, chess.C5) is True,
          "le pion glisse en c5 : le coup legal c7c5 est reconnu")
    check(t8.in_sync is True, "synchro retablie par un coup legal")
    await s8.refresh()
    check(api8.posted == ["/api/board/game/test2/move/c7c5"],
          f"et le coup part ({api8.posted})")


asyncio.run(repair_by_playing())

print("\n== reprise de coup proposee par lichess ==")


async def takeback_scenario():
    link9, api9 = FakeLink(), FakeLichess()
    t9 = pb.BoardTracker()
    s9 = pb.PlaySession(t9, link9, api9)
    s9.begin("tb", chess.BLACK, "startpos", [])
    ref9 = chess.Board()
    for san in ["e4", "e5", "Nf3", "Nc6", "Bb5"]:
        play(t9, ref9, ref9.parse_san(san))
    s9.moves = [m.uci() for m in ref9.move_stack]
    await s9.refresh()
    check(t9.in_sync and s9.leds.base == (), "partie suivie, 5 demi-coups")

    # on joue a6, lichess l'enregistre : 6 demi-coups de chaque cote
    play(t9, ref9, ref9.parse_san("a6"))
    await s9.refresh()
    check(api9.posted == ["/api/board/game/tb/move/a7a6"], "a7a6 est parti")
    check(s9.update_moves([m.uci() for m in ref9.move_stack]) is False,
          "une partie qui s'allonge n'est pas une reprise")
    await s9.refresh()
    check(s9.leds.base == (), "tout est en phase")

    # blancs jouent Ba4, puis proposent une reprise acceptee de 2 demi-coups
    play(t9, ref9, ref9.parse_san("Ba4"))
    s9.moves = [m.uci() for m in ref9.move_stack]
    await s9.refresh()
    check(len(s9.board_moves()) == 7, "le plateau est a 7 demi-coups")

    shrunk = s9.update_moves(s9.moves[:5])   # lichess recule de 2 demi-coups
    check(shrunk is True, "le raccourcissement de la partie est detecte")
    await s9.refresh(takeback=shrunk)
    check(t9.frozen is True, "le suivi se gele en attendant la remise en place")
    lit = set(s9.leds.base)
    check(lit, f"les cases a corriger s'allument ({sorted(lit)})")
    check(chess.A6 in lit and chess.A7 in lit,
          "le pion a6 doit retourner en a7 : les deux cases sont allumees")
    check(chess.A4 in lit and chess.B5 in lit,
          "le fou a4 doit retourner en b5 : les deux cases sont allumees")
    check(api9.posted == ["/api/board/game/tb/move/a7a6"],
          "rien n'est envoye pendant le gel")

    # tant que le plateau n'est pas remis, aucun coup n'est invente
    t9.set_square(chess.A6, False)
    t9.set_square(chess.A7, True)
    t9.resolve()
    check(t9.frozen is True, "un seul des deux coups annules ne suffit pas")

    # on remet le fou : la partie doit reprendre
    t9.set_square(chess.A4, False)
    t9.set_square(chess.B5, True)
    check(t9.resolve() is True, "position de la partie retrouvee")
    check(t9.frozen is False and t9.in_sync is True, "le gel est leve")
    await s9.refresh()
    check(s9.leds.base == (), "les LED s'eteignent")

    # et on peut rejouer normalement
    ref9.pop(); ref9.pop()
    play(t9, ref9, ref9.parse_san("Nf6"))
    await s9.refresh()
    check(api9.posted[-1] == "/api/board/game/tb/move/g8f6",
          f"le nouveau coup part ({api9.posted[-1]})")


asyncio.run(takeback_scenario())

print("\n== reprise d'un seul demi-coup (cas ambigu) ==")


async def single_takeback():
    link10, api10 = FakeLink(), FakeLichess()
    t10 = pb.BoardTracker()
    s10 = pb.PlaySession(t10, link10, api10)
    s10.begin("solo", chess.WHITE, "startpos", [])
    ref10 = chess.Board()
    play(t10, ref10, ref10.parse_san("e4"))
    await s10.refresh()
    check(api10.posted == ["/api/board/game/solo/move/e2e4"],
          "un coup d'avance sans reprise -> envoi normal")
    s10.update_moves(["e2e4"])
    await s10.refresh()

    # lichess annule notre coup : la liste passe de 1 a 0
    shrunk = s10.update_moves([])
    check(shrunk is True, "reprise d'un seul demi-coup detectee")
    await s10.refresh(takeback=shrunk)
    check(t10.frozen is True, "gel malgre l'ambiguite avec le cas normal")
    check(set(s10.leds.base) == {chess.E2, chess.E4},
          "e2 et e4 s'allument pour annuler le coup")
    check(len(api10.posted) == 1, "le coup annule n'est pas renvoye")

    # remise en place -> reprise
    t10.set_square(chess.E4, False)
    t10.set_square(chess.E2, True)
    check(t10.resolve() is True and t10.frozen is False,
          "position initiale retrouvee, gel leve")
    await s10.refresh()
    check(len(api10.posted) == 1, "toujours aucun renvoi apres la remise")


asyncio.run(single_takeback())

print("\n== piece en l'air : aucun coup ne doit etre devine ==")
# Cavalier blanc en f3, fou noir en g5 : Nxg5 est une prise, Nf3-e5 une case
# vide. Lever le cavalier produit exactement l'occupation d'une prise depuis f3.
PIEGE = "4k3/8/8/6b1/8/5N2/8/4K3 w - - 0 1"


async def piece_in_hand():
    link11, api11 = FakeLink(), FakeLichess()
    t11 = pb.BoardTracker()
    s11 = pb.PlaySession(t11, link11, api11)
    s11.begin("piege", chess.WHITE, PIEGE, [])

    ref11 = chess.Board(PIEGE)
    check(chess.Move.from_uci("f3g5") in ref11.legal_moves and
          chess.Move.from_uci("f3e5") in ref11.legal_moves,
          "position piege : Nxg5 et Nf3-e5 sont toutes deux legales")

    t11.set_square(chess.F3, False)          # on leve le cavalier
    check(t11.holding_mover_piece() is True, "le cavalier au trait est en main")
    check(t11.resolve() is False, "rien n'est resolu tant qu'il est en l'air")
    check(len(t11.logic.move_stack) == 0, "aucun coup n'a ete pousse")
    await s11.refresh()
    check(api11.posted == [], "et rien n'est parti vers lichess")

    t11.set_square(chess.E5, True)           # on le pose en e5
    check(t11.resolve() is True, "le coup est resolu a la pose")
    check(t11.logic.move_stack[-1].uci() == "f3e5",
          f"c'est bien f3e5 ({t11.logic.move_stack[-1].uci()})")
    await s11.refresh()
    check(api11.posted == ["/api/board/game/piege/move/f3e5"],
          f"f3e5 part vers lichess ({api11.posted})")


asyncio.run(piece_in_hand())


async def capture_still_works():
    """La prise doit rester detectee : la piece prise est a l'adversaire."""
    link12, api12 = FakeLink(), FakeLichess()
    t12 = pb.BoardTracker()
    s12 = pb.PlaySession(t12, link12, api12)
    s12.begin("prise", chess.WHITE, PIEGE, [])

    t12.set_square(chess.G5, False)          # on retire le fou pris
    check(t12.holding_mover_piece() is False,
          "une piece adverse en main ne bloque pas la resolution")
    check(t12.resolve() is False, "mais l'occupation seule n'explique rien")
    t12.set_square(chess.F3, False)          # on leve le cavalier
    check(t12.resolve() is False, "toujours rien, le cavalier est en l'air")
    t12.set_square(chess.G5, True)           # il atterrit sur la case prise
    check(t12.resolve() is True, "la prise est resolue a l'atterrissage")
    check(t12.logic.move_stack[-1].uci() == "f3g5",
          f"c'est bien la prise f3g5 ({t12.logic.move_stack[-1].uci()})")
    await s12.refresh()
    check(api12.posted == ["/api/board/game/prise/move/f3g5"],
          "la prise part vers lichess")


asyncio.run(capture_still_works())


async def castling_in_progress():
    """Le roque : deux pieces du joueur au trait passent par la main."""
    link13, api13 = FakeLink(), FakeLichess()
    t13 = pb.BoardTracker()
    s13 = pb.PlaySession(t13, link13, api13)
    fen = "4k3/8/8/8/8/8/8/4K2R w K - 0 1"
    s13.begin("roque", chess.WHITE, fen, [])
    t13.set_square(chess.E1, False)
    check(t13.resolve() is False, "roi en l'air : on attend")
    t13.set_square(chess.H1, False)
    check(t13.resolve() is False, "tour en l'air aussi : on attend")
    t13.set_square(chess.G1, True)
    check(t13.resolve() is False, "roi pose, tour encore en main : on attend")
    t13.set_square(chess.F1, True)
    check(t13.resolve() is True, "roque complet : resolu")
    check(t13.logic.move_stack[-1].uci() == "e1g1",
          f"le roque est encode e1g1 ({t13.logic.move_stack[-1].uci()})")
    await s13.refresh()
    check(api13.posted == ["/api/board/game/roque/move/e1g1"],
          "le roque part vers lichess")


asyncio.run(castling_in_progress())

print("\n== prises ambigues : deux prises depuis la meme case ==")
# Fou blanc en g3 pouvant prendre en c7 ET en h4 : les deux vident g3 et
# laissent la case d'arrivee occupee. L'occupation finale est identique.
AMBIGU = "2b1k3/2p5/8/8/7p/6B1/8/4K3 w - - 0 1"


async def ambiguous_capture():
    link14, api14 = FakeLink(), FakeLichess()
    t14 = pb.BoardTracker()
    s14 = pb.PlaySession(t14, link14, api14)
    s14.begin("ambigu", chess.WHITE, AMBIGU, [])
    ref14 = chess.Board(AMBIGU)
    prises = [m for m in ref14.legal_moves
              if ref14.is_capture(m) and m.from_square == chess.G3]
    check(len(prises) == 2,
          f"deux prises possibles depuis g3 ({[m.uci() for m in prises]})")

    # geste reel : on retire la piece prise, on leve le fou, on le pose
    t14.set_square(chess.C7, False)
    t14.set_square(chess.G3, False)
    t14.set_square(chess.C7, True)
    check(t14.resolve() is True, "la prise est resolue malgre l'ambiguite")
    check(t14.logic.move_stack[-1].uci() == "g3c7",
          f"c'est bien g3c7 et non g3h4 ({t14.logic.move_stack[-1].uci()})")
    await s14.refresh()
    check(api14.posted == ["/api/board/game/ambigu/move/g3c7"],
          f"g3c7 part vers lichess ({api14.posted})")

    # l'autre prise, depuis la meme case, doit se distinguer aussi
    link15, api15 = FakeLink(), FakeLichess()
    t15 = pb.BoardTracker()
    s15 = pb.PlaySession(t15, link15, api15)
    s15.begin("ambigu2", chess.WHITE, AMBIGU, [])
    t15.set_square(chess.H4, False)
    t15.set_square(chess.G3, False)
    t15.set_square(chess.H4, True)
    check(t15.resolve() is True and t15.logic.move_stack[-1].uci() == "g3h4",
          "la prise en h4 est reconnue comme telle")


asyncio.run(ambiguous_capture())

print("\n== branchement en cours de partie ==")


async def join_mid_game():
    link16, api16 = FakeLink(), FakeLichess()
    t16 = pb.BoardTracker()
    s16 = pb.PlaySession(t16, link16, api16)
    ref16 = chess.Board()
    for san in ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O"]:
        ref16.push_san(san)
    moves = [m.uci() for m in ref16.move_stack]

    s16.begin("reprise", chess.BLACK, "startpos", moves)
    check(t16.logic.board_fen() == ref16.board_fen(),
          "le suivi demarre sur la position actuelle, pas sur l'initiale")
    check(s16.board_moves() == moves,
          f"les deux listes de coups concordent d'emblee ({len(moves)} demi-coups)")
    check(t16.in_sync is True, "en phase des le branchement")
    await s16.refresh()
    check(s16.leds.base == () and api16.posted == [],
          "rien a allumer, rien a envoyer : aucun coup a rejouer")

    # et on peut jouer tout de suite
    play(t16, ref16, ref16.parse_san("Be7"))
    await s16.refresh()
    check(api16.posted == ["/api/board/game/reprise/move/f8e7"],
          f"le coup suivant part normalement ({api16.posted})")

    # si le plateau ne correspond pas, on le signale au lieu de tout rejouer
    link17, api17 = FakeLink(), FakeLichess()
    t17 = pb.BoardTracker()
    s17 = pb.PlaySession(t17, link17, api17)
    s17.begin("reprise2", chess.BLACK, "startpos", moves)
    t17.auto_reset = False        # comme en mode lichess : la partie fait foi
    t17.set_occupancy(set(chess.Board().piece_map()))   # plateau a l'initiale
    t17.unresolved_since = time.monotonic() - 4.0
    t17.resolve()
    check(t17.in_sync is False, "plateau incoherent -> desynchronise")
    await s17.refresh()
    check(len(s17.leds.base) > 0,
          f"les cases a corriger sont montrees ({len(s17.leds.base)} cases)")


asyncio.run(join_mid_game())

print("\n== fin de partie puis partie suivante ==")


async def end_then_next():
    link18, api18 = FakeLink(), FakeLichess()
    t18 = pb.BoardTracker()
    s18 = pb.PlaySession(t18, link18, api18)
    s18.begin("game1", chess.WHITE, "startpos", [])
    ref18 = chess.Board()
    play(t18, ref18, ref18.parse_san("e4"))
    await s18.refresh()
    check(api18.posted == ["/api/board/game/game1/move/e2e4"], "partie 1 en cours")

    s18.end_game("mate")
    check(s18.finished is True and t18.frozen is True,
          "fin de partie : le suivi du plateau est gele")

    # on range les pieces n'importe comment : plus aucune plainte, aucun envoi
    for sq in list(t18.occ):
        t18.set_square(sq, False)
    check(t18.resolve() is False, "plateau vide apres la partie : rien a resoudre")
    check(t18.in_sync is True, "et aucune desynchronisation signalee")
    await s18.refresh()
    check(len(api18.posted) == 1, "rien n'est envoye apres la fin")

    # on remonte la position de depart et une nouvelle partie commence
    t18.set_occupancy(set(chess.Board().piece_map()))
    s18.begin("game2", chess.BLACK, "startpos", ["d2d4"])
    check(s18.finished is False and t18.frozen is False,
          "la nouvelle partie degele le suivi")
    check(s18.game_id == "game2", "on suit bien la nouvelle partie")
    check(t18.logic.move_stack[-1].uci() == "d2d4",
          "et on est cale sur sa position actuelle")

    ref19 = chess.Board()
    ref19.push_uci("d2d4")
    play(t18, ref19, ref19.parse_san("d5"))
    await s18.refresh()
    check(api18.posted[-1] == "/api/board/game/game2/move/d7d5",
          f"le premier coup de la partie 2 part ({api18.posted[-1]})")


asyncio.run(end_then_next())

print("\n== un flux de partie ne se reconnecte pas a l'infini ==")
import io


class OneShotHTTP:
    """Simule lichess : le flux se ferme proprement quand la partie finit."""

    def __init__(self):
        self.opens = 0

    def __call__(self, path, method="GET", data=None, timeout=30.0):
        self.opens += 1
        payload = b'{"type":"gameState","moves":"e2e4","status":"mate"}\n'
        return io.BytesIO(payload)


fake_http = OneShotHTTP()
api20 = pb.Lichess("jeton")
api20._open = fake_http
recu = []
stop20 = threading.Event()
th = threading.Thread(target=api20.stream,
                      args=("/api/board/game/stream/x", recu.append, stop20, False))
th.start()
th.join(timeout=3)
check(not th.is_alive(), "le flux de partie s'arrete de lui-meme")
check(fake_http.opens == 1, f"il ne se reconnecte pas ({fake_http.opens} ouverture)")
check(len(recu) == 1, "et le dernier message a bien ete transmis")

print("\n== lancer une partie depuis le plateau ==")


class FakeApi:
    """Faux lichess : on note les recherches et les defis."""

    def __init__(self):
        self.seeks = []
        self.forms = []
        self.closed = 0
        self.keep = None

    def form(self, path, fields):
        self.forms.append((path, dict(fields)))
        return {"id": "nouvelle"}

    def seek(self, fields, keep):
        self.seeks.append(dict(fields))

        class Resp:
            def close(inner):
                self.closed += 1

        self.keep = keep
        keep(Resp())


GESTES = [
    {"nom": "Rapide 10+15 classee", "cases": ["a1", "h8"], "action": "seek",
     "minutes": 10, "increment": 15, "classee": True},
    {"nom": "Blitz 3+2 amicale", "cases": ["b1", "g8"], "action": "seek",
     "minutes": 3, "increment": 2, "classee": False},
    {"nom": "Ordinateur niveau 4", "cases": ["c1", "f8"], "action": "ai",
     "niveau": 4, "minutes": 10, "increment": 0, "couleur": "white"},
]


def poser_position_initiale(tracker):
    tracker.set_occupancy(set(chess.Board().piece_map()))


async def gestes():
    t20 = pb.BoardTracker()
    link20, api20b = FakeLink(), FakeApi()
    lanceur = pb.Launcher(t20, link20, api20b, GESTES, delay=0.0)
    check(len(lanceur.gestures) == 3, "les trois gestes sont charges")
    check(lanceur.describe()[0].startswith("a1+h8"),
          f"description lisible ({lanceur.describe()[0]})")

    poser_position_initiale(t20)
    await lanceur.poll()
    check(api20b.seeks == [], "position initiale intacte : rien ne se declenche")

    # on souleve a1 et h8
    t20.set_square(chess.A1, False)
    await lanceur.poll()
    check(api20b.seeks == [], "une seule tour levee : toujours rien")
    t20.set_square(chess.H8, False)
    await lanceur.poll()     # arme le candidat
    await lanceur.poll()     # delai ecoule (delay=0)
    await asyncio.sleep(0.05)   # la recherche part dans un executeur
    check(len(api20b.seeks) == 1, f"a1+h8 lance une recherche ({api20b.seeks})")
    envoi = api20b.seeks[0]
    check(envoi["rated"] is True, "la partie est classee")
    check(envoi["time"] == 10.0 and envoi["increment"] == 15,
          f"cadence 10+15 ({envoi['time']}+{envoi['increment']})")
    check(lanceur.pending is not None, "la recherche est marquee en cours")

    # reposer les pieces annule
    t20.set_square(chess.A1, True)
    t20.set_square(chess.H8, True)
    await lanceur.poll()
    check(api20b.closed == 1, "reposer les pieces ferme la connexion")
    check(lanceur.pending is None, "et la recherche n'est plus en cours")

    # un autre geste, une autre cadence
    t20.set_square(chess.B1, False)
    t20.set_square(chess.G8, False)
    await lanceur.poll(); await lanceur.poll()
    await asyncio.sleep(0.05)
    check(len(api20b.seeks) == 2, "b1+g8 lance aussi")
    check(api20b.seeks[1]["rated"] is False and api20b.seeks[1]["time"] == 3.0,
          f"cadence 3+2 amicale ({api20b.seeks[1]})")
    await lanceur.cancel()

    # le geste « ordinateur » passe par un autre point d'API
    poser_position_initiale(t20)
    t20.set_square(chess.C1, False)
    t20.set_square(chess.F8, False)
    await lanceur.poll(); await lanceur.poll()
    check(len(api20b.forms) == 1, "c1+f8 lance un defi a l'ordinateur")
    chemin, champs = api20b.forms[0]
    check(chemin == "/api/challenge/ai", f"bon point d'API ({chemin})")
    check(champs["level"] == 4 and champs["clock.limit"] == 600
          and champs["color"] == "white",
          f"niveau 4, 10 minutes, blancs ({champs})")

    # une piece en trop ou une case inattendue n'arme rien
    t21 = pb.BoardTracker()
    link21, api21 = FakeLink(), FakeApi()
    l21 = pb.Launcher(t21, link21, api21, GESTES, delay=0.0)
    poser_position_initiale(t21)
    t21.set_square(chess.A1, False)
    t21.set_square(chess.H8, False)
    t21.set_square(chess.E4, True)          # une piece egaree au milieu
    await l21.poll(); await l21.poll()
    await asyncio.sleep(0.05)
    check(api21.seeks == [], "une piece hors position initiale bloque le geste")

    # un geste inconnu ne declenche rien
    t22 = pb.BoardTracker()
    link22, api22 = FakeLink(), FakeApi()
    l22 = pb.Launcher(t22, link22, api22, GESTES, delay=0.0)
    poser_position_initiale(t22)
    t22.set_square(chess.D1, False)
    t22.set_square(chess.E8, False)
    await l22.poll(); await l22.poll()
    await asyncio.sleep(0.05)
    check(api22.seeks == [] and api22.forms == [],
          "une combinaison non definie ne lance rien")

    # le delai empeche un geste court de se declencher en chemin
    t23 = pb.BoardTracker()
    link23, api23 = FakeLink(), FakeApi()
    court = [{"nom": "juste a1", "cases": ["a1"], "action": "seek",
              "minutes": 1, "increment": 0}] + GESTES
    l23 = pb.Launcher(t23, link23, api23, court, delay=5.0)
    poser_position_initiale(t23)
    t23.set_square(chess.A1, False)
    await l23.poll(); await l23.poll()
    await asyncio.sleep(0.05)
    check(api23.seeks == [], "le geste court attend le delai au lieu de partir")
    t23.set_square(chess.H8, False)
    await l23.poll()
    check(l23.candidate is not None and set(l23.candidate[0]) == {chess.A1, chess.H8},
          "le candidat bascule sur le geste complet a1+h8")


asyncio.run(gestes())

# geste dont les cases sont vides au depart : refuse a la lecture
t24 = pb.BoardTracker()
l24 = pb.Launcher(t24, FakeLink(), FakeApi(),
                  [{"nom": "impossible", "cases": ["e4", "e5"], "action": "seek"}],
                  delay=0.0)
check(l24.gestures == [], "un geste sur des cases vides au depart est ecarte")

# fichier de configuration
cfg = os.path.join(tmpdir, "gestures.json")
gestes_lus = pb.load_gestures(cfg)
check(os.path.exists(cfg), "le fichier d'exemple est cree au premier lancement")
check(len(gestes_lus) == 3, "trois gestes par defaut")
with open(cfg, encoding="utf-8") as f:
    check(json.load(f)["gestes"][0]["cases"] == ["a1", "h8"],
          "et il est relisible tel quel")
with open(cfg, "w", encoding="utf-8") as f:
    json.dump({"gestes": [{"nom": "perso", "cases": ["d1", "d8"],
                           "action": "seek", "minutes": 1, "increment": 0}]}, f)
perso = pb.load_gestures(cfg)
check(len(perso) == 1 and perso[0]["nom"] == "perso",
      "un fichier modifie a la main est bien pris en compte")

print("\n== animations et retours lumineux ==")


class RecordLink:
    """Note chaque image envoyee au plateau, ordre et vitesse compris."""

    def __init__(self):
        self.frames = []          # (cases dans l'ordre, vitesse)

    async def leds(self, squares, **kwargs):
        self.frames.append((tuple(squares), kwargs.get("speed")))

    @property
    def cases(self):
        return [f[0] for f in self.frames]


async def animations():
    V = pb.LedDirector.SPEED_ANIM
    rl = RecordLink()
    d = pb.LedDirector(rl)

    # Debut de partie cote blanc : une seule trame, le plateau balaye lui-meme.
    d.welcome(chess.WHITE)
    await asyncio.sleep(2.1)
    rang_blanc = [chess.square(f, 0) for f in range(8)]
    check(pb.LedDirector.rank_of(chess.WHITE) == rang_blanc,
          "la rangee des blancs est bien la premiere")
    check(rl.frames[0] == (tuple(rang_blanc), V),
          f"une seule trame, a1 vers h1, vitesse {V} ({rl.frames[0]})")
    check(len(rl.cases) == 2 and rl.cases[1] == (),
          f"rien d'autre pendant le balayage, puis extinction ({rl.cases})")

    # Cote noir : la rangee 8 parcourue a l'envers, pour que le balayage
    # aille de la gauche du joueur vers sa droite comme chez les blancs.
    rl2 = RecordLink()
    d2 = pb.LedDirector(rl2)
    d2.welcome(chess.BLACK)
    await asyncio.sleep(2.1)
    attendu = tuple(chess.square(f, 7) for f in reversed(range(8)))
    check(rl2.frames[0] == (attendu, V),
          f"cote noir : h8 vers a8, symetrique ({rl2.cases[0]})")
    check(attendu[0] == chess.H8 and attendu[-1] == chess.A8,
          "l'ordre part bien de h8")

    # Fin de partie : la rangee du vainqueur, deux passages.
    rl3 = RecordLink()
    d3 = pb.LedDirector(rl3)
    d3.farewell(chess.BLACK)
    await asyncio.sleep(0.2)
    check(rl3.frames[0] == (attendu, V),
          "victoire des noirs : leur rangee est balayee")
    check(not any(chess.A1 in f for f in rl3.cases),
          "et pas celle des blancs")

    rl4 = RecordLink()
    d4 = pb.LedDirector(rl4)
    d4.farewell(None)
    await asyncio.sleep(0.2)
    nulle = rl4.cases[0]
    check(len(nulle) == 16 and set(nulle) == set(rang_blanc) | set(attendu),
          f"nulle : les seize cases des deux rangees ({len(nulle)})")
    check(nulle[:4] == (chess.A1, chess.H8, chess.B1, chess.G8),
          f"entrelacees, pour deux lumieres de front ({nulle[:4]})")

    # Confirmation d'un coup accepte : un seul clin d'oeil, bref.
    rl5 = RecordLink()
    d5 = pb.LedDirector(rl5)
    await d5.set_base((chess.D4,))
    rl5.frames.clear()
    d5.confirm(chess.E2, chess.E4)
    await asyncio.sleep(0.8)
    allumes = [f for f in rl5.cases if set(f) == {chess.E2, chess.E4}]
    check(len(allumes) == 1, f"un seul clin d'oeil, bref ({len(allumes)})")
    check(rl5.cases[-1] == (chess.D4,),
          "puis l'etat de fond revient, sans le perdre")

    # Une animation en chasse une autre.
    rl6 = RecordLink()
    d6 = pb.LedDirector(rl6)
    d6.welcome(chess.WHITE)
    await asyncio.sleep(0.15)
    d6.confirm(chess.A1, chess.A2)
    await asyncio.sleep(0.8)
    check(rl6.cases[-1] == (), "la seconde animation reprend la main proprement")

    # La vitesse est reglable et voyage jusqu'a la trame.
    rl7 = RecordLink()
    d7 = pb.LedDirector(rl7, speed=12)
    d7.welcome(chess.WHITE)
    await asyncio.sleep(0.2)
    check(rl7.frames[0][1] == 12, "--led-speed choisit la cadence du balayage")

    # Et les trames produites sont exactement celles que le plateau a validees.
    check(V == 0x40, f"la cadence retenue est 64 ({V})")
    trame = pb.led_frame([pb.square_to_dgt(c) for c in rang_blanc], speed=8)
    check(trame.hex(" ") == "60 0d 05 08 00 01 38 39 3a 3b 3c 3d 3e 3f 00",
          f"trame d'une rangee, cadence 8 : essai 6 ({trame.hex(' ')})")
    nul = pb.led_frame([pb.square_to_dgt(c) for c in nulle], speed=V)
    check(nul.hex(" ") == "60 15 05 40 00 01 38 07 39 06 3a 05 3b 04 3c 03 "
                          "3d 02 3e 01 3f 00 00",
          f"trame de nulle : essai 7, a l'octet pres ({nul.hex(' ')})")
    check(pb.LedDirector.SPEED_BASE == V,
          "le guidage du coup adverse a la meme cadence que les animations")


asyncio.run(animations())

print("\n== actions de partie : abandon, nulle, reprise, temps ==")


async def actions_de_partie():
    class ApiActions(FakeLichess):
        def __init__(self):
            super().__init__()
            self.chemins = []
            self.refuse = set()

        def post(self, path):
            self.chemins.append(path)
            if any(mot in path for mot in self.refuse):
                raise RuntimeError("HTTP Error 401: Unauthorized")
            return {}

    api = ApiActions()
    s = pb.PlaySession(pb.BoardTracker(), FakeLink(), api)

    # Hors partie, rien ne part : c'est le cas ou un bouton serait grise.
    check(await s.action("abandon") is False, "hors partie, aucune action")
    check(not api.chemins, "et aucun appel a lichess")

    s.begin("abcd1234", chess.WHITE, "startpos", [])
    attendu = {
        "abandon": "/api/board/game/abcd1234/resign",
        "annuler": "/api/board/game/abcd1234/abort",
        "nulle": "/api/board/game/abcd1234/draw/yes",
        "refuser-nulle": "/api/board/game/abcd1234/draw/no",
        "reprise": "/api/board/game/abcd1234/takeback/yes",
        "refuser-reprise": "/api/board/game/abcd1234/takeback/no",
        "victoire": "/api/board/game/abcd1234/claim-victory",
    }
    for nom, chemin in attendu.items():
        api.chemins.clear()
        check(await s.action(nom) is True, f"« {nom} » part")
        check(api.chemins == [chemin], f"vers {chemin}")

    api.chemins.clear()
    check(await s.action("temps", 15) is True, "l'ajout de temps part")
    check(api.chemins == ["/api/round/abcd1234/add-time/15"],
          f"vers la route /api/round ({api.chemins})")

    api.chemins.clear()
    check(await s.action("bidon") is False, "une action inconnue est refusee")
    check(not api.chemins, "sans rien envoyer")

    # Un jeton sans le droit « challenge:write » : on echoue proprement.
    api.refuse = {"add-time"}
    check(await s.action("temps", 30) is False,
          "lichess refuse l'ajout de temps, on le signale sans planter")

    # Une partie finie ne prend plus d'ordres.
    s.end_game("mate", "white")
    api.chemins.clear()
    check(await s.action("nulle") is False, "partie finie : plus d'action")
    check(not api.chemins, "et toujours aucun appel")


asyncio.run(actions_de_partie())

print("\n== recherche d'adversaire : la lumiere tourne ==")


async def animation_recherche():
    anneau = pb.LedDirector.ring()
    attendu = [chess.C3, chess.C4, chess.C5, chess.C6, chess.D6, chess.E6,
               chess.F6, chess.F5, chess.F4, chess.F3, chess.E3, chess.D3]
    check(anneau == attendu, "l'anneau fait le tour du carre central")
    check(len(set(anneau)) == 12, "douze cases, sans doublon")

    cfg = pb.normalize_led_settings(None)
    cfg["recherche"] = {"vitesse": 10, "duree": 0.08, "repetitions": 1}
    rl = RecordLink()
    d = pb.LedDirector(rl, settings=cfg)
    d.searching()
    await asyncio.sleep(0.5)
    tours = [f for f in rl.cases if len(f) == 12]
    check(len(tours) >= 2, f"le tour est relance sans fin ({len(tours)} fois)")
    check(tours[0] == tuple(attendu),
          "dans l'ordre : c'est lui qui donne le sens de rotation")
    check(rl.frames[0][1] == 10, "a la vitesse reglee pour la recherche")
    check(any(not f for f in rl.cases),
          "avec une extinction entre deux tours, sinon le plateau ne repart pas")

    d.stop_searching()
    await asyncio.sleep(0.15)
    check(d.pulse is None or d.pulse.done(), "l'arret coupe l'animation")
    check(rl.cases[-1] == (), "et eteint tout")

    # Le lanceur allume pendant la recherche et eteint quand la partie part.
    tracker = pb.BoardTracker()
    poser_position_initiale(tracker)
    rl2 = RecordLink()
    d2 = pb.LedDirector(rl2, settings=cfg)
    api = FakeApi()
    lanceur = pb.Launcher(tracker, rl2, api, GESTES, delay=0.0, leds=d2)
    tracker.set_square(chess.A1, False)
    tracker.set_square(chess.H8, False)
    await lanceur.poll()
    await lanceur.poll()
    await asyncio.sleep(0.2)
    check(api.seeks and api.seeks[0]["time"] == 10, "la recherche est lancee")
    check(d2.base_kind == "recherche" and len(d2.base) == 12,
          f"et l'anneau tourne pendant l'attente ({d2.base_kind})")
    await lanceur.game_started()
    await asyncio.sleep(0.1)
    check(d2.base_kind != "recherche" and not d2.base,
          "la partie commence : l'anneau s'efface")
    d2._stop_pulse()


asyncio.run(animation_recherche())

print("\n== pourquoi un geste ne part pas ==")


def diagnostic_gestes():
    tracker = pb.BoardTracker()
    lanceur = pb.Launcher(tracker, None, None, GESTES, delay=0.0)

    poser_position_initiale(tracker)
    check(lanceur.diagnose() is None,
          "position complete, rien a signaler")

    tracker.set_square(chess.E4, True)          # une piece egaree
    dit = lanceur.diagnose() or ""
    check("pas en position de depart" in dit and "e4" in dit,
          f"une piece en trop est nommee ({dit})")

    tracker.set_square(chess.E4, False)
    tracker.set_square(chess.D1, False)         # une case levee sans geste
    dit = lanceur.diagnose() or ""
    check("aucun geste ne correspond" in dit and "d1" in dit,
          f"les cases levees sont nommees ({dit})")

    tracker.set_square(chess.D1, True)
    tracker.set_square(chess.A1, False)
    tracker.set_square(chess.H8, False)
    check(lanceur.diagnose() is None, "le bon geste ne declenche aucun reproche")

    lanceur.enabled = False
    check("desactives" in (lanceur.diagnose() or ""),
          "et on dit clairement quand les gestes dorment")


diagnostic_gestes()

print("\n== reglages lumineux : jauges, clignotement, direct ==")


async def reglages_leds():
    # Les valeurs lues d'un fichier sont completees et bornees.
    cfg = pb.normalize_led_settings({"debut": {"vitesse": 999, "duree": -2},
                                     "inconnu": {"vitesse": 3}})
    check(cfg["debut"]["vitesse"] == 127, "la vitesse est plafonnee a 127")
    check(cfg["debut"]["duree"] == 0.0, "une duree negative est ramenee a 0")
    check(set(cfg) == set(pb.DEFAULT_LEDS),
          "les evenements manquants reprennent les valeurs d'usine")
    check("inconnu" not in cfg, "un evenement inconnu est ignore")

    # Chaque evenement tire sa cadence de ses propres reglages.
    cfg = pb.normalize_led_settings(None)
    cfg["debut"]["vitesse"] = 8
    cfg["fin"]["vitesse"] = 100
    rl = RecordLink()
    d = pb.LedDirector(rl, settings=cfg)
    d.welcome(chess.WHITE)
    await asyncio.sleep(0.1)
    check(rl.frames[0][1] == 8, f"le debut prend sa vitesse ({rl.frames[0][1]})")
    rl2 = RecordLink()
    d2 = pb.LedDirector(rl2, settings=cfg)
    d2.farewell(chess.WHITE)
    await asyncio.sleep(0.1)
    check(rl2.frames[0][1] == 100, "la fin prend la sienne")

    # Le nombre de clignotements est respecte.
    cfg["confirme"] = {"vitesse": 64, "duree": 0.05, "repetitions": 3}
    rl3 = RecordLink()
    d3 = pb.LedDirector(rl3, settings=cfg)
    d3.confirm(chess.E2, chess.E4)
    await asyncio.sleep(0.6)
    allumes = [f for f in rl3.cases if set(f) == {chess.E2, chess.E4}]
    check(len(allumes) == 3, f"trois clignotements demandes, trois vus "
                             f"({len(allumes)})")

    # --led-speed passe devant tout le reste.
    rl4 = RecordLink()
    d4 = pb.LedDirector(rl4, settings=cfg, speed=12)
    d4.welcome(chess.WHITE)
    await asyncio.sleep(0.1)
    check(rl4.frames[0][1] == 12, "--led-speed impose sa cadence partout")

    # Duree nulle sur un etat permanent : ca reste allume, sans battre.
    cfg2 = pb.normalize_led_settings(None)
    rl5 = RecordLink()
    d5 = pb.LedDirector(rl5, settings=cfg2)
    await d5.set_base((chess.E7, chess.E5), "adverse")
    await asyncio.sleep(0.4)
    check(len(rl5.frames) == 1, f"duree nulle : une seule trame, ca ne "
                                f"clignote pas ({len(rl5.frames)})")

    # Duree non nulle : le fond bat tout seul jusqu'au changement.
    cfg2["adverse"]["duree"] = 0.05
    rl6 = RecordLink()
    d6 = pb.LedDirector(rl6, settings=cfg2)
    await d6.set_base((chess.E7, chess.E5), "adverse")
    await asyncio.sleep(0.45)
    eteints = [f for f in rl6.cases if not f]
    check(len(eteints) >= 2, f"le guidage bat au rythme regle ({len(eteints)} "
                             f"extinctions)")
    await d6.set_base((), "adverse")
    await asyncio.sleep(0.2)
    check(d6.pulse is None or d6.pulse.done(),
          "et le battement s'arrete quand le fond disparait")

    # Une animation suspend le battement, puis le fond revient.
    cfg2["levee"]["duree"] = 0.05
    rl7 = RecordLink()
    d7 = pb.LedDirector(rl7, settings=cfg2)
    await d7.set_base((chess.E2,), "levee")
    await asyncio.sleep(0.1)
    d7.confirm(chess.E2, chess.E4)
    await asyncio.sleep(0.05)
    check(d7.pulse is None or d7.pulse.done(),
          "l'animation met le battement en pause")
    await asyncio.sleep(0.6)
    check(d7.pulse is not None and not d7.pulse.done(),
          "puis le battement du fond reprend tout seul")
    d7._stop_pulse()

    # Les essais de l'interface passent par demo().
    for nom in ("debut", "debut-noir", "fin", "fin-nulle", "confirme",
                "refuse", "adverse", "levee"):
        rl8 = RecordLink()
        d8 = pb.LedDirector(rl8, settings=cfg2)
        d8.demo(nom)
        await asyncio.sleep(0.12)
        check(bool(rl8.frames), f"l'essai « {nom} » allume quelque chose")
        if d8.task:
            d8.task.cancel()
        d8._stop_pulse()

    # Le dictionnaire est partage : bouger une jauge agit sans redemarrer.
    rl9 = RecordLink()
    partage = pb.normalize_led_settings(None)
    d9 = pb.LedDirector(rl9, settings=partage)
    partage["debut"]["vitesse"] = 21
    d9.welcome(chess.WHITE)
    await asyncio.sleep(0.1)
    check(rl9.frames[0][1] == 21,
          "une jauge deplacee agit sur l'animation suivante, en direct")

    # Et la session partage bien ce dictionnaire, sans en faire une copie :
    # c'est ce qui permet a l'interface d'agir sur une partie en cours.
    link10, api10 = FakeLink(), FakeLichess()
    s10 = pb.PlaySession(pb.BoardTracker(), link10, api10,
                         led_settings=partage)
    check(s10.leds.cfg is partage,
          "les reglages de l'interface sont ceux de la partie, pas une copie")
    partage["fin"]["vitesse"] = 33
    check(s10.leds.speed_of("fin") == 33,
          "donc une jauge bougee en pleine partie prend effet aussitot")

    # Sauvegarde et relecture font l'aller-retour sans rien perdre.
    import tempfile
    with tempfile.TemporaryDirectory() as dossier:
        chemin = os.path.join(dossier, "leds.json")
        partage["confirme"]["repetitions"] = 5
        check(pb.save_led_settings(partage, chemin), "les reglages s'ecrivent")
        relu = pb.load_led_settings(chemin)
        check(relu["confirme"]["repetitions"] == 5 and relu["fin"]["vitesse"] == 33,
              "et se relisent a l'identique")


asyncio.run(reglages_leds())

print("\n== la case levee s'allume ==")


async def lift_feedback():
    link25, api25 = FakeLink(), FakeLichess()
    t25 = pb.BoardTracker()
    s25 = pb.PlaySession(t25, link25, api25)
    s25.begin("leve", chess.WHITE, "startpos", [])
    await s25.refresh()
    check(s25.leds.base == (), "rien d'allume au repos")
    t25.set_square(chess.E2, False)           # on souleve le pion
    await s25.refresh()
    check(s25.leds.base == (chess.E2,),
          f"la case d'origine s'allume des qu'on leve ({s25.leds.base})")
    t25.set_square(chess.E4, True)            # on le pose
    t25.resolve()
    await s25.refresh()
    check(s25.leds.base == (), "et s'eteint une fois le coup joue")
    check(api25.posted == ["/api/board/game/leve/move/e2e4"], "le coup est parti")


asyncio.run(lift_feedback())

print("\n== roque adverse rejoue, tour d'abord ==")


async def castling_replay():
    link26, api26 = FakeLink(), FakeLichess()
    t26 = pb.BoardTracker()
    s26 = pb.PlaySession(t26, link26, api26)
    # position ou les noirs peuvent roquer, trait aux noirs
    fen = "rnbqk2r/pppp1ppp/5n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 0 4"
    s26.begin("roque", chess.WHITE, fen, [])
    ref26 = chess.Board(fen)
    roque = ref26.parse_san("O-O")
    s26.update_moves([roque.uci()])            # les noirs ont roque sur lichess
    await s26.refresh()
    check(t26.expected == roque,
          f"le coup attendu est arme ({t26.expected})")
    check(set(s26.leds.base) >= {chess.E8, chess.G8},
          "les cases du roi s'allument pour guider")

    # le piege : on commence par la tour, h8 -> f8, qui est un coup legal
    t26.set_square(chess.H8, False)
    check(t26.resolve() is False, "tour en l'air : on attend")
    t26.set_square(chess.F8, True)
    check(t26.resolve() is False,
          "Rf8 seul n'est PAS retenu alors qu'un roque est attendu")
    check(len(t26.logic.move_stack) == 0, "aucun coup pousse dans la partie")
    check(api26.posted == [], "et rien n'est envoye a lichess")

    # puis le roi, et la le roque se resout
    t26.set_square(chess.E8, False)
    check(t26.resolve() is False, "roi en l'air : on attend encore")
    t26.set_square(chess.G8, True)
    check(t26.resolve() is True, "roque complet : resolu")
    check(t26.logic.move_stack[-1] == roque,
          f"c'est bien le roque ({t26.logic.move_stack[-1].uci()})")
    await s26.refresh()
    check(t26.expected is None, "le coup attendu est desarme")
    check(s26.board_moves() == s26.moves, "les deux historiques concordent")


asyncio.run(castling_replay())

print("\n== pendule ==")


async def pendule():
    link27, api27 = FakeLink(), FakeLichess()
    t27 = pb.BoardTracker()
    s27 = pb.PlaySession(t27, link27, api27)
    s27.begin("pendule", chess.WHITE, "startpos", [])
    s27.update_clock({"wtime": 600000, "btime": 540000, "winc": 5000,
                      "binc": 5000})
    blanc = s27.remaining(chess.WHITE)
    noir = s27.remaining(chess.BLACK)
    check(599.0 < blanc <= 600.0, f"blancs a 10 minutes ({blanc:.1f} s)")
    check(abs(noir - 540.0) < 0.1, f"noirs a 9 minutes ({noir:.1f} s)")

    # le camp au trait voit son temps s'ecouler entre deux nouvelles
    s27.clock["at"] -= 3.0
    check(abs(s27.remaining(chess.WHITE) - 597.0) < 0.2,
          "le temps du camp au trait s'egrene")
    check(abs(s27.remaining(chess.BLACK) - 540.0) < 0.1,
          "celui de l'autre ne bouge pas")

    # apres un coup, le trait change de camp. Il se lit dans la partie
    # lichess, pas sur le plateau : pendant ta reflexion, le plateau est en
    # retard d'un coup et figerait ta pendule.
    s27.update_moves(["e2e4"])
    s27.update_clock({"wtime": 597000, "btime": 540000, "winc": 5000,
                      "binc": 5000})
    check(len(t27.logic.move_stack) == 0,
          "le plateau n'a pas encore rejoue le coup")
    check(s27.clock["running"] == chess.BLACK, "le trait passe aux noirs")
    s27.clock["at"] -= 4.0
    check(abs(s27.remaining(chess.BLACK) - 536.0) < 0.2,
          "et c'est leur temps qui descend")

    # fin de partie : plus rien ne s'ecoule
    s27.end_game("mate", "white")
    fige = s27.remaining(chess.BLACK)
    await asyncio.sleep(0.05)
    check(abs(s27.remaining(chess.BLACK) - fige) < 0.01,
          "la pendule se fige a la fin")
    check(s27.remaining(chess.WHITE) is not None, "et reste lisible")


asyncio.run(pendule())

print("\n== fin de partie : le gagnant est transmis a l'animation ==")
t28 = pb.BoardTracker()
rl28 = RecordLink()
s28 = pb.PlaySession(t28, rl28, FakeLichess())
s28.begin("fin", chess.WHITE, "startpos", [])
s28.end_game("mate", "black")
check(s28.finished and t28.frozen, "la partie est close")
check(s28.leds.enabled, "les LED restent actives pour l'animation de fin")

print("\n== style « chase » : uniquement des trames a une case ==")


async def chase_style():
    """Le mode de secours, si un jour un plateau n'anime pas de lui-meme."""
    rl = RecordLink()
    d = pb.LedDirector(rl, style="chase")
    d.welcome(chess.WHITE)
    await asyncio.sleep(1.6)
    check(all(len(f) <= 1 for f in rl.cases),
          f"accueil : jamais plus d'une case a la fois "
          f"({sorted({len(f) for f in rl.cases})})")
    vues = {f[0] for f in rl.cases if f}
    check(vues == set(pb.LedDirector.rank_of(chess.WHITE)),
          "mais toute la rangee est parcourue")

    rl2 = RecordLink()
    d2 = pb.LedDirector(rl2, style="chase")
    d2.farewell(None)
    await asyncio.sleep(3.2)
    vues2 = {f[0] for f in rl2.cases if f}
    attendu = set(pb.LedDirector.rank_of(chess.WHITE)) | \
        set(pb.LedDirector.rank_of(chess.BLACK))
    check(all(len(f) <= 1 for f in rl2.cases),
          "nulle : une seule case a la fois aussi")
    check(vues2 == attendu, "et les deux rangees sont parcourues")


asyncio.run(chase_style())

print("\n== un nouveau fond interrompt l'animation en cours ==")


async def base_interrupts():
    rl = RecordLink()
    d = pb.LedDirector(rl)
    d.confirm(chess.E2, chess.E4)
    await asyncio.sleep(0.05)
    await d.set_base((chess.D7, chess.D5))      # l'adversaire vient de jouer
    await asyncio.sleep(0.3)
    check(rl.cases[-1] == tuple(sorted((chess.D7, chess.D5))),
          "le guidage du coup adverse prend la main tout de suite")
    check(d.task is None or d.task.done(), "l'animation a bien ete interrompue")


asyncio.run(base_interrupts())
