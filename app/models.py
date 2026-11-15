"""The webhook payload Desperado sends. Java sends camelCase; Python uses snake_case.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Java sends camelCase JSON; Python code uses snake_case."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class EventType(str, Enum):
    MOVE = "MOVE"
    TIMEOUT_PASS = "TIMEOUT_PASS"
    TIMEOUT_MATE = "TIMEOUT_MATE"


class GameResult(str, Enum):
    WHITE_WON = "WHITE_WON"
    BLACK_WON = "BLACK_WON"
    DRAW = "DRAW"
    UNFINISHED = "UNFINISHED"


class BoardEvent(CamelModel):
    type: EventType
    player_id: int
    fen_before: str
    uci: str | None
    think_ms: int
    captured: str | None
    points: int
    bounty: bool
    checkmate: bool
    at_epoch_ms: int


class GameRecord(CamelModel):
    board_id: int
    game_number: int
    white_id: int
    black_id: int
    result: GameResult
    events: list[BoardEvent]


class PlayerRecord(CamelModel):
    player_id: int
    name: str
    position: int
    bot: bool
    score: int


class MatchRecord(CamelModel):
    match_id: str
    room_code: str
    started_at_ms: int
    ended_at_ms: int
    players: list[PlayerRecord]
    games: list[GameRecord]


class WebhookPayload(CamelModel):
    event_id: str
    type: str
    match: MatchRecord
