"""engine evaluation of every move, per-player stats, and awards, win probability and accuracy use the lichess
"""

import math
import statistics
from dataclasses import dataclass, asdict
from typing import Protocol

import chess
import chess.engine

from app.models import BoardEvent, EventType, MatchRecord


MATE_CP = 10_000     # a forced mate counts as a huge advantage
CP_CLAMP = 1_000     # beyond ±10 pawns, win probability is effectively decided

# Win-probability drop (percentage points) that classifies a move. Same scale as Lichess's
# 0.1 / 0.2 / 0.3 "winning chances" thresholds, which run from -1 to 1 (so ×50 in percentage points).
INACCURACY_DROP = 5.0
MISTAKE_DROP = 10.0
BLUNDER_DROP = 15.0


def win_percent(cp_white: int) -> float:
    """White's chance of winning (0–100) from an evaluation in centipawns, White's point of view."""
    cp = max(-CP_CLAMP, min(CP_CLAMP, cp_white))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def move_accuracy(win_drop: float) -> float:
    """0–100 accuracy for one move, from how many win-percentage points it gave away."""
    raw = 103.1668 * math.exp(-0.04354 * max(0.0, win_drop)) - 3.1669 + 1  # +1: Lichess's uncertainty bonus
    return max(0.0, min(100.0, raw))


def classify(win_drop: float) -> str | None:
    if win_drop >= BLUNDER_DROP:
        return "blunder"
    if win_drop >= MISTAKE_DROP:
        return "mistake"
    if win_drop >= INACCURACY_DROP:
        return "inaccuracy"
    return None


@dataclass(frozen=True)
class Evaluation:
    win_white: float            # White's win chance, 0–100
    best_uci: str | None        # engine's preferred move; None in finished positions


class Evaluator(Protocol):
    def evaluate(self, fen: str) -> Evaluation: ...


class StockfishEvaluator:
    """Evaluates positions with Stockfish, caching by FEN: the position after one move is usually the
    position before the next, so each position is only analysed once."""

    def __init__(self, engine: chess.engine.SimpleEngine, depth: int = 10):
        self._engine = engine
        self._limit = chess.engine.Limit(depth=depth)
        self._cache: dict[str, Evaluation] = {}

    def evaluate(self, fen: str) -> Evaluation:
        if fen not in self._cache:
            self._cache[fen] = self._analyse(fen)
        return self._cache[fen]

    def _analyse(self, fen: str) -> Evaluation:
        board = chess.Board(fen)
        terminal = terminal_evaluation(board)
        if terminal:
            return terminal
        info = self._engine.analyse(board, self._limit)
        cp_white = info["score"].white().score(mate_score=MATE_CP)
        pv = info.get("pv") or []
        return Evaluation(win_percent(cp_white), pv[0].uci() if pv else None)


def terminal_evaluation(board: chess.Board) -> Evaluation | None:
    """Finished positions are decided by the rules, not the engine."""
    if board.is_checkmate():
        # The side to move is mated, so the other side has won.
        return Evaluation(0.0 if board.turn == chess.WHITE else 100.0, None)
    if board.is_stalemate() or board.is_insufficient_material():
        return Evaluation(50.0, None)
    return None


@dataclass(frozen=True)
class AnalysedMove:
    player_id: int
    board_id: int
    game_number: int
    move_number: int            # full-move number, as players count moves
    uci: str
    san: str
    fen_before: str
    best_uci: str | None
    best_san: str | None
    win_drop: float             # win-percentage points this move gave away (0 = as good as the engine's choice)
    accuracy: float
    label: str | None           # "inaccuracy" | "mistake" | "blunder" | None
    think_ms: int
    captured: str | None
    points: int
    bounty: bool
    checkmate: bool


def analyse_move(event: BoardEvent, board_id: int, game_number: int, evaluator: Evaluator) -> AnalysedMove:
    board = chess.Board(event.fen_before)
    mover_is_white = board.turn == chess.WHITE
    move = chess.Move.from_uci(event.uci)
    san = board.san(move)
    move_number = board.fullmove_number

    before = evaluator.evaluate(event.fen_before)
    best_san = board.san(chess.Move.from_uci(before.best_uci)) if before.best_uci else None
    board.push(move)
    after = evaluator.evaluate(board.fen())

    # Everything from the mover's point of view
    win_before = before.win_white if mover_is_white else 100 - before.win_white
    win_after = after.win_white if mover_is_white else 100 - after.win_white
    win_drop = max(0.0, win_before - win_after)
    if event.uci == before.best_uci:
        win_drop = 0.0  # the engine's own choice can't be a mistake (removes depth noise)

    return AnalysedMove(
        player_id=event.player_id, board_id=board_id, game_number=game_number, move_number=move_number,
        uci=event.uci, san=san, fen_before=event.fen_before, best_uci=before.best_uci, best_san=best_san,
        win_drop=round(win_drop, 1), accuracy=round(move_accuracy(win_drop), 1), label=classify(win_drop),
        think_ms=event.think_ms, captured=event.captured, points=event.points, bounty=event.bounty,
        checkmate=event.checkmate,
    )


def analyse_match(match: MatchRecord, evaluator: Evaluator) -> list[AnalysedMove]:
    """Every move in the match, analysed. Timeouts aren't moves, so they're skipped here."""
    moves = []
    for game in match.games:
        for event in game.events:
            if event.type == EventType.MOVE and event.uci:
                moves.append(analyse_move(event, game.board_id, game.game_number, evaluator))
    return moves


MIN_MOVES_FOR_RATES = 5   # accuracy and speed aren't meaningful from one or two moves


@dataclass(frozen=True)
class Moment:
    """A specific move worth talking about. Moments are the only moves the commentary may refer to."""
    id: str
    kind: str                 # "worst_move" | "checkmate"
    player_id: int
    board_id: int
    game_number: int
    move_number: int
    san: str
    best_san: str | None
    win_drop: float


@dataclass(frozen=True)
class PlayerStats:
    player_id: int
    name: str
    bot: bool
    score: int
    rank: int                 # competition ranking: equal scores share a rank
    moves: int
    accuracy: float | None    # None when fewer than MIN_MOVES_FOR_RATES moves
    blunders: int
    mistakes: int
    inaccuracies: int
    captures: int
    bounty_captures: int
    checkmates: int
    timeouts: int
    median_think_ms: int | None
    worst_move: Moment | None


@dataclass(frozen=True)
class Award:
    id: str
    title: str
    player_id: int
    value: str                # the number behind the award, ready to display
    reason: str               # one factual line explaining why


def player_stats(match: MatchRecord, moves: list[AnalysedMove]) -> list[PlayerStats]:
    timeouts = {p.player_id: 0 for p in match.players}
    for game in match.games:
        for event in game.events:
            if event.type in (EventType.TIMEOUT_PASS, EventType.TIMEOUT_MATE):
                timeouts[event.player_id] += 1

    scores = sorted((p.score for p in match.players), reverse=True)
    result = []
    for p in match.players:
        mine = [m for m in moves if m.player_id == p.player_id]
        enough = len(mine) >= MIN_MOVES_FOR_RATES
        worst = max((m for m in mine if m.label), key=lambda m: m.win_drop, default=None)
        result.append(PlayerStats(
            player_id=p.player_id, name=p.name, bot=p.bot, score=p.score,
            rank=scores.index(p.score) + 1,
            moves=len(mine),
            accuracy=round(statistics.fmean(m.accuracy for m in mine), 1) if enough else None,
            blunders=sum(m.label == "blunder" for m in mine),
            mistakes=sum(m.label == "mistake" for m in mine),
            inaccuracies=sum(m.label == "inaccuracy" for m in mine),
            captures=sum(m.captured is not None for m in mine),
            bounty_captures=sum(m.bounty for m in mine),
            checkmates=sum(m.checkmate for m in mine),
            timeouts=timeouts[p.player_id],
            median_think_ms=int(statistics.median(m.think_ms for m in mine)) if enough else None,
            worst_move=_moment("worst_move", worst) if worst else None,
        ))
    return result


def awards(stats: list[PlayerStats]) -> list[Award]:
    """Each award goes to one player. Ties go to the player with more moves, then the lower seat."""
    result: list[Award] = []

    def pick(candidates, key):
        candidates = list(candidates)
        if not candidates:
            return None
        return max(candidates, key=lambda s: (key(s), s.moves, -s.player_id))

    top = pick(stats, lambda s: s.score)
    if top and top.score > 0 and sum(s.score == top.score for s in stats) == 1:
        result.append(Award("top_scorer", "Top Scorer", top.player_id, f"{top.score} pts",
                            f"Most points: {top.score}."))

    rated = [s for s in stats if s.accuracy is not None]
    sharpest = pick(rated, lambda s: s.accuracy)
    if sharpest:
        if top and sharpest.player_id != top.player_id and sharpest.rank > 1:
            result.append(Award("robbed", "Robbed", sharpest.player_id, f"{pct(sharpest.accuracy)} accuracy",
                                f"Most accurate player ({pct(sharpest.accuracy)}) but finished #{sharpest.rank}."))
        else:
            result.append(Award("sharpest", "Sharpest Player", sharpest.player_id, f"{pct(sharpest.accuracy)} accuracy",
                                f"Highest accuracy: {pct(sharpest.accuracy)}."))

    blunderer = pick((s for s in stats if s.blunders > 0), lambda s: s.blunders)
    if blunderer:
        result.append(Award("blunder_machine", "Blunder Machine", blunderer.player_id,
                            plural(blunderer.blunders, "blunder"), f"Most blunders: {blunderer.blunders}."))

    hunter = pick((s for s in stats if s.bounty_captures > 0), lambda s: s.bounty_captures)
    if hunter:
        result.append(Award("bounty_hunter", "Bounty Hunter", hunter.player_id,
                            plural(hunter.bounty_captures, "bounty capture"),
                            f"Most captures from the leader: {hunter.bounty_captures}."))

    finisher = pick((s for s in stats if s.checkmates > 0), lambda s: s.checkmates)
    if finisher:
        result.append(Award("checkmate_artist", "Checkmate Artist", finisher.player_id,
                            plural(finisher.checkmates, "checkmate"), f"Most checkmates: {finisher.checkmates}."))

    fastest = pick((s for s in stats if s.median_think_ms is not None), lambda s: -s.median_think_ms)
    if fastest:
        seconds = fastest.median_think_ms / 1000
        result.append(Award("speed_demon", "Speed Demon", fastest.player_id, f"{seconds:.1f} s per move",
                            f"Fastest typical move: {seconds:.1f} s."))

    victim = pick((s for s in stats if s.timeouts > 0), lambda s: s.timeouts)
    if victim:
        result.append(Award("clock_victim", "Clock Victim", victim.player_id, plural(victim.timeouts, "timeout"),
                            f"Most turns lost to the clock: {victim.timeouts}."))
    return result


def pct(value: float) -> str:
    """91.0 -> "91%", 91.5 -> "91.5%"."""
    return f"{value:g}%"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def to_dict(obj) -> dict:
    return asdict(obj)


def _moment(kind: str, m: AnalysedMove) -> Moment:
    return Moment(id=f"b{m.board_id}g{m.game_number}m{m.move_number}p{m.player_id}", kind=kind,
                  player_id=m.player_id, board_id=m.board_id, game_number=m.game_number,
                  move_number=m.move_number, san=m.san, best_san=m.best_san, win_drop=m.win_drop)
