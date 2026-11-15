"""Template commentary: the right line for each situation, correct numbers, and never a broken sentence.
"""

import random
import re

from app.analysis import Moment, PlayerStats, awards
from app.commentary import HEADLINES, LINES, build_commentary


MAX_LENGTH = 140


def stats(player_id: int, *, name: str | None = None, score: int = 0, rank: int = 1, moves: int = 10,
          accuracy: float | None = 80.0, blunders: int = 0, bounty: int = 0, checkmates: int = 0,
          timeouts: int = 0, think: int | None = 5000, worst: Moment | None = None) -> PlayerStats:
    return PlayerStats(player_id=player_id, name=name or f"P{player_id}", bot=False, score=score, rank=rank,
                       moves=moves, accuracy=accuracy, blunders=blunders, mistakes=0, inaccuracies=0, captures=0,
                       bounty_captures=bounty, checkmates=checkmates, timeouts=timeouts,
                       median_think_ms=think, worst_move=worst)


def worst_move(player_id: int, san: str = "g4", best: str | None = "e4", number: int = 2) -> Moment:
    return Moment(id=f"m{player_id}", kind="worst_move", player_id=player_id, board_id=1, game_number=1,
                  move_number=number, san=san, best_san=best, win_drop=40.0)


def commentary_for(players: list[PlayerStats], match_id: str = "match-1"):
    return build_commentary(match_id, players, awards(players))


def quip(result, player_id: int):
    return next(q for q in result.quips if q.player_id == player_id)


# ---------- the right line for the situation ----------

def test_robbed_player_gets_the_robbed_line():
    players = [stats(1, name="Bob", score=30, rank=1, accuracy=60.0),
               stats(2, name="Alice", score=4, rank=2, accuracy=91.5)]
    result = commentary_for(players)

    alice = quip(result, 2)
    assert "Alice" in alice.text and "91.5%" in alice.text
    assert "award:robbed" in alice.facts
    assert "accuracy" in alice.facts
    assert result.headline in {h.format(winner="Bob", robbed="Alice") for h in HEADLINES["robbed"]}


def test_winner_who_was_also_most_accurate_gets_the_dominant_line():
    players = [stats(1, name="Bob", score=30, rank=1, accuracy=92.0), stats(2, score=4, rank=2, accuracy=70.0)]
    bob = quip(commentary_for(players), 1)
    assert "30" in bob.text and "92%" in bob.text


def test_blunder_machine_line_uses_the_real_count():
    players = [stats(1, score=30, rank=1, accuracy=90.0), stats(2, score=4, rank=2, blunders=3, accuracy=50.0)]
    line = quip(commentary_for(players), 2)
    assert "3 blunders" in line.text or "3 times" in line.text
    assert line.facts[0] == "blunders"


def test_player_without_an_award_gets_their_worst_move():
    players = [stats(1, score=30, rank=1, accuracy=90.0, blunders=0, think=4000),
               stats(2, name="Cara", score=4, rank=2, accuracy=70.0, think=6000, worst=worst_move(2))]
    line = quip(commentary_for(players), 2)
    assert "g4" in line.text and "2" in line.text
    assert line.facts == ("worst_move",)


def test_rated_player_with_nothing_notable_gets_a_steady_line_not_the_unrated_one():
    players = [stats(1, score=30, rank=1, think=3000), stats(2, name="Eve", score=4, rank=2, moves=40, blunders=1,
                                                              accuracy=75.0, think=6000)]
    players[1] = PlayerStats(**{**players[1].__dict__, "blunders": 1})
    line = quip(commentary_for(players), 2)
    assert "even here" not in line.text and "mysterious" not in line.text


def test_player_with_too_few_moves_gets_the_unrated_line():
    players = [stats(1, score=30, rank=1), stats(2, name="Dan", score=0, rank=2, moves=1, accuracy=None, think=None)]
    line = quip(commentary_for(players), 2)
    assert "1 move" in line.text and "1 moves" not in line.text


def test_tie_at_the_top_headline_names_everyone():
    players = [stats(1, name="Ann", score=12, rank=1), stats(2, name="Ben", score=12, rank=1),
               stats(3, name="Cy", score=3, rank=3)]
    assert "Ann and Ben" in commentary_for(players).headline


def test_no_points_headline():
    players = [stats(1, score=0), stats(2, score=0)]
    assert commentary_for(players).headline == HEADLINES["no_points"][0]


# ---------- stability and variety ----------

def test_same_match_always_gives_the_same_text():
    players = [stats(1, score=30, rank=1), stats(2, score=4, rank=2, blunders=2)]
    assert commentary_for(players, "m-42") == commentary_for(players, "m-42")


def test_different_matches_get_different_wordings():
    players = [stats(1, score=30, rank=1), stats(2, score=4, rank=2, blunders=2)]
    texts = {quip(commentary_for(players, f"m-{i}"), 2).text for i in range(20)}
    assert len(texts) > 1


# ---------- never a broken sentence ----------

def numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def test_random_situations_never_produce_broken_or_unsupported_lines():
    rng = random.Random(1234)
    for case in range(500):
        n = rng.randint(2, 4)
        scores = [rng.choice([0, 0, 1, 3, 5, 9, 15, 16, 30]) for _ in range(n)]
        ordered = sorted(scores, reverse=True)
        players = []
        for i in range(n):
            rated = rng.random() > 0.2
            pid = i + 1
            players.append(stats(
                pid, score=scores[i], rank=ordered.index(scores[i]) + 1, moves=rng.randint(5, 60) if rated else rng.randint(0, 4),
                accuracy=round(rng.uniform(30, 100), 1) if rated else None, blunders=rng.randint(0, 6),
                bounty=rng.randint(0, 3), checkmates=rng.randint(0, 2), timeouts=rng.randint(0, 3),
                think=rng.randint(800, 20000) if rated else None,
                worst=worst_move(pid, best=rng.choice(["e4", None])) if rng.random() > 0.3 else None))

        result = commentary_for(players, f"case-{case}")
        assert len(result.quips) == n
        for line in [result.headline, *(q.text for q in result.quips)]:
            assert "{" not in line and "None" not in line, line
            assert len(line) <= MAX_LENGTH, line
            assert not re.search(r"\b1 (blunders|checkmates|moves|turns|bounty captures)\b", line), line

        for q in result.quips:
            player = next(p for p in players if p.player_id == q.player_id)
            allowed = {str(v) for v in (player.score, player.rank, player.moves, player.blunders, player.checkmates,
                                         player.bounty_captures, player.timeouts)}
            if player.accuracy is not None:
                allowed |= numbers_in(f"{player.accuracy:g}")
            if player.median_think_ms is not None:
                allowed.add(f"{player.median_think_ms / 1000:.1f}")
            if player.worst_move:
                allowed |= {str(player.worst_move.move_number)} | numbers_in(player.worst_move.san)
                allowed |= numbers_in(player.worst_move.best_san or "")
            allowed |= numbers_in(player.name)
            assert numbers_in(q.text) <= allowed, (q.text, allowed)


def test_every_wording_fits_on_a_results_card():
    longest_name = "N" * 16  # nickname limit
    for key, options in LINES.items():
        for template in options:
            filled = template.format(
                name=longest_name, score=999, rank=4, accuracy_text="99.9%", blunders_count="99 blunders",
                blunders_text="99 times", checkmates_count="99 checkmates", bounty_count="99 bounty captures",
                timeouts_count="99 turns", timeouts_text="99 times", think_seconds="19.9", moves_count="999 moves",
                worst_san="Qxf7+", worst_move_number=99, best_san="Nxe5+", accuracy=99.9, blunders=99,
                checkmates=99, bounty_captures=99, timeouts=99, moves=999)
            assert len(filled) <= MAX_LENGTH + 20, (key, filled)   # worst case with 16-char names twice
