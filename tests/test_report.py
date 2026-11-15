"""The full report on the sample match, with the real engine. Skipped when Stockfish isn't found.
"""

import os
import shutil

import pytest

from app.main import Settings
from app.models import WebhookPayload
from app.report import make_report_builder


def find_stockfish() -> str | None:
    """Same lookup as the app: STOCKFISH_PATH from the environment or .env, else 'stockfish' on PATH."""
    path = Settings().stockfish_path
    return shutil.which(path) or (path if os.path.isfile(path) else None)


STOCKFISH = find_stockfish()
pytestmark = pytest.mark.skipif(not STOCKFISH, reason="Stockfish not found (set STOCKFISH_PATH in .env)")


@pytest.fixture
def report(sample_payload) -> dict:
    match = WebhookPayload.model_validate(sample_payload).match
    return make_report_builder(STOCKFISH, depth=10)(match)


def test_fools_mate_blunder_is_alices_worst_move(report):
    alice = next(p for p in report["players"] if p["name"] == "Alice")
    assert alice["worstMove"]["san"] == "g4"
    assert alice["worstMove"]["winDrop"] >= 15
    assert alice["blunders"] >= 1
    assert alice["timeouts"] == 1


def test_awards_match_what_happened(report):
    awards = {a["id"]: a for a in report["awards"]}
    bob_id = next(p["playerId"] for p in report["players"] if p["name"] == "Bob")
    assert awards["top_scorer"]["playerId"] == bob_id
    assert awards["checkmate_artist"]["playerId"] == bob_id
    assert awards["clock_victim"]["value"] == "1 timeout"


def test_every_player_gets_one_comment_and_a_headline(report):
    commentary = report["commentary"]
    assert commentary["headline"]
    assert sorted(q["playerId"] for q in commentary["quips"]) == sorted(p["playerId"] for p in report["players"])
    assert all(q["source"] == "template" for q in commentary["quips"])


def test_report_shape(report):
    assert report["version"] == 3
    assert [s["name"] for s in report["standings"]] == ["Bob", "Alice"]
    assert report["meta"]["movesAnalysed"] == 9
