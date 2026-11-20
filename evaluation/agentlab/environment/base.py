from __future__ import annotations

from typing import Any, Protocol

from agentlab.domain import AgentAction, InterfaceMode


class EnvironmentUnavailable(RuntimeError):
    """Infrastructure failure, not an ordinary unsuccessful game action."""


class TargetGroundingError(EnvironmentUnavailable):
    """An untrusted conversion must stop before charging an agent action."""

    def __init__(self, resolution: dict[str, Any]):
        self.resolution = resolution
        super().__init__("E2E 目标转换链异常，已暂停且未执行动作："
                         + str(resolution.get("status", "grounding_error")))


class EnvironmentClient(Protocol):
    def scenarios(self) -> list[dict[str, Any]]: ...
    def reset(self, scenario_id: str, seed: int, mode: InterfaceMode,
              house_index: int | None = None,
              agent_mode: str | None = None) -> dict[str, Any]: ...
    def step(self, action: AgentAction) -> dict[str, Any]: ...
    def return_to_visited(self, target: dict[str, Any]) -> dict[str, Any]: ...
    def apply_handoff(self, subtask_id: str) -> dict[str, Any]: ...
    def capabilities(self) -> dict[str, Any]: ...
