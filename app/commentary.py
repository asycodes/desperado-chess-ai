import hashlib
import string
from dataclasses import dataclass

from app.analysis import Award, PlayerStats, pct, plural


@dataclass(frozen=True)
class Quip:
    player_id: int
    text: str
    facts: tuple[str, ...]   # which facts the line relies on, e.g. ("accuracy", "rank")
    source: str = "template"


@dataclass(frozen=True)
class Commentary:
    headline: str
    quips: tuple[Quip, ...]


# Each situation has a few wordings. {placeholders} are filled from the player's facts.
LINES: dict[str, list[str]] = {
    "robbed": [
        "{name} played {accuracy_text} accurate chess and finished #{rank}. Robbed in broad daylight.",
        "The cleanest chess in the room ({accuracy_text}) belonged to {name}. The points did not.",
        "{name}: {accuracy_text} accuracy, rank #{rank}. Somewhere a coach is crying.",
    ],
    "dominant": [
        "{name} took the most points ({score}) and played the cleanest chess ({accuracy_text}). No notes.",
        "{score} points and {accuracy_text} accuracy. {name} wasn't playing the same game as everyone else.",
    ],
    "messy_winner": [
        "{name} won with {score} points. Accuracy: {accuracy_text}. Results are results.",
        "{score} points for {name}. Not always pretty ({accuracy_text} accuracy), but it counts.",
    ],
    "winner_unrated": [
        "{name} won with {score} points. Efficient.",
        "{score} points and the top spot for {name}.",
    ],
    "blunder_machine": [
        "{name} blundered {blunders_text} and still finished #{rank}. Chaos is a strategy.",
        "{blunders_count} from {name}. Generous with the pieces today.",
    ],
    "checkmate_artist": [
        "{name} delivered {checkmates_count}. Clinical when it counted.",
        "When {name} smelled mate, {name} took it: {checkmates_count}.",
    ],
    "bounty_hunter": [
        "{name} hunted the leader and cashed {bounty_count}.",
        "{bounty_count} off the leader. {name} came for the bounty.",
    ],
    "clock_victim": [
        "The clock beat {name} {timeouts_text}. Thinking is allowed, just not that much.",
        "{name} lost {timeouts_count} to the clock. Deep thoughts, no moves.",
    ],
    "speed_demon": [
        "{name} moved in {think_seconds} s on average. Blink and you missed it.",
        "{think_seconds} s per move. {name} plays like the board is on fire.",
    ],
    "worst_move": [
        "{name}'s {worst_san} on move {worst_move_number} cost the most. {best_san} was right there.",
        "Move {worst_move_number}, {worst_san}: {name}'s costliest moment. The engine wanted {best_san}.",
    ],
    "worst_move_no_alt": [
        "{name}'s {worst_san} on move {worst_move_number} was the one to forget.",
    ],
    "clean": [
        "{name} got through {moves_count} without a single blunder. Quietly solid.",
        "Zero blunders in {moves_count}. {name} kept it tidy.",
    ],
    "steady": [
        "{name}: {accuracy_text} accuracy over {moves_count}. Steady.",
        "{moves_count} at {accuracy_text} accuracy. {name} kept the boards ticking over.",
    ],
    "unrated": [
        "{name} made {moves_count}. Was {name} even here?",
        "Just {moves_count} from {name}. A mysterious presence.",
    ],
}

HEADLINES: dict[str, list[str]] = {
    "robbed": ["Points went to {winner}. The chess went to {robbed}.",
               "{winner} won it, but {robbed} played it best."],
    "dominant": ["{winner} wins it, and deserved it.",
                 "{winner} runs the table with {score} points."],
    "winner": ["{winner} takes it with {score} points.",
               "{score} points and the win for {winner}."],
    "tie": ["A tie at the top: {tied} on {score} points.",
            "Nobody blinked: {tied} share first on {score}."],
    "no_points": ["Nobody scored. A match of pure defence.",],
}


def build_commentary(match_id: str, stats: list[PlayerStats], awards: list[Award]) -> Commentary:
    by_player: dict[int, dict[str, Award]] = {}
    for award in awards:
        by_player.setdefault(award.player_id, {})[award.id] = award

    quips = tuple(_player_line(match_id, s, by_player.get(s.player_id, {})) for s in stats)
    return Commentary(headline=_headline(match_id, stats, awards), quips=quips)


def _player_line(match_id: str, s: PlayerStats, mine: dict[str, Award]) -> Quip:
    key, award_id = _situation(s, mine)
    template = _choose(LINES[key], match_id, s.player_id)
    used = tuple(FACT_OF[p] for p in _placeholders(template) if FACT_OF.get(p)) + ((f"award:{award_id}",) if award_id else ())
    return Quip(player_id=s.player_id, text=template.format(**_facts(s)), facts=tuple(dict.fromkeys(used)))


# Which underlying fact each placeholder shows (name isn't a claim, so it isn't listed).
FACT_OF = {
    "score": "score", "rank": "rank", "accuracy_text": "accuracy", "blunders_count": "blunders",
    "blunders_text": "blunders", "checkmates_count": "checkmates", "bounty_count": "bounty_captures",
    "timeouts_count": "timeouts", "timeouts_text": "timeouts", "think_seconds": "median_think_ms",
    "moves_count": "moves", "worst_san": "worst_move", "worst_move_number": "worst_move",
    "best_san": "worst_move",
}


def _placeholders(template: str) -> list[str]:
    return [field for _, field, _, _ in string.Formatter().parse(template) if field]


def _situation(s: PlayerStats, mine: dict[str, Award]) -> tuple[str, str | None]:
    """The first rule that applies decides the line, so every player gets exactly one.
    Returns the situation and the award it's about (if any)."""
    if "robbed" in mine:
        return "robbed", "robbed"
    if "top_scorer" in mine:
        if "sharpest" in mine:
            return "dominant", "top_scorer"
        return ("messy_winner" if s.accuracy is not None else "winner_unrated"), "top_scorer"
    for award_id in ("blunder_machine", "checkmate_artist", "bounty_hunter", "clock_victim", "speed_demon"):
        if award_id in mine:
            return award_id, award_id
    if s.worst_move is not None:
        return ("worst_move" if s.worst_move.best_san else "worst_move_no_alt"), None
    if s.accuracy is None:
        return "unrated", None
    if s.blunders == 0:
        return "clean", None
    return "steady", None


def _headline(match_id: str, stats: list[PlayerStats], awards: list[Award]) -> str:
    ids = {a.id: a for a in awards}
    names = {s.player_id: s.name for s in stats}
    top_score = max((s.score for s in stats), default=0)
    leaders = [s for s in stats if s.score == top_score]

    if top_score == 0:
        key, values = "no_points", {}
    elif len(leaders) > 1:
        key, values = "tie", {"tied": _join([s.name for s in leaders]), "score": top_score}
    elif "robbed" in ids:
        key, values = "robbed", {"winner": leaders[0].name, "robbed": names[ids["robbed"].player_id]}
    elif "sharpest" in ids and ids["sharpest"].player_id == leaders[0].player_id:
        key, values = "dominant", {"winner": leaders[0].name, "score": top_score}
    else:
        key, values = "winner", {"winner": leaders[0].name, "score": top_score}
    return _choose(HEADLINES[key], match_id, 0).format(**values)


def _facts(s: PlayerStats) -> dict:
    worst = s.worst_move
    return {
        "name": s.name, "score": s.score, "rank": s.rank, "moves": s.moves,
        "accuracy_text": pct(s.accuracy) if s.accuracy is not None else None,
        "blunders": s.blunders, "checkmates": s.checkmates,
        "bounty_captures": s.bounty_captures, "timeouts": s.timeouts,
        "think_seconds": f"{s.median_think_ms / 1000:.1f}" if s.median_think_ms is not None else None,
        "blunders_count": plural(s.blunders, "blunder"),
        "blunders_text": "once" if s.blunders == 1 else f"{s.blunders} times",
        "checkmates_count": plural(s.checkmates, "checkmate"),
        "bounty_count": plural(s.bounty_captures, "bounty capture"),
        "timeouts_count": plural(s.timeouts, "turn"),
        "timeouts_text": "once" if s.timeouts == 1 else f"{s.timeouts} times",
        "moves_count": plural(s.moves, "move"),
        "worst_san": worst.san if worst else None,
        "worst_move_number": worst.move_number if worst else None,
        "best_san": worst.best_san if worst else None,
    }


def _choose(options: list[str], match_id: str, player_id: int) -> str:
    digest = hashlib.sha256(f"{match_id}:{player_id}".encode()).digest()
    return options[digest[0] % len(options)]


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
