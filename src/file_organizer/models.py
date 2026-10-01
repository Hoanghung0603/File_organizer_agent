from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FileInfo:
    path: str
    extension: str
    size: int
    modified_time: str
    sha256: str
    preview: str | None = None


@dataclass(frozen=True)
class Action:
    kind: str
    source: str | None
    destination: str | None
    reason: str
    confidence: float
    sha256: str | None = None


@dataclass(frozen=True)
class Result:
    kind: str
    source: str | None
    destination: str | None
    status: str
    message: str


@dataclass(frozen=True)
class AgentOutcome:
    actions: list[Action]
    summary: str
    trace: list[dict[str, object]] = field(default_factory=list)
    llm_calls: int = 0
