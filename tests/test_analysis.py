"""Analysis formulas, single moves, and award rules. A fake evaluator keeps the numbers exact.
"""

from dataclasses import replace

import chess
import pytest

from app.analysis import BLUNDER_DROP, Evaluation, analyse_move, classify, move_accuracy, terminal_evaluation, win_percent, PlayerStats, awards
from app.models import BoardEvent, EventType


class FakeEvaluator:
    """Returns chosen evaluations per position; anything not listed is an even position."""

    def __init__(self, evals: dict[str, Evaluation]):
        self.evals = evals

    def evaluate(self, fen: str) -> Evaluation:
        return self.evals.get(fen, Evaluation(50.0, None))


def fen_after(fen: str, uci: str) -> str:
    board = chess.Board(fen)
    board.push_uci(uci)
    return board.fen()


def move_event(player_id: int, fen: str, uci: str, think_ms: int = 1000) -> BoardEvent:
    return BoardEvent(type=EventType.MOVE, player_id=player_id, fen_before=fen, uci=uci, think_ms=think_ms,
                      captured=None, points=0, bounty=False, checkmate=False, at_epoch_ms=0)


START = chess.STARTING_FEN


# ---------- formulas ----------

def test_win_percent_is_even_at_zero_and_symmetric():
    assert win_percent(0) == pytest.approx(50.0)
    assert win_percent(300) == pytest.approx(100 - win_percent(-300))
    assert win_percent(10_000) == pytest.approx(win_percent(1_000))  # capped at ±10 pawns, like Lichess
    assert win_percent(10_000) > 97


def test_accuracy_is_100_for_a_perfect_move_and_falls_with_the_drop():
    assert move_accuracy(0) == 100.0
    assert move_accuracy(10) < move_accuracy(5) < 100
    assert move_accuracy(100) == 0.0


@pytest.mark.parametrize("drop, label", [(0, None), (4.9, None), (5, "inaccuracy"),
                                         (10, "mistake"), (15, "blunder"), (60, "blunder")])
def test_classification_thresholds(drop, label):
    assert classify(drop) == label


def test_checkmated_position_is_decided_by_the_rules():
    mated = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")  # Fool's mate
    assert terminal_evaluation(mated) == Evaluation(0.0, None)  # White is mated


# ---------- one move ----------

def test_engine_choice_costs_nothing():
    after = fen_after(START, "e2e4")
    evaluator = FakeEvaluator({START: Evaluation(55.0, "e2e4"), after: Evaluation(52.0, None)})

    move = analyse_move(move_event(1, START, "e2e4"), board_id=1, game_number=1, evaluator=evaluator)

    assert move.win_drop == 0.0          # depth noise ignored for the engine's own move
    assert move.label is None
    assert move.san == "e4"


def test_drop_is_measured_from_the_movers_point_of_view():
    # Black to move. Before: White 40% (Black 60%). After Black's move: White 80% (Black 20%).
    fen = fen_after(START, "e2e4")
    after = fen_after(fen, "f7f6")
    evaluator = FakeEvaluator({fen: Evaluation(40.0, "e7e5"), after: Evaluation(80.0, None)})

    move = analyse_move(move_event(2, fen, "f7f6"), 1, 1, evaluator)

    assert move.win_drop == 40.0
    assert move.label == "blunder"
    assert move.best_san == "e5"
    assert move.move_number == 1


def test_a_move_that_improves_on_the_estimate_counts_as_no_loss():
    after = fen_after(START, "d2d4")
    evaluator = FakeEvaluator({START: Evaluation(50.0, "e2e4"), after: Evaluation(58.0, None)})

    move = analyse_move(move_event(1, START, "d2d4"), 1, 1, evaluator)

    assert move.win_drop == 0.0
    assert move.accuracy == 100.0
    assert BLUNDER_DROP > 0


def stats(player_id: int, *, score: int = 0, rank: int = 1, moves: int = 10, accuracy: float | None = 80.0,
          blunders: int = 0, bounty: int = 0, checkmates: int = 0, timeouts: int = 0,
          think: int | None = 5000) -> PlayerStats:
    return PlayerStats(player_id=player_id, name=f"P{player_id}", bot=False, score=score, rank=rank, moves=moves,
                       accuracy=accuracy, blunders=blunders, mistakes=0, inaccuracies=0, captures=0,
                       bounty_captures=bounty, checkmates=checkmates, timeouts=timeouts,
                       median_think_ms=think, worst_move=None)


def by_id(result):
    return {a.id: a for a in result}


def test_most_accurate_player_who_did_not_win_is_robbed():
    result = by_id(awards([stats(1, score=30, rank=1, accuracy=70.0), stats(2, score=5, rank=2, accuracy=91.0)]))

    assert result["top_scorer"].player_id == 1
    assert result["robbed"].player_id == 2
    assert result["robbed"].value == "91% accuracy"
    assert "sharpest" not in result


def test_winner_who_was_also_most_accurate_is_sharpest_not_robbed():
    result = by_id(awards([stats(1, score=30, rank=1, accuracy=90.0), stats(2, score=5, rank=2, accuracy=70.0)]))

    assert result["sharpest"].player_id == 1
    assert "robbed" not in result


def test_tied_top_score_gives_no_top_scorer():
    result = by_id(awards([stats(1, score=10, rank=1), stats(2, score=10, rank=1)]))
    assert "top_scorer" not in result


def test_awards_need_something_to_count():
    result = by_id(awards([stats(1, score=5), stats(2, score=3, rank=2)]))
    for absent in ("blunder_machine", "bounty_hunter", "checkmate_artist", "clock_victim"):
        assert absent not in result


def test_players_with_too_few_moves_are_not_rated():
    # P1 won but made too few moves to be rated, so P2 is the most accurate rated player: robbed.
    result = by_id(awards([stats(1, score=5, accuracy=None, think=None), stats(2, score=3, rank=2, think=None)]))
    assert result["robbed"].player_id == 2
    assert "speed_demon" not in result


def test_speed_demon_is_the_lowest_median_think_time():
    result = by_id(awards([stats(1, think=4200), stats(2, think=2100), stats(3, think=None)]))
    assert result["speed_demon"].player_id == 2
    assert result["speed_demon"].value == "2.1 s per move"


def test_singular_and_plural_wording():
    result = by_id(awards([stats(1, blunders=1, checkmates=2, timeouts=1)]))
    assert result["blunder_machine"].value == "1 blunder"
    assert result["checkmate_artist"].value == "2 checkmates"
    assert result["clock_victim"].value == "1 timeout"


def test_ties_go_to_the_player_with_more_moves():
    a = stats(1, blunders=3, moves=10)
    b = replace(stats(2, blunders=3), moves=20)
    assert by_id(awards([a, b]))["blunder_machine"].player_id == 2
