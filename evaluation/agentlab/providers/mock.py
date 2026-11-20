from __future__ import annotations

import json
from typing import Any

from .base import ProviderResult


class MockProvider:
    def __init__(self, model: str = "deterministic-mock-v1"):
        self.model = model
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]]) -> ProviderResult:
        self.calls += 1
        sequence = [
            ("rotate_right", {"degrees": 22.5}),
            ("move_forward", {"distance_m": 0.25}),
            ("rotate_left", {"degrees": 7.5}),
            ("done", {}),
        ]
        verb, parameters = sequence[min(self.calls - 1, len(sequence) - 1)]
        payload = {
            "decision_summary": f"Mock 第 {self.calls} 步，用于验证完整数据流。",
            "current_goal": "完成演示子任务",
            "evidence_used": [],
            "uncertainty": 0.1,
            "memory_updates": [],
            "action": {"verb": verb, "target": None, "parameters": parameters},
        }
        text='\n'.join(part.get('text','') for message in messages
            for part in (message['content'] if isinstance(message.get('content'),list) else []) if part.get('type')=='text')
        if '在线视觉 BEV 已开启' in text:
            payload.update(place_update=None,visual_landmarks=[])
        if ('持续任务与失败恢复已开启' in text or '任务记忆与失败恢复已开' in text or 'task_support.current_plan' in text):
            payload['plan_update']={'goal':'完成演示子任务','expected_observable_change':'观察动作反馈','change_strategy_if':'动作失败'}
        if '局部导航辅助已开启' in text:
            payload['navigation_request']=None
            if 'front_rgb_navigation_v2' in text:
                payload['decision_type']='action'
        if 'explored_path_recall_v1' in text:
            payload.update(decision_type='action', return_request=None)
        if 'visible_gt_waypoint_v1' in text:
            payload.update(decision_type='action',gt_navigation_request=None)
        content = json.dumps(payload, ensure_ascii=False)
        return ProviderResult(
            content=content,
            raw={"id": f"mock-{self.calls}", "model": self.model, "choices": [{"message": {"content": content}}]},
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            latency_ms=1,
        )

    def test_connection(self) -> dict[str, Any]:
        return {"ok": True, "model": self.model, "detail": "local deterministic provider"}
