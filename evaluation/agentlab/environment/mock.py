from __future__ import annotations

import hashlib
from typing import Any

from agentlab.domain import AgentAction, InterfaceMode


class MockEnvironmentClient:
    """Deterministic contract fixture. It contains private truth to test non-leakage."""

    def __init__(self):
        self.current_views_enabled = False
        self.episode_id = ""
        self.step_count = 0
        self.mode = InterfaceMode.E2E
        self.done = False
        self.focused_subtask = "mock_find_exit"
        self.translation_m = 0.0
        self.rotation_degrees = 0.0
        self.door_open = False

    def scenarios(self) -> list[dict[str, Any]]:
        return [{
            "scenario_id": "mock_long_horizon_001",
            "title": "透明数据流演示",
            "channel": "development",
            "digest": hashlib.sha256(b"mock_long_horizon_001:v1").hexdigest(),
            "supported_verbs": ["move_forward", "move_backward", "rotate_left", "rotate_right", "look_up", "look_down", "open", "wait", "done"],
        }]

    def capabilities(self) -> dict[str, Any]:
        return {
            "protocol_version": "1.0", "target_modes": ["pixel", "handle"], "handoff": True,
            "motion": {
                "move_distance_m": {"min": 0.05, "max": 5.0},
                "rotate_degrees": {"min": 1, "max": 90},
                "look_degrees": {"min": 1, "max": 30},
            },
            "assisted_interactables": "currently_operable_only",
            "current_views": "left_front_right_v1",
        }

    def reset(self, scenario_id: str, seed: int, mode: InterfaceMode,
              house_index: int | None = None,
              agent_mode: str | None = None) -> dict[str, Any]:
        if scenario_id != "mock_long_horizon_001":
            raise ValueError("unknown mock scenario")
        self.step_count = 0
        self.done = False
        self.mode = mode
        self.episode_id = f"mock-{seed}-{mode.value}"
        self.focused_subtask = "mock_find_exit"
        self.translation_m = 0.0
        self.rotation_degrees = 0.0
        self.door_open = False
        return self._observation("Episode started")

    def _observation(self, feedback: str) -> dict[str, Any]:
        obs_id = f"obs_{self.step_count:04d}"
        observation: dict[str, Any] = {
            "observation_id": obs_id,
            "step": self.step_count,
            "rgb": {"artifact_hint": f"mock-frame-{self.step_count}", "width": 640, "height": 360},
            "task": "打开当前交互范围内的门。",
            "public_feedback": feedback,
            "health": 100,
            "remaining_time_seconds": max(0, 600 - self.step_count * 5),
            "remaining_actions": max(0, 100 - self.step_count),
            "focused_subtask": self.focused_subtask,
            "episode_done": self.done,
            # Only sanitized fields may survive in assisted public_observation.
            "interactables": ([{
                "handle": "h_demo_door",
                "label": "Door",
                "bbox": [0.72, 0.18, 0.92, 0.88],
                "allowed_actions": ["open"],
                "currently_operable": True,
                "objectId": "Door|private|123",
                "distance": 1.2345,
            }] if not self.door_open else []) + [{
                "handle": "h_far_cabinet",
                "label": "Cabinet",
                "bbox": [0.1, 0.1, 0.3, 0.5],
                "allowed_actions": ["open"],
                "currently_operable": False,
                "objectId": "Cabinet|private|999",
                "distance": 4.8,
            }],
            "_private": {
                "world_state": {"agent_position": [1.0, 0.0, 2.0], "door_open": self.door_open},
                "dag": {"mock_find_exit": ["mock_finish"]},
                "instance_masks": {"Door|private|123": "not-serialized-in-real-runtime"},
            },
        }
        if self.current_views_enabled:
            import numpy as np
            from PIL import Image,ImageDraw
            from agentlab.current_views import labelled_view,VERSION
            views=[]
            for i,key in enumerate(('left','front','right')):
                im=Image.new('RGB',(640,360),('#243842','#314839','#43362f')[i])
                ImageDraw.Draw(im).text((24,90),f'MOCK / {key} / step {self.step_count}',fill='white')
                views.append(labelled_view(np.asarray(im),key))
            observation['current_views']=dict(version=VERSION,views=views)
            observation['rgb']={k:views[1][k] for k in ('data_url','width','height')}
        return observation

    def step(self, action: AgentAction) -> dict[str, Any]:
        if self.done:
            raise RuntimeError("episode is already done")
        previous = self.step_count
        self.step_count += 1
        success = True
        feedback = f"Action {action.verb} executed"
        requested = dict(action.parameters)
        actual = dict(action.parameters)
        target_resolution: dict[str, Any] = {"status": "not_required", "private_object_id": None}
        target_resolved = False
        if action.target:
            if action.target.kind == "handle":
                target_resolved = action.target.handle == "h_demo_door" and not self.door_open
            elif action.target.kind == "pixel":
                target_resolved = (
                    action.target.x is not None and action.target.y is not None
                    and 0.72 <= action.target.x <= 0.92 and 0.18 <= action.target.y <= 0.88
                    and not self.door_open
                )
            target_resolution = {
                "status": "resolved" if target_resolved else "miss",
                "private_object_id": "Door|private|123" if target_resolved else None,
            }
        if action.verb in {"move_forward", "move_backward"}:
            self.translation_m += float(action.parameters["distance_m"])
        if action.verb in {"rotate_left", "rotate_right"}:
            self.rotation_degrees += float(action.parameters["degrees"])
        if action.verb == "open":
            if target_resolved:
                self.door_open = True
                self.done = True
                self.focused_subtask = None
                feedback = "Door opened; mock task completed"
            else:
                success = False
                feedback = "Target was not resolved to a currently operable door"
        elif action.verb == "done" and not self.door_open:
            success = False
            feedback = "Task is not complete: the door remains closed"
        if self.step_count >= 20 and not self.done:
            self.done = True
            self.focused_subtask = None
            success = False
            feedback = "Action budget exhausted"
        observation = self._observation(feedback)
        return {
            "observation": observation,
            "public_result": {
                "success": success, "message": feedback, "done": self.done,
                "requested": requested, "actual": actual, "collision": False,
            },
            "target_resolution": target_resolution,
            "world_state_diff": {
                "step": {"before": previous, "after": self.step_count},
                "door_open": self.door_open,
            },
            "capability_evidence": ([
                {"capability_id": "D4.1", "status": "passed_unassisted", "step": self.step_count}
            ] + ([
                {"capability_id": "D4.2", "status": "passed_unassisted", "step": self.step_count}
            ] if self.door_open else [])),
            "metrics": {
                "true_goal_completion": 1.0 if self.door_open else 0.0,
                "cumulative_translation_m": round(self.translation_m, 4),
                "cumulative_rotation_degrees": round(self.rotation_degrees, 4),
                "collision_count": 0,
            },
        }

    def apply_handoff(self, subtask_id: str) -> dict[str, Any]:
        if subtask_id != self.focused_subtask:
            raise ValueError("handoff subtask does not match current focus")
        self.focused_subtask = "mock_finish"
        return {
            "assist_id": f"assist_{self.step_count:04d}",
            "subtask_id": subtask_id,
            "assist_types": ["dependency_release", "information_grant"],
            "capabilities": ["D3.1"],
            "information_grants": [{"kind": "handoff", "content": "出口位于当前视野右侧。"}],
            "world_state_diff": {},
            "postconditions_verified": True,
            "handoff_context": "上一探索节点已由评测器辅助释放。出口位于当前视野右侧，请继续后续任务。",
            "next_subtask": "mock_finish",
            "taint_lineage": ["D3.1", "D4.2"],
        }
