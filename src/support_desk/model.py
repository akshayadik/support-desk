from dataclasses import dataclass


@dataclass(frozen=True)
class ModelTurn:
    tool_name: str | None
    arguments: dict
    text: str
    done: bool
