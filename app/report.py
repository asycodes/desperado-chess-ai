"""Builds the report for a match: analysis, then awards, then commentary.
"""

import time
from collections.abc import Callable
from contextlib import contextmanager

import chess.engine

from app.analysis import Evaluator, StockfishEvaluator, analyse_match, awards, player_stats, to_dict
from app.commentary import build_commentary
from app.models import MatchRecord


REPORT_VERSION = 3


def build_report_with(match: MatchRecord, evaluator: Evaluator) -> dict:
    started = time.perf_counter()
    moves = analyse_match(match, evaluator)
    stats = player_stats(match, moves)
    award_list = awards(stats)
    commentary = build_commentary(match.match_id, stats, award_list)
    standings = sorted(stats, key=lambda s: (s.rank, s.player_id))
    return {
        "version": REPORT_VERSION,
        "matchId": match.match_id,
        "standings": [{"playerId": s.player_id, "name": s.name, "score": s.score, "rank": s.rank, "bot": s.bot}
                      for s in standings],
        "players": [_camel(to_dict(s)) for s in stats],
        "awards": [_camel(to_dict(a)) for a in award_list],
        "commentary": {
            "headline": commentary.headline,
            "quips": [{"playerId": q.player_id, "text": q.text, "facts": list(q.facts), "source": q.source}
                      for q in commentary.quips],
        },
        "meta": {"movesAnalysed": len(moves), "analysisMs": round((time.perf_counter() - started) * 1000)},
    }


@contextmanager
def stockfish(path: str):
    engine = chess.engine.SimpleEngine.popen_uci(path)
    try:
        yield engine
    finally:
        engine.quit()


def make_report_builder(stockfish_path: str, depth: int) -> Callable[[MatchRecord], dict]:
    """One engine process per job: simple, isolated, and the start-up cost (~50 ms) is negligible."""
    def build(match: MatchRecord) -> dict:
        with stockfish(stockfish_path) as engine:
            return build_report_with(match, StockfishEvaluator(engine, depth=depth))
    return build


def _camel(d: dict) -> dict:
    def conv(key: str) -> str:
        head, *rest = key.split("_")
        return head + "".join(w.title() for w in rest)
    return {conv(k): (_camel(v) if isinstance(v, dict) else v) for k, v in d.items()}
