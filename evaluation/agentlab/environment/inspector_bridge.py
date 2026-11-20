"""Development bridge: run agent episodes against the live 8851 Inspector.

HTTP-only boundary — this module never imports scenegen code.  It converts the
researcher-facing Inspector session into the strict Agent-Lab observation
contract:

- the model sees the first-person JPEG, initial task, typed interaction
  receipts and proprioception; assisted mode additionally provides currently
  visible operable anonymous handles;
- raw objectIds, lock codes, note truths, distances and world coordinates stay
  on the server side of this adapter and are surfaced only through
  server_private audit events;
- E2E pixel targets resolve through 8851's frame-bound private mask endpoint;
  a stale frame, background pixel or ambiguous hit is a hard failure, never an
  auto-snap.

The Inspector executes some manipulations with an audited IK bypass /
forceAction debug profile, so every run through this backend is permanently
`formal_sr_eligible=false` (development observation, not a formal benchmark
result).
"""
from __future__ import annotations
from agentlab.runtime_state import health_report_value

import hashlib
import base64
import json
import re
import time
import uuid
from typing import Any, Callable

import httpx

from agentlab.domain import ALLOWED_VERBS, AgentAction, InterfaceMode
from agentlab.environment.base import EnvironmentUnavailable, TargetGroundingError
from agentlab.information import VERSION as INFORMATION_VERSION, FEEDBACK_VERSION, LABELS, render_feedback
from agentlab.security import SPAWNED_OBJECT_ID_PATTERN

PROFILE = {
    "backend": "development_inspector",
    "watermark": "DEVELOPMENT-INSPECTOR",
    "formal_sr_eligible": False,
    "reason": (
        "8851 Inspector runs a debug profile (audited IK-bypass pickup, "
        "forceAction open); trajectories are development observations and are "
        "never formal SR."
    ),
}

GROUNDING_VERSION = "exact_pixel_identity_v2"

# Provisional goal-kind -> capability evidence map (must be re-aligned with
# scenegen/contracts/taxonomy.py before any formal capability report).
_GOAL_CAPABILITIES = {
    "door": ("D4.1", "D4.2"),
    "unlock": ("D4.1", "D5.1", "D5.3"),
    "escape_out": ("D3.2",),
    "screen_channel": ("D1.3",),
    "pick_place": ("D4.2",),
}

_RAW_ID_PATTERN = re.compile(r"\b[A-Za-z][\w.]*\|[^\s，。,;:）)\"']+")

_NAVIGATION_BOUNDS_ERROR = '原生导航可达区域查询发生场景边界异常；已保留待执行决策，未选点、未移动。这是环境工程故障，需要修复导航服务后重试。'


class SceneGenInspectorBridgeClient:
    """EnvironmentClient over the 8851 Inspector HTTP API (development only)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8851",
        *,
        timeout_seconds: float = 120,
        boot_timeout_seconds: float = 2400,
        poll_interval_seconds: float = 3.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.gt_navigation_enabled = False
        self.path_recall_enabled = False
        self._recall_odometry = {}
        self.fog_gt_enabled = False
        self.fog_gt_first_person = False
        self.depth_image_enabled = False
        self.current_views_enabled = False
        self._front_annotation_rect = None
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.boot_timeout_seconds = boot_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._transport = transport
        self._sleep = sleep
        self.mode = InterfaceMode.E2E
        self.scenario_id: str | None = None
        self.seed: int | None = None
        self.bound_digest: str | None = None
        self._episode_salt = ""
        self._obs_seq = 0
        self._current_obs_id: str | None = None
        self._frame_token: str | None = None
        self._frame_size: tuple[int, int] | None = None
        self._frame_rgb_sha256: str | None = None
        self._motion_protocol: str | None = None
        self._entities: dict[str, dict[str, Any]] = {}   # handle -> entry
        self._label_by_raw: dict[str, str] = {}
        self._last_statuses: dict[str, str] = {}
        self._forced_done = False
        self._assist_seq = 0
        self._collisions = 0
        self._safety_events: list[dict[str, Any]] = []
        self._last_health: float | None = None
        self._translation_m = 0.0
        self._rotation_degrees = 0.0
        # 航位推算（全部由公开动作结果累加,给模型的自我里程计）
        self._heading_deg = 0.0
        self._pitch_deg = 0.0
        self._dr_x = 0.0
        self._dr_z = 0.0
        self._panels_subtasks: list[dict[str, Any]] = []
        self._native_task_success: bool | None = None
        self._boot = {"phase": "idle", "log": [], "scenario": None}

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if method == 'POST' and path in ('/api/inspect/act', '/api/inspect/npc-query', '/api/inspect/path-recall'):
            decision = getattr(self, '_safety_decision', None)
            if decision:
                kwargs['json'] = {**kwargs.get('json', {}), '_safety_decision': decision}
        guarded = (method == "POST"
                   and path in ("/api/inspect/act", "/api/inspect/npc-query"))
        if guarded:
            if not self._frame_token:
                raise TargetGroundingError({"status": "no_frame_token", "failure_class": "interface_error"})
            kwargs["json"] = {**kwargs.get("json", {}), "_grounding_token": self._frame_token}
        # Native magnetic pickup/place may finish several minutes after its
        # request. Keep a single connection open; never resend the operation.
        timeout = (httpx.Timeout(self.timeout_seconds, read=max(600, self.timeout_seconds))
                   if guarded else self.timeout_seconds)
        with httpx.Client(timeout=timeout, trust_env=False,
                          transport=self._transport) as client:
            checkpoint_hook = getattr(self, '_checkpoint_before_dispatch', None)
            if method == 'POST' and checkpoint_hook:
                checkpoint_hook(path)
            response = client.request(method, f"{self.base_url}{path}", **kwargs)
        if guarded and response.status_code == 409:
            detail = response.json()
            if detail.get("failure_class") == "interface_error":
                raise TargetGroundingError({"status": detail.get("error"),
                    "failure_class": "interface_error", "stage": "before_execution",
                    "execution_started": False, "expected_frame_token": self._frame_token,
                    "backend_operation": {k: v for k, v in kwargs["json"].items()
                                          if k != "_grounding_token"}})
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, path, **kwargs)
        if path in ('/api/inspect/gt-navigation/prepare','/api/inspect/gt-navigation/plan') and response.status_code == 503:
            try:
                data = response.json()
            except ValueError:
                data = {}
            if (data.get('feedback_code') == 'gt_navigation_reachability_bounds_error'
                    and data.get('execution_started') is False):
                raise EnvironmentUnavailable(_NAVIGATION_BOUNDS_ERROR)
            if (data.get('feedback_code') == 'gt_navigation_native_start_query_unavailable'
                    and data.get('execution_started') is False):
                raise EnvironmentUnavailable('原生起步碰撞查询服务异常；未选点、未移动。需要修复环境接口后重试。')
            if (data.get('feedback_code') == 'gt_navigation_native_route_query_unavailable'
                    and data.get('execution_started') is False):
                raise EnvironmentUnavailable('原生导航路线核验服务异常；导航请求已保留，未移动。需要修复环境接口后重试。')
        response.raise_for_status()
        return response.json()

    def begin_safety_decision(self, key: str, verb: str):
        decision = dict(id=key, verb=verb)
        response = self._request('POST', '/api/inspect/safety-decision', json={
            **decision, '_grounding_token': self._frame_token})
        if response.status_code == 409:
            detail = response.json()
            if (detail.get('error') == 'stale_observation_token'
                    and detail.get('execution_started') is False):
                raise TargetGroundingError(dict(status=detail['error'],
                    stage='safety_before_execution', execution_started=False,
                    failure_class='interface_error', backend_receipt=detail))
        response.raise_for_status()
        result = response.json()
        if not result.get('ok') or result.get('policy', {}).get('version') != 'decision_safety_v2':
            raise EnvironmentUnavailable('需要按决策计数的安全规则；未执行动作')
        self._safety_decision = decision

    def assert_ready(self) -> None:
        """Probe process/pipe health without advancing Unity or trusting RGB cache."""
        try:
            health = self._json("GET", "/api/inspect/runtime-health")
        except (httpx.HTTPError, ValueError) as error:
            raise EnvironmentUnavailable("无法验证 Unity 连接；已暂停，未请求模型或重置场景") from error
        if not health.get("ready"):
            raise EnvironmentUnavailable(
                "Unity 环境不可用：" + str(health.get("reason") or "连接已断开")
                + "；网页画面可能是缓存，需要恢复环境后继续")

    # ----------------------------------------------------------------- boot
    def boot_status(self) -> dict[str, Any]:
        return {
            "phase": self._boot["phase"],
            "scenario": self._boot["scenario"],
            "log": list(self._boot["log"])[-30:],
        }

    def _log_boot(self, text: str) -> None:
        self._boot["log"].append(text)
        del self._boot["log"][:-200]

    def scenarios(self) -> list[dict[str, Any]]:
        payload = self._json("GET", "/api/v3/scenario-packages")
        rows = payload.get("packages") or payload.get("scenarios") or []
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            scenario_id = str(row.get("scenario_id") or row.get("id") or "")
            if not scenario_id:
                continue
            result.append({
                "scenario_id": scenario_id,
                "title": str(row.get("title_zh") or row.get("title")
                             or scenario_id),
                "channel": "development",
                "readiness": str(row.get("content_status") or "unknown"),
                "digest": "dev-unbound:" + hashlib.sha256(
                    scenario_id.encode()).hexdigest()[:16],
                "supported_verbs": sorted([
                    "move_forward", "move_backward", "rotate_left",
                    "rotate_right", "look_up", "look_down", "open", "close",
                    "pickup", "put", "push", "read", "input_code", "unlock",
                    "screen_on", "screen_off", "screen_cycle", "answer_call",
                    "ask_npc", "answer_npc", "drop", "wait", "done",
                    "fill_water", "handoff_npc", "use_elevator", "use_stairs",
                ]),
            })
        return result

    def capabilities(self) -> dict[str, Any]:
        return {
            "protocol_version": "1.0",
            "backend": "development_inspector",
            "formal_sr_eligible": False,
            "target_modes": ["pixel", "handle"],
            "handoff": True,
            "motion": {
                "version": "straight_motion_v2_5m",
                "move_distance_m": {"min": 0.05, "max": 5.0},
                "rotate_degrees": {"min": 1, "max": 90},
                "look_degrees": {"min": 1, "max": 30},
            },
            "assisted_interactables": "visible_doors_with_operability_and_nearby_objects",
            "current_views": "left_front_right_v1",
        }

    def reset(self, scenario_id: str, seed: int, mode: InterfaceMode,
              house_index: int | None = None,
              agent_mode: str | None = None) -> dict[str, Any]:
        self.mode = mode
        self._safety_events = []
        self._last_health = None
        self._episode_salt = uuid.uuid4().hex
        self._obs_seq = 0
        self._forced_done = False
        self._assist_seq = 0
        self._collisions = 0
        self._translation_m = 0.0
        self._rotation_degrees = 0.0
        # 航位推算（全部由公开动作结果累加,给模型的自我里程计）
        self._heading_deg = 0.0
        self._pitch_deg = 0.0
        self._dr_x = 0.0
        self._dr_z = 0.0
        self._last_statuses = {}
        self._entities = {}
        self._label_by_raw = {}
        self._motion_protocol = None
        self._boot = {"phase": "starting", "log": [], "scenario": scenario_id}

        status = self._wait_not_busy("waiting for the inspector to be free")
        same_binding = (
            bool(status.get("ready"))
            and status.get("scenario") == scenario_id
            and self.scenario_id == scenario_id
            and self.seed == seed
            and (house_index is None
                 or status.get("house_index") == house_index)
            and (agent_mode is None
                 or status.get("agent_mode") == agent_mode)
        )
        if same_binding:
            self._boot["phase"] = "resetting_episode"
            self._log_boot("scenario already bound; starting a fresh episode")
            try:
                payload = self._json("POST", "/api/inspect/reset", json={})
                if payload.get("error"):
                    raise RuntimeError(
                        f"inspector reset failed: {payload['error']}")
                status = self._json("GET", "/api/inspect/status")
            except Exception as error:
                # A dead Unity session 500s every in-episode reset; a full
                # start lets 8851 probe + rebuild the controller instead of
                # surfacing "boot failed" to the user.
                self._log_boot(
                    f"episode reset failed ({type(error).__name__}); "
                    "falling back to a full start")
                same_binding = False
        if not same_binding:
            self._boot["phase"] = "binding_scene"
            self._log_boot(f"binding {scenario_id} (seed={seed}) on 8851…")
            start_body: dict[str, Any] = {
                "scenario": scenario_id, "seed": seed, "rebind": False,
            }
            if house_index is not None:
                start_body["house_index"] = int(house_index)
            if agent_mode:
                start_body["agent_mode"] = agent_mode
            payload = self._json("POST", "/api/inspect/start", json=start_body)
            if payload.get("error"):
                raise RuntimeError(f"inspector start rejected: {payload['error']}")
            status = self._wait_ready(scenario_id)
        self.scenario_id = scenario_id
        self.seed = seed
        bound_house = status.get("house_index")
        if house_index is not None and bound_house != house_index:
            self._log_boot(
                f"⚠ 请求的 house {house_index} 未通过物理预检（容器无法正常"
                f"开合等），已回退绑定 house {bound_house}")
        self.bound_digest = (
            f"dev-inspector:{scenario_id}:house{bound_house}:seed{seed}")
        self._boot["phase"] = "ready"
        self._log_boot(f"scene ready (house_index={bound_house})")
        return self._observation("Episode started", status=status)

    def _poll_status(self, deadline: float) -> dict[str, Any]:
        """Status poll that survives the Unity-bind CPU freeze.

        While CreateHouse saturates the host, 8851 can stop accepting TCP for
        minutes at a time.  A transport error therefore means "still busy",
        not "bind failed" — keep polling until the overall boot deadline.
        """
        while True:
            try:
                return self._json("GET", "/api/inspect/status")
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"inspector unreachable during bind: {error}") from error
                self._boot["phase"] = "binding:inspector_busy_unreachable"
                self._log_boot(f"status poll retry: {type(error).__name__}")
                self._sleep(self.poll_interval_seconds)

    def _wait_not_busy(self, why: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.boot_timeout_seconds
        while True:
            status = self._poll_status(deadline)
            if not status.get("busy"):
                return status
            self._boot["phase"] = f"busy:{status['busy']}"
            self._log_boot(f"{why}: {status['busy']}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"inspector stayed busy: {status['busy']}")
            self._sleep(self.poll_interval_seconds)

    def _wait_ready(self, scenario_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.boot_timeout_seconds
        seen = 0
        while True:
            status = self._poll_status(deadline)
            for row in (status.get("log") or [])[seen:]:
                text = row.get("text") if isinstance(row, dict) else str(row)
                if text:
                    self._log_boot(text)
            seen = len(status.get("log") or [])
            if status.get("busy"):
                self._boot["phase"] = f"binding:{status['busy']}"
            elif status.get("ready") and status.get("scenario") == scenario_id:
                return status
            else:
                errors = [row.get("text") for row in (status.get("log") or [])
                          if isinstance(row, dict) and row.get("kind") == "error"]
                raise RuntimeError(
                    "inspector bind finished without a ready scene: "
                    + (errors[-1] if errors else "unknown error"))
            if time.monotonic() > deadline:
                raise TimeoutError("inspector scene bind timed out")
            self._sleep(self.poll_interval_seconds)

    # ---------------------------------------------------------- observation
    def _handle_for(self, raw_key: str) -> str:
        digest = hashlib.sha256(
            f"{self._episode_salt}:{raw_key}".encode()).hexdigest()
        return f"h_{digest[:8]}"

    def _bbox_for(self, raw_id: str | None,
                  detections: dict[str, list[float]]) -> list[float] | None:
        if not raw_id:
            return None
        box = detections.get(raw_id)
        if not box:
            return None
        return [round(float(value), 4) for value in box]

    def _collect_entities(
        self,
        panels: dict[str, Any],
        manual: dict[str, Any],
        detections: dict[str, list[float]],
    ) -> dict[str, dict[str, Any]]:
        """Fail-closed operable set: only entities the runtime confirms now."""
        entities: dict[str, dict[str, Any]] = {}

        def add(raw_key: str, *, raw_id: str | None, label: str,
                verbs: list[str], kind: str, op_id: str, visible: bool,
                extra: dict[str, Any] | None = None) -> None:
            if not verbs:
                return
            handle = self._handle_for(raw_key)
            entry = entities.setdefault(handle, {
                "raw_key": raw_key, "raw_id": raw_id, "label": label,
                "verbs": [], "kind": kind, "op_id": op_id,
                # Only exact rendered pixels establish visual availability.
                "visible": bool(visible),
                "bbox": self._bbox_for(raw_id, detections),
                "extra": dict(extra or {}),
            })
            entry["visible"] = entry["visible"] or bool(visible)
            for verb in verbs:
                if verb not in entry["verbs"]:
                    entry["verbs"].append(verb)
            if extra:
                entry["extra"].update(extra)

        locks_by_fixture_object: dict[str, dict[str, Any]] = {}
        for lock in panels.get("locks") or []:
            if lock.get("fixture_object_id"):
                locks_by_fixture_object[str(lock["fixture_object_id"])] = lock

        def row_visible(row: dict[str, Any]) -> bool:
            return str(row.get('id')) in detections

        for row in manual.get("nearby_controls") or []:
            raw_id = str(row.get("id"))
            label = LABELS.get(str(row.get('type')), '物体')
            verbs: list[str] = []
            if row.get("openable"):
                verbs.append("close" if row.get("is_open") else "open")
            if row.get("toggleable"):
                verbs.append("toggle_off" if row.get("is_toggled")
                             else "toggle_on")
            lock = locks_by_fixture_object.get(raw_id)
            if (lock and lock.get("locked") and lock.get("discovered")):
                verbs.append("input_code" if lock.get("input_required")
                             else "unlock")
            add(f"object:{raw_id}", raw_id=raw_id, label=label, verbs=verbs,
                kind="control", op_id=raw_id, visible=row_visible(row),
                extra={"is_exit_door": bool(row.get("is_exit_door"))})
        for row in manual.get("visible_door_controls") or []:
            raw_id = str(row.get("id"))
            if raw_id not in detections or any(e.get("raw_id") == raw_id for e in entities.values()):
                continue
            add(f"object:{raw_id}", raw_id=raw_id, label='门',
                verbs=["close" if row.get("is_open") else "open"],
                kind="control", op_id=raw_id, visible=True,
                extra={"currently_operable": False})
        for row in manual.get("pickup_candidates") or []:
            raw_id = str(row.get("id"))
            add(f"object:{raw_id}", raw_id=raw_id,
                label=LABELS.get(str(row.get('type')), '物体'),
                verbs=["pickup"], kind="pickupable", op_id=raw_id,
                visible=row_visible(row))
        for row in manual.get("placement_targets") or []:
            if not row.get("can_place"):
                continue
            raw_id = str(row.get("id"))
            add(f"object:{raw_id}", raw_id=raw_id,
                label=LABELS.get(str(row.get('type')), '物体'),
                verbs=["put"], kind="receptacle", op_id=raw_id,
                visible=row_visible(row),
                extra={"target_kind": str(row.get("target_kind") or "surface")})
        for row in manual.get("pushable_objects") or []:
            raw_id = str(row.get("id"))
            add(f"object:{raw_id}", raw_id=raw_id,
                label=LABELS.get(str(row.get('type')), '物体'),
                verbs=["push"], kind="pushable", op_id=raw_id,
                visible=row_visible(row))
        for note in panels.get("notes") or []:
            if not note.get("agent_readable"):
                continue
            container_id = note.get("container_object_id")
            # 纸条随其(已打开的)容器一并暴露:容器在视野里,纸条才可读。
            note_visible = (
                str(container_id) in detections
                if container_id else False)
            add(f"note:{note['id']}",
                raw_id=str(container_id) if container_id else None,
                label='可读文字',
                verbs=["read"], kind="note", op_id=str(note["id"]),
                visible=note_visible)
        for screen in panels.get("screens") or []:
            if not screen.get("in_range"):
                continue
            state = str(screen.get("state") or "off")
            add(f"screen:{screen['id']}", raw_id=screen.get("visual_object_id"),
                label='屏幕',
                # 开机后可"切换频道/画面"（cycle）——视觉屏幕类线索的二级机关
                verbs=(["screen_on"] if state == "off"
                       else ["screen_off", "screen_cycle"]),
                kind="screen", op_id=str(screen["id"]),
                visible=screen.get('visual_object_id') in detections)

        # ---- 二级/隐藏机关 -----------------------------------------------
        # 手持物：在手里的东西永远可感知（fov_exempt），可原地放下。
        held = manual.get("held")
        if isinstance(held, dict) and held.get("id"):
            # Preserve the already public pickup identity, never an authored
            # object name or hidden semantic category. E2E still filters handles.
            held_key = f"object:{held['id']}"
            previous = self._entities.get(self._handle_for(held_key), {})
            held_label = previous.get('label') if previous.get('visible') else None
            entities.pop(self._handle_for(held_key), None)
            add(held_key, raw_id=str(held["id"]),
                label=held_label or '手持物',
                verbs=["drop"] + (["fill_water"] if held.get("can_fill_water") else []), kind="held", op_id=str(held["id"]),
                visible=True, extra={"fov_exempt": True})
        # A callback handle is visual and bound to one phone in both modes.
        if manual.get("has_callback") and manual.get("callback_ready"):
            phones = manual.get('callback_phones')
            if not isinstance(phones, list):
                raise EnvironmentUnavailable('观察缺少电话实例绑定，需要核对环境接口')
            for phone in phones:
                if phone.get('in_range') and phone.get('id'):
                    add('phone:' + str(phone['id']), raw_id=str(phone['id']),
                        label='手机', verbs=['answer_call'], kind='phone',
                        op_id=str(phone['id']), visible=str(phone['id']) in detections)
        # A role or author description is never a public NPC label.
        for npc in panels.get("npcs") or []:
            npc_id = str(npc.get("id") or "")
            if not npc_id or not npc.get("in_range") or npc.get("queryable") is False:
                continue
            # Conversational attempts are available independently of private goal types.
            verbs = ["ask_npc", "answer_npc"]
            if manual.get("held"):
                verbs.append("handoff_npc")
            add(f"npc:{npc_id}", raw_id=npc.get("visual_object_id"),
                label='NPC', verbs=verbs, kind="npc", op_id=npc_id,
                visible=npc.get('visual_object_id') in detections)
        return entities

    def _frame_private(self) -> dict[str, Any]:
        try:
            return self._json("GET", "/api/inspect/runtime-frame-private")
        except httpx.HTTPStatusError:
            return {"observation_token": None, "detections": [],
                    "width": None, "height": None}

    def _observation(self, feedback: str,
                     status: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            query=[]
            if self.gt_navigation_enabled:query.append("gt_navigation=visible_gt_waypoint_v2_triplet")
            if self.path_recall_enabled:query.append("path_recall=1")
            if self.fog_gt_enabled:
                query.extend(['fog_gt_geometry=1','fog_gt_bev=1'])
                if self.fog_gt_first_person:query.append('fog_gt_fpv=1')
            if self.current_views_enabled:query.append('current_views=left_front_right_v1')
            if self.depth_image_enabled:query.append('depth_image=front_metric_depth_v1')
            bundle = self._json("GET", "/api/inspect/agent-observation-private"
                + ('?'+'&'.join(query) if query else ''))
        except httpx.HTTPStatusError as error:
            # This message is surfaced to the operator, not a model observation.
            # Preserve the actual failure instead of mislabelling every 503 as
            # an old/missing API (camera restoration and BEV failures differ).
            try:
                problem = error.response.json()
            except ValueError:
                problem = {}
            code = str(problem.get('error') or 'observation_request_failed')
            detail = str(problem.get('detail') or '请检查环境加载状态及 8851 日志')
            raise EnvironmentUnavailable(
                f"原子观察失败（HTTP {error.response.status_code}，{code}）：{detail}") from error
        if self.path_recall_enabled:
            anchor=(bundle.get('fog_geometry') or {}).get('_recall_anchor') or {}
            if anchor.get('version')!='explored_path_recall_v1' or not anchor.get('key'):
                raise EnvironmentUnavailable('后端未提供可验证的历史站位；路径回溯尚未就绪')
            self._recall_odometry[anchor['key']]=dict(x=self._dr_x,z=self._dr_z,heading=self._heading_deg)
        status, manual, private_frame = bundle["status"], bundle["manual"], bundle["frame"]
        if (private_frame.get("grounding_version") != GROUNDING_VERSION
                or private_frame.get('information_version') != INFORMATION_VERSION):
            raise EnvironmentUnavailable("对象转换或信息边界协议版本不匹配")
        rgb = base64.b64decode((status.get("frames") or {}).get("fpv", ""), validate=True)
        if not rgb or hashlib.sha256(rgb).hexdigest() != private_frame.get("rgb_sha256"):
            raise EnvironmentUnavailable("图像与目标解析帧的指纹不一致")
        if self.fog_gt_enabled and (not bundle.get('fog_geometry') or
                bundle['fog_geometry'].get('frame_rgb_sha256') != private_frame['rgb_sha256']):
            raise EnvironmentUnavailable('GT map geometry and FPV must share the same atomic frame')
        self._frame_size = (int(private_frame["width"]), int(private_frame["height"]))
        self._frame_rgb_sha256 = private_frame["rgb_sha256"]
        self._motion_protocol = private_frame.get('motion_protocol')
        self._frame_identity_aliases = private_frame.get("identity_aliases", {})
        current_views=None
        self._front_annotation_rect=None
        if self.current_views_enabled:
            from agentlab.agent.views import public_views
            try:current_views=public_views(bundle.get('current_views'),verify=True)
            except ValueError as error:
                raise EnvironmentUnavailable('三视图缺失、方向或图像指纹不一致；已暂停，禁止退回单图') from error
            if (current_views['views'][1]['width'],current_views['views'][1]['height'])!=self._frame_size:
                raise EnvironmentUnavailable('三视图与对象解析图尺寸不一致')
            if self.fog_gt_enabled and self.fog_gt_first_person:
                hashes = {v['view_id']: v['sha256'] for v in current_views['views']}
                if bundle['fog_geometry'].get('view_image_sha256') != hashes:
                    raise EnvironmentUnavailable('探索地图与实际输入的三视图指纹不一致')
            self._front_annotation_rect=current_views['views'][1]['annotation_rect_px']
            self._gt_navigation_front_sha=current_views['views'][1]['sha256']
            self._gt_navigation_view_hashes={v['view_id']:v['sha256'] for v in current_views['views']}
        front_depth = None
        if self.depth_image_enabled:
            from agentlab.agent.depth import public_depth
            try:
                front_depth = public_depth(bundle.get('front_depth'),
                    current_views['views'][1] if current_views else None, verify=True)
            except (ValueError, KeyError, TypeError, OSError) as error:
                raise EnvironmentUnavailable('当前正视深度图缺失或不同帧；禁止静默退回 RGB') from error
        if not status.get("ready"):
            raise RuntimeError("inspector session lost its scene")
        panels = status.get("panels") or {}
        frames = status.get("frames") or {}
        if private_frame.get("camera_pitch_deg") is not None:
            import math
            pitch = float(private_frame["camera_pitch_deg"])
            if not math.isfinite(pitch) or abs(pitch) > 90:
                raise EnvironmentUnavailable("环境返回无效相机俯仰，已暂停核对")
            # A binding may start tilted; manipulation can also change the
            # view. Read the current angle instead of assuming a zero origin.
            self._pitch_deg = pitch
        detections = {
            str(row["object_id"]): row["bbox"]
            for row in private_frame.get("detections") or []
            if row.get("bbox")
        }
        for part, owners in self._frame_identity_aliases.items():
            if len(owners) == 1 and part in detections:
                box = detections[part]
                old = detections.get(owners[0], box)
                detections[owners[0]] = [min(old[0], box[0]), min(old[1], box[1]),
                                          max(old[2], box[2]), max(old[3], box[3])]
        manual = {**manual, "visible_door_controls": private_frame.get("visible_door_controls", [])}
        self._entities = self._collect_entities(panels, manual, detections)
        self._label_by_raw = {
            str(entry["raw_id"]): entry["label"]
            for entry in self._entities.values()
            # A note borrows its container's geometry for visibility and hit
            # testing. It must not rename the physical fridge/box in collision
            # feedback: there is no separate paper collider at that raw ID.
            if entry.get("raw_id") and entry.get("kind") != "note"
        }
        self._frame_token = private_frame.get("observation_token")
        obs_id = f"obs_{self._obs_seq:04d}"
        self._obs_seq += 1
        self._current_obs_id = obs_id

        from agentlab.task_progress import from_panels, summary
        subtasks, progress_source = from_panels(self.scenario_id, panels)
        progress_summary = summary(subtasks, progress_source)
        progress_summary["native_task_success"] = panels.get("success") if isinstance(panels.get("success"), bool) else None
        self._panels_subtasks = subtasks
        self._native_task_success = panels.get("success") if isinstance(panels.get("success"), bool) else None
        self._last_statuses = {
            str(row.get("id")): str(row.get("status"))
            for row in subtasks if not row.get("masked")
        }
        focused = next((
            str(row["id"]) for row in subtasks
            if row.get("mandatory") and not row.get("prohibited")
            and row.get("status") == "ready" and not row.get("masked")), None)
        episode_done = bool(panels.get("success") or panels.get("failed")
                            or self._forced_done)

        observation: dict[str, Any] = {
            "observation_id": obs_id,
            "step": int(panels.get("steps") or 0),
            "rgb": ({
                "data_url": "data:image/jpeg;base64," + frames["fpv"],
                "width": private_frame.get("width"),
                "height": private_frame.get("height"),
            } if frames.get("fpv") else {"artifact_hint": "no-frame"}),
            "task": str(panels.get("instruction") or ""),
            **({'current_views':current_views} if current_views else {}),
            **({'front_depth':front_depth} if front_depth else {}),
            "public_feedback": feedback,
            "health": health_report_value((panels.get("safety") or {}).get("health")),
            # Public, episode-local safety channel. It contains only current
            # health and already-triggered causes/advice; no hazard locations
            # or future task truth are exposed.
            "safety": dict(current_health=health_report_value((panels.get("safety") or {}).get("health")),
                recent_events=list(self._safety_events[-6:])),
            "sim_time_seconds": panels.get("sim_time"),
            "focused_subtask": focused,
            # Evaluator/APC state: public_observation removes this entire field.
            "subtask_progress": [{
                "id": str(row.get("id")),
                "status": row.get("status"),
                "description": row.get("description"),
                "masked": False,
            } for row in subtasks if not row.get("masked")],
            "episode_done": episode_done,
            # Exact rendered visibility, with a generic held-object exception only.
            "interactables": [{
                "handle": handle,
                "label": entry["label"],
                "bbox": entry["bbox"],
                "allowed_actions": list(entry["verbs"]),
                "currently_operable": entry["extra"].get("currently_operable", True),
            } for handle, entry in sorted(self._entities.items())
                if entry.get("visible") or entry["extra"].get("fov_exempt")],
            # 自我里程计:完全由公开动作结果航位推算而来,等价于真实机器人
            # 的轮式编码器/IMU;不含任何环境私有信息。
            "ego_state": {
                "position_relative_spawn_m": {
                    "x": round(self._dr_x, 2), "z": round(self._dr_z, 2)},
                "heading_relative_spawn_deg": round(self._heading_deg, 1),
                "camera_pitch_deg": round(self._pitch_deg, 1),
                "cumulative_translation_m": round(self._translation_m, 2),
                "cumulative_rotation_deg": round(self._rotation_degrees, 1),
                "collision_count": self._collisions,
                "held": bool((manual.get('held') or {}).get('id')),
            },
            **({"_fog_geometry": bundle["fog_geometry"]} if self.fog_gt_enabled else {}),
            "_observer": {
                "task_progress": progress_summary,
                "safety_status": {k:(panels.get('safety') or {}).get(k) for k in (
                    'health','initial_health','total_damage','alive','policy')},
                "tpv": ({"data_url": "data:image/jpeg;base64," + frames["tpv"]}
                        if frames.get("tpv") else None),
                "bev": [{
                    "floor": row.get("floor"),
                    "data_url": "data:image/jpeg;base64," + row["img"],
                } for row in (frames.get("topdowns") or []) if row.get("img")],
                "collision_flash": bool(frames.get("collision_flash")),
                "subtask_progress_full": [{
                    "id": str(row.get("id")),
                    "status": row.get("status"),
                    "description": row.get("description"),
                    "masked": bool(row.get("masked")),
                    "mandatory": row.get("mandatory", True),
                    "prohibited": row.get("prohibited", False),
                    "requires": row.get("requires", []),
                    "evidence_source": row.get("evidence_source", progress_source),
                } for row in subtasks],
                "offscreen_operables": [{
                    "handle": handle, "label": entry["label"],
                    "allowed_actions": list(entry["verbs"]),
                } for handle, entry in sorted(self._entities.items())
                    if not entry.get("visible")
                    and not entry["extra"].get("fov_exempt")],
            },
            "_private": {
                "frame_token": self._frame_token,
                "grounding_version": private_frame.get("grounding_version"),
                "frame_rgb_sha256": private_frame.get("rgb_sha256"),
                "house_index": status.get("house_index"),
                "handle_map": {
                    handle: {"raw_key": entry["raw_key"],
                             "raw_id": entry["raw_id"],
                             "verbs": entry["verbs"]}
                    for handle, entry in self._entities.items()
                },
                "panels_digest": hashlib.sha256(
                    json.dumps(panels, ensure_ascii=False, sort_keys=True,
                               default=str).encode()).hexdigest(),
            },
        }
        self._last_health = health_report_value(observation.get('health'))
        observation['available_actions'] = sorted(ALLOWED_VERBS - {'pull', 'use'})
        observation['_private']['information_version'] = INFORMATION_VERSION
        if private_frame.get('surface_observation_evidence'):
            observation['_private']['surface_observation_evidence'] = private_frame['surface_observation_evidence']
        return observation

    # ---------------------------------------------------------------- step
    def _sanitize(self, message: str) -> str:
        """Public feedback must not carry raw objectIds."""
        def swap(match: re.Match[str]) -> str:
            return '[目标]'
        message = _RAW_ID_PATTERN.sub(swap, message)
        return SPAWNED_OBJECT_ID_PATTERN.sub(swap, message)

    def _rejection_feedback(self, raw_id: str, verb: str) -> dict:
        response = self._request('POST', '/api/inspect/target-feedback-private', json={
            'observation_token': self._frame_token, 'object_id': raw_id, 'verb': verb})
        if response.status_code != 200:
            raise TargetGroundingError({'status': 'rejection_feedback_unavailable',
                                        'failure_class': 'interface_error'})
        body = response.json()
        if body.get('observation_token') != self._frame_token:
            raise TargetGroundingError({'status': 'stale_rejection_feedback',
                                        'failure_class': 'interface_error'})
        try:
            render_feedback(body['agent_feedback'], verb)
        except (ValueError, KeyError) as error:
            raise TargetGroundingError({'status': 'invalid_rejection_feedback',
                                        'failure_class': 'interface_error'}) from error
        return body['agent_feedback']

    def _resolve_target(self, action: AgentAction) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        target = action.target
        if target is None:
            return None, {"status": "not_required", "private_object_id": None}
        if target.kind == 'pixel' and self._front_annotation_rect:
            w,h=self._frame_size;x,y=target.x*w,target.y*h
            x0,y0,x1,y1=self._front_annotation_rect
            if x0<=x<=x1+1 and y0<=y<=y1+1:
                return None,dict(status='view_label_region',private_object_id=None,
                    agent_feedback={'version':FEEDBACK_VERSION,'code':'view_label_region'})
        if target.kind == "handle":
            entry = self._entities.get(str(target.handle))
            if entry is None:
                return None, {"status": "unknown_handle",
                              "private_object_id": None}
            if (not entry.get("visible")
                    and not entry["extra"].get("fov_exempt")):
                # Off-screen entities are never exposed to the model; a handle
                # for one must fail closed even if it were somehow guessed.
                return None, {"status": "handle_not_in_view",
                              "private_object_id": entry.get("raw_id"),
                              "handle": target.handle}
            if action.verb not in entry["verbs"]:
                return None, {"status": "verb_not_operable",
                              'agent_feedback': self._rejection_feedback(entry.get('raw_id'), action.verb),
                              "private_object_id": entry.get("raw_id"),
                              "handle": target.handle}
            return entry, {"status": "resolved",
                           "private_object_id": entry.get("raw_id"),
                           "handle": target.handle}
        # pixel: frame-bound private mask resolution, fail closed.
        if not self._frame_token:
            raise TargetGroundingError({"status": "no_frame_token", "failure_class": "interface_error"})
        response = self._request("POST", "/api/inspect/resolve-pixel-private", json={
            "observation_token": self._frame_token,
            "x": target.x, "y": target.y,
        })
        if response.status_code != 200:
            detail = {}
            try:
                detail = response.json()
            except ValueError:
                pass
            resolution = {"status": str(detail.get("error")
                                        or f"http_{response.status_code}"),
                          "match_count": detail.get("match_count"),
                          "private_object_id": None, "grounding_proof": detail,
                          "failure_class": detail.get("failure_class", "interface_error")}
            if resolution["failure_class"] != "invalid_agent_target":
                raise TargetGroundingError(resolution)
            return None, resolution
        proof = response.json()
        if not isinstance(proof, dict):
            raise TargetGroundingError({'status': 'invalid_grounding_response',
                                        'failure_class': 'interface_error'})
        if not self._frame_size:
            raise TargetGroundingError({"status": "missing_frame_dimensions", "failure_class": "interface_error"})
        width, height = self._frame_size
        expected_index = [min(width - 1, int(target.x * width)),
                          min(height - 1, int(target.y * height))]
        if (proof.get("grounding_version") != GROUNDING_VERSION
                or proof.get("observation_token") != self._frame_token
                or proof.get("pixel") != [target.x, target.y]
                or proof.get("pixel_index") != expected_index
                or proof.get("image_size") != [width, height]
                or proof.get("native_identity_verified") is not True
                or not proof.get("rendered_object_ids")
                or not isinstance(proof.get('object_id'), str) or not proof.get('object_id')
                or not isinstance(proof.get('native_object_id'), str)):
            raise TargetGroundingError({"status": "invalid_grounding_response",
                "failure_class": "interface_error", "grounding_proof": proof})
        raw_id = str(proof["object_id"])
        aliases = getattr(self, '_frame_identity_aliases', {})
        hits = proof['rendered_object_ids']
        if (not isinstance(hits, list) or not all(isinstance(oid, str) for oid in hits)
                or any(aliases.get(oid, [oid]) != [raw_id] for oid in hits)
                or proof.get('identity_mapping') != {oid: aliases.get(oid, [oid]) for oid in hits}
                or aliases.get(proof.get('native_object_id'), [proof.get('native_object_id')]) != [raw_id]):
            raise TargetGroundingError({'status': 'invalid_identity_mapping',
                'failure_class': 'interface_error', 'grounding_proof': proof})
        matches = [row for row in self._entities.values()
                      if row.get("raw_id") == raw_id
                      and action.verb in row["verbs"]]
        if len(matches) > 1:
            raise TargetGroundingError({"status": "semantic_target_ambiguous",
                "failure_class": "interface_error", "private_object_id": raw_id,
                "grounding_proof": proof,
                "operation_candidates": [{"kind": row["kind"], "id": row["op_id"]} for row in matches]})
        if not matches:
            # The pixel resolved to a real object, but the runtime does not
            # confirm this verb on it right now -> fail closed.
            return None, {"status": "resolved_not_operable",
                          'agent_feedback': self._rejection_feedback(raw_id, action.verb),
                          "failure_class": "operation_rejected",
                          "private_object_id": raw_id, "grounding_proof": proof}
        entry = matches[0]
        return entry, {"status": "resolved", "private_object_id": raw_id,
                       "grounding_proof": proof,
                       "operation_identity": {"kind": entry["kind"], "id": entry["op_id"], "verb": action.verb}}

    def _act(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._request("POST", "/api/inspect/act", json=body)
        if response.status_code >= 500:
            raise EnvironmentUnavailable(
                "环境执行服务异常；动作是否部分执行需要核对，已暂停以避免重复执行")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 200 or payload.get("error"):
            return {"ok": False,
                    "message": str(payload.get("error")
                                   or f"act failed http_{response.status_code}"),
                    "diff": []}
        return payload

    def step(self, action: AgentAction) -> dict[str, Any]:
        self.assert_ready()
        if self._forced_done:
            raise RuntimeError("episode is already done")
        statuses_before = dict(self._last_statuses)
        health_before = self._current_health()
        requested = dict(action.parameters)
        actual = dict(requested)
        result: dict[str, Any] = {"ok": False, "message": "", "diff": []}
        resolution: dict[str, Any] = {"status": "not_required",
                                      "private_object_id": None}
        collision = False
        if (action.verb in ('move_forward','move_backward')
                and float(action.parameters['distance_m']) > 1
                and self._motion_protocol != 'straight_motion_v2_5m'):
            raise EnvironmentUnavailable('后端尚未启用 5 米分段移动协议；未执行本次动作，请更新后端')

        motion_ops = {
            "move_forward": ("move", {"action": "MoveAhead"}),
            "move_backward": ("move", {"action": "MoveBack"}),
            "rotate_left": ("rotate", {"action": "RotateLeft"}),
            "rotate_right": ("rotate", {"action": "RotateRight"}),
            "look_up": ("look", {"action": "LookUp"}),
            "look_down": ("look", {"action": "LookDown"}),
        }
        if action.verb in motion_ops:
            op, extra = motion_ops[action.verb]
            body = {"op": op, **extra}
            if op == "move":
                body["magnitude"] = float(action.parameters["distance_m"])
            else:
                body["degrees"] = float(action.parameters["degrees"])
            result = self._act(body)
            if op == "move":
                import math
                reported_motion = (result.get("detail") or {}).get("actual_motion") or {}
                if (float(requested['distance_m']) > 1
                        and reported_motion.get('protocol') != 'straight_motion_v2_5m'):
                    raise EnvironmentUnavailable('长距离动作缺少实际位移回执；动作可能已执行，暂停核对，禁止重放')
                # A collision may occur AFTER part of a long move has executed.
                dist = float(reported_motion.get("distance_m",
                    action.parameters["distance_m"] if result.get("ok") else 0.))
                if not math.isfinite(dist) or dist < 0 or dist > float(requested['distance_m'])+.02:
                    raise EnvironmentUnavailable("环境返回无效实际位移；动作可能已执行，已暂停核对")
                actual["distance_m"] = dist
                if reported_motion.get('protocol') == 'straight_motion_v2_5m':
                    actual['primitive_steps'] = int(reported_motion['primitive_steps'])
                    actual['stopped_early'] = bool(reported_motion['stopped_early'])
                    actual['motion_protocol'] = reported_motion['protocol']
                sign = 1.0 if action.verb == "move_forward" else -1.0
                self._translation_m += dist
                self._dr_x += math.sin(math.radians(self._heading_deg)) * dist * sign
                self._dr_z += math.cos(math.radians(self._heading_deg)) * dist * sign
            elif result.get("ok") and op == "rotate":
                deg = float(action.parameters["degrees"])
                self._rotation_degrees += deg
                delta = deg if action.verb == "rotate_right" else -deg
                self._heading_deg = (self._heading_deg + delta) % 360.0
            elif result.get("ok") and op == "look":
                deg = float(action.parameters["degrees"])
                self._pitch_deg += deg if action.verb == "look_down" else -deg
            if not result.get("ok") and op in ("move", "rotate"):
                collision = True
                self._collisions += 1
        elif action.verb in ("use_elevator", "use_stairs"):
            result = self._act({"op": "elevator" if action.verb == "use_elevator" else "stairs",
                                "floor": int(action.parameters["floor"]) - 1})
        elif action.verb == "wait":
            result = self._act({"op": "wait", "steps": 1})
        elif action.verb == "done":
            result = {"ok": True, "message": "Agent declared done", "diff": []}
        else:
            entry, resolution = self._resolve_target(action)
            if entry is None:
                result = {
                    "ok": False,
                    'agent_feedback': resolution.get('agent_feedback') or {
                        'version': FEEDBACK_VERSION, 'code': ('target_miss'
                            if resolution.get('status') == 'pixel_miss' else 'invalid_target')},
                    "message": (
                        "已定位到所指物体，但当前无法对它执行该动作。"
                        if resolution.get("status") == "resolved_not_operable" else
                        "目标未选中：" + str(resolution.get("status"))
                        + "。请依据当前画面重新指定目标。"),
                    "diff": [],
                }
            else:
                result = self._dispatch_entity_action(action, entry)

        return self.finalize_action_receipt(action, result, resolution,
            requested, actual, collision, statuses_before, health_before)

    def prepare_gt_navigation(self, observation_id):
        if not self.gt_navigation_enabled or observation_id != self._current_obs_id:
            raise EnvironmentUnavailable('GT navigation disabled or stale observation')
        data = self._json('POST', '/api/inspect/gt-navigation/prepare', json={'_grounding_token':self._frame_token})
        # Older Inspector hotfixes returned HTTP 200 for this infrastructure
        # error. Never turn it into an agent-visible failed decision.
        if data.get('feedback_code') == 'gt_navigation_reachability_bounds_error':
            raise EnvironmentUnavailable(_NAVIGATION_BOUNDS_ERROR)
        if data.get('success'):
            hashes={v['view_id']:v['source_rgb_sha256'] for v in data.get('views',[])}
            rendering=data.get('selection_rendering')
            paired=(hashes==self._gt_navigation_view_hashes)
            if rendering is not None:
                paired=(rendering.get('version')=='navigation_self_occlusion_removed_v1'
                    and rendering.get('source_observation_view_sha256')==self._gt_navigation_view_hashes
                    and rendering.get('selection_view_sha256')==hashes
                    and set(hashes)=={'left','front','right'}
                    and all(rendering.get(k) is True for k in (
                        'world_occlusion_preserved','paired_rgb_depth','physical_state_unchanged','normal_view_restored')))
            if data.get('version')!='visible_gt_waypoint_v2_triplet' or not paired:
                raise EnvironmentUnavailable('GT navigation requires paired current left/front/right RGB; upgrade 8851')
        return data

    def plan_gt_navigation(self, selection):
        data = self._json('POST', '/api/inspect/gt-navigation/plan', json={
            **selection, '_grounding_token':self._frame_token})
        if data.get('success'):
            import math
            # Calibrated world route -> existing public odometry reference.
            yaw=math.radians(self._heading_deg-data['start_heading'])
            ax,az=data['start_world_xz_m'];route=[]
            for x,z in data['route_world_xz_m'][1:]:
                dx,dz=x-ax,z-az
                route.append([self._dr_x+dx*math.cos(yaw)+dz*math.sin(yaw),
                              self._dr_z-dx*math.sin(yaw)+dz*math.cos(yaw)])
            self._gt_navigation_route=route
            self._gt_navigation_precise_start=bool(data.get('native_start_connection_used'))
            self._gt_navigation_precise_route=bool(data.get('native_route_verified'))
        return data

    def next_gt_navigation_action(self):
        import math
        route=self._gt_navigation_route
        while route and math.dist(route[0],[self._dr_x,self._dr_z]) <= (
                .05 if (getattr(self,'_gt_navigation_precise_start',False) or
                        getattr(self,'_gt_navigation_precise_route',False)) else .12):
            route.pop(0)
            self._gt_navigation_precise_start=False
        if not route:return None
        dx,dz=route[0][0]-self._dr_x,route[0][1]-self._dr_z
        distance=math.hypot(dx,dz)
        angle=(math.degrees(math.atan2(dx,dz))-self._heading_deg+180)%360-180
        # Align the body before every forward segment. No strafing/diagonal
        # translation action; the <=1 degree residual matches the action minimum.
        if abs(angle)>1:
            return AgentAction('rotate_right' if angle>0 else 'rotate_left',None,{'degrees':min(90.,abs(angle))})
        travel=min(.75,distance)
        if (getattr(self,'_gt_navigation_precise_start',False) or
                getattr(self,'_gt_navigation_precise_route',False)) and .75<distance<.80:
            travel=distance/2
        return AgentAction('move_forward',None,{'distance_m':travel})

    def return_to_visited(self, target: dict[str, Any]) -> dict[str, Any]:
        """Request a server-side checked recall; never retry or snap locally."""
        self.assert_ready()
        if not self._frame_token:
            raise TargetGroundingError({'status':'no_frame_token','failure_class':'interface_error'})
        if not self.path_recall_enabled or target.get('current_observation_id') != self._current_obs_id:
            return {'public_result':dict(success=False,feedback_code='recall_map_stale',message='观察已过期；未移动。')}
        anchor_key=target.get('anchor_key')
        if anchor_key not in self._recall_odometry:
            return {'public_result':dict(success=False,feedback_code='recall_node_not_found',message='历史站位不属于本轮；未移动。')}
        payload = dict(anchor_key=anchor_key, _grounding_token=self._frame_token)
        response = self._request('POST', '/api/inspect/path-recall', json=payload)
        if response.status_code >= 500:
            raise EnvironmentUnavailable('路径回溯服务异常；是否已执行需要核对，禁止重放')
        data = response.json()
        if response.status_code != 200:
            raise EnvironmentUnavailable('路径回溯接口未就绪或回执异常，暂停核对，禁止重放')
        if data.get('version')!='explored_path_recall_v1' or type(data.get('success')) is not bool:
            raise EnvironmentUnavailable('回溯回执协议无效；暂停核对，禁止重放')
        if data.get('success'):
            import math
            distance=data.get('distance_m')
            if not isinstance(distance,(int,float)) or not math.isfinite(distance) or distance<0:
                raise EnvironmentUnavailable('回溯距离回执无效；暂停核对，禁止重放')
            old=self._recall_odometry[anchor_key]
            self._dr_x,self._dr_z,self._heading_deg=old['x'],old['z'],old['heading']
            observation = self._observation(data.get('message','已回溯'))
            return {'observation': observation, 'public_result': {
                'success': True, 'feedback_code': data.get('feedback_code','recall_succeeded'),
                'message': data.get('message','已回溯到历史站位'),
                'distance_m': data.get('distance_m'), 'route_edge_count':data.get('route_edge_count'), 'node_id': target.get('node_id')},
                'metrics': {'recall_count': 1, 'recall_distance_m': data.get('distance_m',0)}}
        rejected_result = {'public_result': {'success': False,
            'feedback_code': data.get('feedback_code','recall_rejected'),
            'message': data.get('message','回溯被环境拒绝；未移动、未刷新画面。')}}
        if data.get('execution_started'):
            if not data.get('rolled_back'):
                raise EnvironmentUnavailable('回溯失败但未确认回滚，暂停核对，禁止重放')
            # A verified unchanged base/world does not imply byte-identical
            # camera/arm metadata after a native placement attempt and rollback.
            rejected_result['observation']=self._observation(rejected_result['public_result']['message'])
            rejected_result['public_result']['observation_refreshed']=True
        return rejected_result

    def _current_health(self):
        return self._last_health

    @staticmethod
    def _public_safety_event(result, health_before, health_after, action):
        if health_before is None or health_after is None or health_after >= health_before:
            return None
        detail = result.get("detail") if isinstance(result.get("detail"), dict) else {}
        nested = detail.get("safety_event") if isinstance(detail.get("safety_event"), dict) else {}
        feedback = result.get("agent_feedback") if isinstance(result.get("agent_feedback"), dict) else {}
        code = str(feedback.get("code") or result.get("feedback_code") or "health_decreased")
        text = " ".join(str(result.get(k) or "") for k in ("message", "error"))
        lower = (code + " " + text).lower()
        if "pet" in lower or "dog" in lower or "animal" in lower:
            cause = "靠近动态动物的危险范围"
            advice = "保持与动态动物的公开安全距离，等待其离开后再通过。"
            event = "dynamic_hazard_proximity"
        elif "collid" in lower or "blocked" in lower or "obstruct" in lower:
            cause = "移动碰撞或被障碍阻挡"
            advice = "停止原方向，先观察障碍，后退或改变朝向后小步尝试。"
            event = "movement_collision"
        else:
            cause = str(nested.get("reason") or detail.get("cause") or detail.get("reason") or result.get("message") or "公开动作反馈导致生命值下降")[:240]
            advice = "依据这次公开反馈改变位置或动作条件，不要原样重复导致掉血的动作。"
            event = "public_feedback_damage"
        return dict(event=event,cause=cause,advice=advice,
            health_before=round(health_before,4),health_after=round(health_after,4),
            health_delta=round(health_after-health_before,4),action=action.verb,
            source="completed_public_action_feedback")

    def finalize_action_receipt(self, action, result, resolution, requested,
                                actual, collision, statuses_before, health_before=None):
        """Build an outcome from a verified receipt; never dispatch an action.

        Used by normal step completion and audited timeout reconciliation.
        Recovery callers must independently verify the completed operation.
        """
        raw_message = str(result.get("message") or "")
        if action.verb == 'done':
            result['agent_feedback'] = {'version': FEEDBACK_VERSION, 'code': 'succeeded'}
        try:
            public_message = self._sanitize(render_feedback(result.get('agent_feedback'), action.verb))
        except ValueError as error:
            raise EnvironmentUnavailable('动作反馈协议缺失或无效；动作可能已执行，暂停核对，禁止自动重放') from error
        warning = (result.get('detail') or {}).get('safety_warning') or {}
        safety_warning = None
        if warning.get('code') == 'body_collision_warning':
            # Fixed public wording only; never copy engine collider IDs/debug prose.
            safety_warning = dict(code='body_collision_warning',
                consecutive=bool(warning.get('consecutive')),
                damage_applied=float(warning.get('damage_applied', 0)),
                message='主躯干碰撞受阻，请改变方向或路线。首次不扣血；连续碰撞扣2点，扣血后两次操作免扣。')
            public_message += ' '+safety_warning['message']
        if action.verb == "done":
            self._forced_done = True

        observation = self._observation(public_message or "(no feedback)")
        health_after = self._current_health()
        safety_event = self._public_safety_event(result, health_before, health_after, action)
        if safety_event:
            self._safety_events.append(safety_event)
            self._safety_events = self._safety_events[-24:]
        elif safety_warning:
            self._safety_events.append(dict(event='body_collision_warning',
                cause='主躯干碰撞受阻', advice=safety_warning['message'],
                health_before=health_before, health_after=health_after, health_delta=0,
                action=action.verb, source='completed_public_action_feedback'))
            self._safety_events = self._safety_events[-24:]
        observation["safety"] = dict(current_health=health_after if health_after is not None else observation.get("health"),
            recent_events=list(self._safety_events[-6:]))
        panels_done = bool(observation.get("episode_done"))
        statuses_after = self._last_statuses

        evidence = []
        for subtask_id, status_now in statuses_after.items():
            if status_now == "done" and statuses_before.get(subtask_id) != "done":
                subtask = next((row for row in observation["subtask_progress"]
                                if row["id"] == subtask_id), {})
                goal_kind = self._goal_kind_for(subtask_id)
                for capability_id in _GOAL_CAPABILITIES.get(goal_kind, ("D4.1",)):
                    evidence.append({
                        "capability_id": capability_id,
                        "status": "passed_unassisted",
                        "subtask_id": subtask_id,
                        "goal_kind": goal_kind,
                        "provisional_capability_mapping": True,
                        "world_state_readback": True,
                        "description": subtask.get("description"),
                    })

        mandatory = self._evaluation_subtasks(observation)
        done_count = sum(row.get("status") == "done" for row in mandatory)
        total = len(mandatory)
        from agentlab.task_progress import summary
        progress = summary(mandatory)
        return {
            "observation": observation,
            "public_result": {
                "success": bool(result.get("ok")),
                "message": public_message,
                "feedback_code": result["agent_feedback"]["code"],
                "information_version": INFORMATION_VERSION,
                "done": panels_done,
                "requested": requested,
                "actual": actual if result.get("ok") or action.verb in ('move_forward','move_backward') else {},
                "collision": collision,
                **({"safety_event": safety_event} if safety_event else {}),
                **({'safety_warning': safety_warning} if safety_warning else {}),
            },
            "target_resolution": resolution,
            "world_state_diff": {
                "inspector_diff": result.get("diff") or [],
                "raw_message": raw_message,
            },
            "capability_evidence": evidence,
            "metrics": {
                "true_goal_completion": progress["gc"],
                "task_progress": progress,
                "task_success": self._panels_success(observation),
                "subtasks_done": progress["done"],
                "subtasks_total": progress["total"],
                "cumulative_translation_m": round(self._translation_m, 4),
                "cumulative_rotation_degrees": round(self._rotation_degrees, 4),
                "collision_count": self._collisions,
                "mandatory_visible": sum(not row.get("masked") for row in mandatory),
            },
        }

    def _evaluation_subtasks(self, observation: dict[str, Any]) -> list[dict[str, Any]]:
        # Scoring may use complete evaluator state. Public observations and
        # model context continue to omit the entire subtask plan.
        rows = (getattr(self, "_panels_subtasks", []) or
                observation.get("_observer", {}).get("subtask_progress_full") or
                observation.get("subtask_progress", []))
        return [row for row in rows if row.get("mandatory", True)
                and not row.get("prohibited", False)]

    def _panels_success(self, observation: dict[str, Any]) -> bool:
        native = getattr(self, "_native_task_success", None)
        if native is not None:
            return native
        rows = self._evaluation_subtasks(observation)
        return bool(rows) and all(row.get("status") == "done" for row in rows)

    def _goal_kind_for(self, subtask_id: str) -> str:
        subtask = next((row for row in self._panels_subtasks
                        if str(row.get("id")) == subtask_id), None)
        return str((subtask or {}).get("type") or "unknown")

    def _dispatch_entity_action(self, action: AgentAction,
                                entry: dict[str, Any]) -> dict[str, Any]:
        verb = action.verb
        op_id = entry["op_id"]
        if verb in ("open", "close"):
            return self._act({"op": verb, "id": op_id})
        if verb == "fill_water":
            return self._act({"op": "fill_water", "id": op_id})
        if verb == "handoff_npc":
            return self._act({"op": "handoff", "id": op_id})
        if verb == "pickup":
            return self._act({"op": "manual_pickup", "id": op_id})
        if verb == "put":
            return self._act({"op": "manual_place", "id": op_id,
                              "target_kind": entry["extra"].get(
                                  "target_kind", "surface")})
        if verb == "push":
            # A current, allowed agent action has already selected this target.
            # Inspector's human-UI confirmation must not reject that selection.
            return self._act({"op": "manual_push", "id": op_id,
                              "confirmed": True})
        if verb == "read":
            return self._act({"op": "read_note", "id": op_id})
        if verb in ("input_code", "unlock"):
            body: dict[str, Any] = {"op": "unlock", "id": op_id}
            if verb == "input_code":
                body["code"] = str(action.parameters["code"]).strip()
            return self._act(body)
        if verb == "drop":
            return self._act({"op": "manual_release_ground",
                              "confirmed": True})
        if verb == "answer_call":
            body = {"op": "callback", "text": str(action.parameters["text"]).strip()}
            body['id'] = op_id
            return self._act(body)
        if verb == "ask_npc":
            payload = self._json("POST", "/api/inspect/npc-query", json={
                "human_id": op_id, "mode": "query",
                "utterance": str(action.parameters["question"]).strip(),
            })
            return {"ok": bool(payload.get("ok", not payload.get("error"))),
                    "agent_feedback": payload.get("agent_feedback"),
                    "message": str(payload.get("message")
                                   or payload.get("response_text")
                                   or payload.get("reply")
                                   or payload.get("error") or ""),
                    "diff": []}
        if verb == "answer_npc":
            payload = self._json("POST", "/api/inspect/npc-query", json={
                "human_id": op_id, "mode": "answer",
                "utterance": str(action.parameters["text"]).strip(),
            })
            ok = bool(payload.get("success", payload.get("ok",
                      not payload.get("error"))))
            return {"ok": ok, "agent_feedback": payload.get("agent_feedback"),
                    "message": str(payload.get("response_text")
                                   or payload.get("message")
                                   or payload.get("error") or ""),
                    "diff": []}
        if verb == "screen_cycle":
            return self._act({"op": "screen", "id": op_id, "signal": "cycle"})
        if verb in ("screen_on", "screen_off"):
            return self._act({"op": "screen", "id": op_id,
                              "signal": "on" if verb == "screen_on" else "off"})
        if verb in ("toggle_on", "toggle_off"):
            return self._act({"op": "toggle", "id": op_id,
                              "state": verb == "toggle_on"})
        return {"ok": False, "message": f"unsupported verb {verb}", "diff": []}

    # ------------------------------------------------------------- handoff
    def apply_handoff(self, subtask_id: str) -> dict[str, Any]:
        """Ask the environment to realize exactly the bound intermediate node.

        No client-side guessing from the first screen/container/lock. The
        private endpoint returns real postcondition evidence, including failed
        partial attempts. Its raw contract never enters ordinary observation.
        """
        response = self._json("POST", "/api/inspect/apc-handoff-private",
                              json={"subtask_id": subtask_id})
        result = (response.get("detail") or {}).get("handoff")
        if not isinstance(result, dict):
            raise RuntimeError("Inspector did not return a bound APC result")
        if not result.get("attempted"):
            return {**result, "subtask_id": subtask_id, "applied": False}
        self._assist_seq += 1
        kind = str(result.get("goal_kind") or "")
        grants = list(result.get("information_grants") or [])
        capabilities = list(_GOAL_CAPABILITIES.get(kind, ("D4.1",)))
        agent_diff = (result.get("world_state_diff") or {}).get("agent") or {}
        if agent_diff.get("before") != agent_diff.get("after"):
            capabilities = sorted(set(capabilities) | {"D3.1", "D3.2"})
        taint = sorted(set(capabilities) | ({"D5.1", "D5.3", "D6.2"} if grants else set()))
        verified = bool(result.get("postconditions_verified"))
        context = (f"子任务 {subtask_id} 的 APC 特权操作已"
                   + ("通过真实后置条件验证。" if verified else
                      "尝试，但后置条件未通过；该节点仍未完成。"))
        return {**result, "applied": True,
                "assist_id": f"assist_{self._assist_seq:04d}",
                "assist_types": ["world_transition"] + (["information_grant"] if grants else []),
                "capabilities": capabilities, "taint_lineage": taint,
                "handoff_context": context,
                "observation": self._observation(context)}
