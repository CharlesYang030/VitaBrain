from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderOutputUnavailable(RuntimeError):
    """A provider resource limit prevented a complete output; no action is valid."""


@dataclass
class ProviderResult:
    content: str
    raw: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0


class Provider(Protocol):
    def complete(self, messages: list[dict[str, Any]]) -> ProviderResult: ...
    def test_connection(self) -> dict[str, Any]: ...
