from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class InterfaceMode(StrEnum):
    E2E = "e2e_visual_grounding"
    ASSISTED = "affordance_assisted"


class RunMode(StrEnum):
    STRICT = "strict"
    ADAPTIVE = "adaptive_probe"


class Visibility(StrEnum):
    AGENT_VISIBLE = "agent_visible"
    AGENT_OUTPUT = "agent_output"
    SERVER_PRIVATE = "server_private"
    EVALUATOR_PRIVATE = "evaluator_private"
    PRIVILEGED_ASSIST = "privileged_assist"


class Stage(StrEnum):
    OBSERVED = "OBSERVED"
    CONTEXT_BUILT = "CONTEXT_BUILT"
    API_REQUESTED = "API_REQUESTED"
    MODEL_RESPONDED = "MODEL_RESPONDED"
    CONTROLLER_DECIDED = "CONTROLLER_DECIDED"
    NAVIGATION_COMPLETED = "NAVIGATION_COMPLETED"
    GT_NAVIGATION_INPUT = "GT_NAVIGATION_INPUT"
    GT_NAVIGATION_PREPARED = "GT_NAVIGATION_PREPARED"
    GT_NAVIGATION_MODEL_OUTPUT = "GT_NAVIGATION_MODEL_OUTPUT"
    GT_NAVIGATION_OUTPUT_RETRY = "GT_NAVIGATION_OUTPUT_RETRY"
    GT_NAVIGATION_PLANNED = "GT_NAVIGATION_PLANNED"
    PATH_RECALL_CHECKED = "PATH_RECALL_CHECKED"
    PATH_RECALL_COMPLETED = "PATH_RECALL_COMPLETED"
    PROVIDER_TRANSPORT_COMPLETED = "PROVIDER_TRANSPORT_COMPLETED"
    PROVIDER_HTTP_RETRY = "PROVIDER_HTTP_RETRY"
    MODEL_OUTPUT_REPAIRED = "MODEL_OUTPUT_REPAIRED"
    MODEL_OUTPUT_RETRY = "MODEL_OUTPUT_RETRY"
    RESPONSE_PARSED = "RESPONSE_PARSED"
    ACTION_VALIDATED = "ACTION_VALIDATED"
    TARGET_RESOLVED = "TARGET_RESOLVED"
    ACTION_EXECUTED = "ACTION_EXECUTED"
    WORLD_STATE_UPDATED = "WORLD_STATE_UPDATED"
    MEMORY_UPDATED = "MEMORY_UPDATED"
    CAPABILITY_SCORED = "CAPABILITY_SCORED"
    ASSIST_TRIGGERED = "ASSIST_TRIGGERED"
    HANDOFF_APPLIED = "HANDOFF_APPLIED"
    HANDOFF_SKIPPED = "HANDOFF_SKIPPED"
    RUN_STATE = "RUN_STATE"


CAPABILITIES: tuple[dict[str, str], ...] = (
    {"id": "D1.1", "dimension": "视觉感知", "name": "目标发现"},
    {"id": "D1.2", "dimension": "视觉感知", "name": "实例指代"},
    {"id": "D1.3", "dimension": "视觉感知", "name": "状态识别"},
    {"id": "D2.1", "dimension": "空间认知", "name": "相对方位"},
    {"id": "D2.2", "dimension": "空间认知", "name": "可达性判断"},
    {"id": "D2.3", "dimension": "空间认知", "name": "空间记忆"},
    {"id": "D3.1", "dimension": "探索导航", "name": "主动探索"},
    {"id": "D3.2", "dimension": "探索导航", "name": "路径规划"},
    {"id": "D3.3", "dimension": "探索导航", "name": "重定位"},
    {"id": "D4.1", "dimension": "交互执行", "name": "动作选择"},
    {"id": "D4.2", "dimension": "交互执行", "name": "目标操作"},
    {"id": "D4.3", "dimension": "交互执行", "name": "执行恢复"},
    {"id": "D5.1", "dimension": "时序推理", "name": "线索整合"},
    {"id": "D5.2", "dimension": "时序推理", "name": "因果推断"},
    {"id": "D5.3", "dimension": "时序推理", "name": "顺序规划"},
    {"id": "D6.1", "dimension": "长程管理", "name": "目标分解"},
    {"id": "D6.2", "dimension": "长程管理", "name": "跨阶段记忆"},
    {"id": "D6.3", "dimension": "长程管理", "name": "全局纠错"},
)

ALLOWED_VERBS = {
    "move_forward", "move_backward", "rotate_left", "rotate_right", "look_up",
    "look_down", "pickup", "put", "open", "close", "toggle_on", "toggle_off",
    "push", "pull", "use", "wait", "done",
    # Escape-room / information verbs (STB_201/202 development bridge).
    "read", "input_code", "unlock", "screen_on", "screen_off",
    # Nested / second-level mechanisms (screens cycling, phone callback,
    # NPC hint queries + answering, releasing the held object).
    "screen_cycle", "answer_call", "ask_npc", "answer_npc", "drop",
    "fill_water", "handoff_npc", "use_elevator", "use_stairs",
}

TARGET_REQUIRED_VERBS = {
    "pickup", "put", "open", "close", "toggle_on", "toggle_off", "push",
    "pull", "use", "read", "input_code", "unlock", "screen_on", "screen_off",
    "screen_cycle", "answer_call", "ask_npc", "answer_npc", "drop", "fill_water", "handoff_npc",
}


@dataclass(frozen=True)
class TargetRef:
    kind: str
    observation_id: str
    x: float | None = None
    y: float | None = None
    handle: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TargetRef":
        if data.get('view_id','front') != 'front':
            raise ValueError('object targets must use the current front view; turn toward a side-view object first')
        target = cls(
            kind=str(data.get("kind", "")),
            observation_id=str(data.get("observation_id", "")),
            x=data.get("x"), y=data.get("y"), handle=data.get("handle"),
        )
        if not target.observation_id:
            raise ValueError("target.observation_id is required")
        if target.kind == "pixel":
            if any(isinstance(v, bool) or not isinstance(v, (int, float))
                   for v in (target.x, target.y)):
                raise ValueError("pixel target requires numeric x and y")
            if not (0 <= target.x <= 1 and 0 <= target.y <= 1):
                raise ValueError("pixel x and y must be normalized to [0, 1]")
        elif target.kind == "handle":
            if not isinstance(target.handle, str) or not target.handle:
                raise ValueError("handle target requires a non-empty handle")
        else:
            raise ValueError("target.kind must be pixel or handle")
        return target


@dataclass(frozen=True)
class AgentAction:
    verb: str
    target: TargetRef | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentAction":
        verb = str(data.get("verb", ""))
        if verb not in ALLOWED_VERBS:
            raise ValueError(f"unsupported verb: {verb!r}")
        target_data = data.get("target")
        target = TargetRef.from_dict(target_data) if isinstance(target_data, dict) else None
        if verb in TARGET_REQUIRED_VERBS and not target:
            raise ValueError(f"{verb} requires a target")
        params = data.get("parameters") or {}
        if not isinstance(params, dict):
            raise ValueError("parameters must be an object")
        cls._validate_parameters(verb, params)
        return cls(verb=verb, target=target, parameters=params)

    @staticmethod
    def _validate_parameters(verb: str, params: dict[str, Any]) -> None:
        if verb in {"move_forward", "move_backward"}:
            value = params.get("distance_m")
            if not isinstance(value, (int, float)):
                raise ValueError(f"{verb} requires numeric parameters.distance_m")
            if not 0.05 <= float(value) <= 5.0:
                raise ValueError("distance_m must be in [0.05, 5.0]")
            if set(params) - {"distance_m"}:
                raise ValueError("movement only accepts distance_m")
        elif verb in {"rotate_left", "rotate_right"}:
            value = params.get("degrees")
            if not isinstance(value, (int, float)):
                raise ValueError(f"{verb} requires numeric parameters.degrees")
            if not 1 <= float(value) <= 90:
                raise ValueError("rotation degrees must be in [1, 90]")
            if set(params) - {"degrees"}:
                raise ValueError("rotation only accepts degrees")
        elif verb in {"look_up", "look_down"}:
            value = params.get("degrees")
            if not isinstance(value, (int, float)):
                raise ValueError(f"{verb} requires numeric parameters.degrees")
            if not 1 <= float(value) <= 30:
                raise ValueError("look degrees must be in [1, 30]")
            if set(params) - {"degrees"}:
                raise ValueError("look only accepts degrees")
        elif verb in {"use_elevator", "use_stairs"}:
            floor = params.get("floor")
            if isinstance(floor, bool) or not isinstance(floor, int) or not 1 <= floor <= 8:
                raise ValueError("floor must be an integer from 1 to 8 (1 = ground floor)")
            if set(params) != {"floor"}:
                raise ValueError("floor transition only accepts floor")
        elif verb in {"fill_water", "handoff_npc"}:
            if params:
                raise ValueError(f"{verb} accepts no parameters")
        elif verb == "input_code":
            code = params.get("code")
            if not isinstance(code, str) or not code.strip():
                raise ValueError("input_code requires non-empty string parameters.code")
            if set(params) - {"code"}:
                raise ValueError("input_code only accepts code")
        elif verb == "answer_call":
            text = params.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("answer_call requires non-empty string parameters.text")
            if set(params) - {"text"}:
                raise ValueError("answer_call only accepts text")
        elif verb == "ask_npc":
            question = params.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError("ask_npc requires non-empty string parameters.question")
            if set(params) - {"question"}:
                raise ValueError("ask_npc only accepts question")
        elif verb == "answer_npc":
            text = params.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("answer_npc requires non-empty string parameters.text")
            if set(params) - {"text"}:
                raise ValueError("answer_npc only accepts text")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_action_for_interface(action: AgentAction, mode: InterfaceMode, observation_id: str) -> None:
    if action.target is None:
        return
    expected = "pixel" if mode == InterfaceMode.E2E else "handle"
    if action.target.kind != expected:
        raise ValueError(f"{mode.value} requires {expected} targets")
    if action.target.observation_id != observation_id:
        raise ValueError("stale or unknown observation_id")


def public_observation(raw: dict[str, Any], mode: InterfaceMode) -> dict[str, Any]:
    """Return the only observation object permitted to enter model context."""
    allowed = {
        "observation_id", "step", "rgb", "task", "public_feedback", "audio",
        "health", "remaining_time_seconds", "remaining_actions",
        "episode_done", "sim_time_seconds", "safety",
    }
    result = {key: raw[key] for key in allowed if key in raw}
    if raw.get('current_views') is not None:
        from agentlab.agent.views import public_views
        result['current_views']=public_views(raw['current_views'])
        front=result['current_views']['views'][1]
        result['rgb']={k:front[k] for k in ('data_url','width','height')}
    if raw.get('front_depth') is not None:
        from agentlab.agent.depth import public_depth
        result['front_depth'] = public_depth(raw['front_depth'],
            (result.get('current_views') or {}).get('views', [None,None])[1])
    if 'available_actions' in raw:
        result['available_actions'] = raw['available_actions']
    ego = raw.get('ego_state')
    if isinstance(ego, dict):
        result['ego_state'] = {k: ego[k] for k in ('heading_relative_spawn_deg', 'camera_pitch_deg',
            'cumulative_translation_m', 'cumulative_rotation_deg', 'collision_count')
            if type(ego.get(k)) in (int, float)}
        position = ego.get('position_relative_spawn_m')
        if isinstance(position, dict):
            result['ego_state']['position_relative_spawn_m'] = {k: position[k] for k in ('x', 'z')
                if type(position.get(k)) in (int, float)}
        result['ego_state']['held'] = bool(ego.get('held'))
    if mode == InterfaceMode.ASSISTED:
        result["interactables"] = []
        for item in raw.get("interactables", []):
            if item.get("currently_operable") is not True and not (
                    item.get('currently_operable') is False and item.get('label') == '门'
                    and isinstance(item.get('bbox'), list) and len(item['bbox']) == 4):
                continue
            result["interactables"].append({
                key: item[key]
                for key in ("handle", "label", "bbox", "allowed_actions", "currently_operable")
                if key in item
            })
    return result
