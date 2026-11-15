"""Generates tests/fixtures/sample_match.json: a small but realistic match record in Desperado's format.

Board 1 (Alice vs Bob): Bob wins game 1 by Fool's mate; game 2 is unfinished and includes a timeout.
Used by tests now, and as a starting point for the evaluation set later.
"""

import json
from pathlib import Path

import chess

PIECE_NAMES = {chess.PAWN: "PAWN", chess.KNIGHT: "KNIGHT", chess.BISHOP: "BISHOP",
               chess.ROOK: "ROOK", chess.QUEEN: "QUEEN", chess.KING: "KING"}
VALUES = {"PAWN": 1, "KNIGHT": 3, "BISHOP": 3, "ROOK": 5, "QUEEN": 9}
T0 = 1794400000000


def play_game(board_id, game_number, white, black, moves, t_start, result, timeout_after=None):
    board = chess.Board()
    events, t = [], t_start
    for i, uci in enumerate(moves):
        mover = white if board.turn == chess.WHITE else black
        move = chess.Move.from_uci(uci)
        assert board.is_legal(move), f"illegal {uci} in {board.fen()}"
        captured = board.piece_type_at(move.to_square)
        if board.is_en_passant(move):
            captured = chess.PAWN
        fen_before = board.fen()
        board.push(move)
        mate = board.is_checkmate()
        name = PIECE_NAMES[captured] if captured else None
        points = (VALUES.get(name, 0) if name else 0) + (15 if mate else 0)
        think = 2000 + 700 * i
        t += think
        events.append({"type": "MOVE", "playerId": mover, "fenBefore": fen_before, "uci": uci,
                       "thinkMs": think, "captured": name, "points": points, "bounty": False,
                       "checkmate": mate, "atEpochMs": t})
        if timeout_after is not None and i == timeout_after:
            stalled = white if board.turn == chess.WHITE else black
            t += 60000
            events.append({"type": "TIMEOUT_PASS", "playerId": stalled, "fenBefore": board.fen(), "uci": None,
                           "thinkMs": 60000, "captured": None, "points": 0, "bounty": False,
                           "checkmate": False, "atEpochMs": t})
            fen = board.fen().split(" ")
            fen[1] = "b" if fen[1] == "w" else "w"
            fen[3] = "-"
            board = chess.Board(" ".join(fen))
    return {"boardId": board_id, "gameNumber": game_number, "whiteId": white, "blackId": black,
            "result": result, "events": events}, t


ALICE, BOB = 1, 2
game1, t = play_game(1, 1, ALICE, BOB, ["f2f3", "e7e5", "g2g4", "d8h4"], T0, "BLACK_WON")
game2, t = play_game(1, 2, BOB, ALICE, ["e2e4", "d7d5", "e4d5", "b1c3", "d8d5"], t, "UNFINISHED", timeout_after=2)

record = {
    "eventId": "match-sample-1",
    "type": "match.finished",
    "match": {
        "matchId": "match-sample-1",
        "roomCode": "HSGG",
        "startedAtMs": T0,
        "endedAtMs": T0 + 600000,
        "players": [
            {"playerId": ALICE, "name": "Alice", "position": 1, "bot": False, "score": 0},
            {"playerId": BOB, "name": "Bob", "position": 2, "bot": False,
             "score": sum(e["points"] for g in (game1, game2) for e in g["events"] if e["playerId"] == BOB)},
        ],
        "games": [game1, game2],
    },
}
record["match"]["players"][0]["score"] = sum(
    e["points"] for g in (game1, game2) for e in g["events"] if e["playerId"] == ALICE)

out = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sample_match.json"
out.write_text(json.dumps(record, indent=2))
print(f"wrote {out}")
