from __future__ import annotations

from typing import Any

import httpx

from agentlab.domain import AgentAction, InterfaceMode


class SceneGenHttpClient:
    """Versioned boundary to the future SceneGen Runtime Service; no parent-code imports."""

    def __init__(self, base_url: str, timeout_seconds: float = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.episode_id: str | None = None

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout_seconds, trust_env=False) as client:
            response = client.request(method, f"{self.base_url}{path}", **kwargs)
            response.raise_for_status()
            return response.json()

    def scenarios(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/scenarios").get("scenarios", [])

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/v1/interface")

    def reset(self, scenario_id: str, seed: int, mode: InterfaceMode,
              house_index: int | None = None,
              agent_mode: str | None = None) -> dict[str, Any]:
        result = self._request("POST", "/v1/episodes", json={
            "scenario_id": scenario_id, "seed": seed, "interface_mode": mode.value,
            "protocol_version": "1.0",
        })
        self.episode_id = result["episode_id"]
        return result["observation"]

    def step(self, action: AgentAction) -> dict[str, Any]:
        if not self.episode_id:
            raise RuntimeError("reset must be called before step")
        return self._request("POST", f"/v1/episodes/{self.episode_id}/step", json={"action": action.to_dict()})

    def apply_handoff(self, subtask_id: str) -> dict[str, Any]:
        if not self.episode_id:
            raise RuntimeError("reset must be called before handoff")
        return self._request("POST", f"/v1/episodes/{self.episode_id}/handoff", json={"subtask_id": subtask_id})

