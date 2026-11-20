from __future__ import annotations

from agentlab.runner_messages import message as _runner_message
from agentlab.runtime_state import terminal_observation

from agentlab.agent.compaction import budget_for

from agentlab.agent.behavior import public_history_warnings

import json
import platform
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agentlab import __version__
from agentlab.agent import APCMonitor, ContextBuilder, StructuredMemory
from agentlab.audit import AuditStore
from agentlab.domain import (
    CAPABILITIES, AgentAction, InterfaceMode, RunMode, Stage, Visibility,
    public_observation, validate_action_for_interface,
)
from agentlab.environment.base import EnvironmentClient, TargetGroundingError
from agentlab.providers.base import Provider, ProviderOutputUnavailable
from agentlab.providers.openai_compatible import ProviderRequestCancelled, retry_policy
from agentlab.security import redact


MODEL_OUTPUT_POLICY = "append_missing_closers_v1"
MAX_OUTPUT_RETRIES = 2
OUTPUT_BUDGET_POLICY = "length_flag_bounded_1_2_4_v2"


class ModelOutputRejected(ValueError):
    def __init__(self, error: Exception, *, output_truncated: bool = False):
        super().__init__(str(error))
        self.original_error = error
        self.output_truncated = output_truncated


def _missing_json_closers(text: str) -> str | None:
    """Only complete delimiters after a complete container, never a value."""
    if not text.startswith("{") or not text.endswith(("}", "]")):
        return None
    stack: list[str] = []
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack.pop() != char:
                return None
    if in_string or not 1 <= len(stack) <= 8:
        return None
    return "".join(reversed(stack))


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_model_json(content: str, *,
                     repairs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    stripped = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL | re.IGNORECASE)
    if fenced:
        stripped = fenced.group(1)
    try:
        result = json.loads(stripped)
    except json.JSONDecodeError as error:
        suffix = _missing_json_closers(stripped)
        repaired = None
        if suffix:
            try:
                repaired = json.loads(stripped + suffix,
                                      object_pairs_hook=_unique_json_object)
                action = repaired.get("action")
                if not isinstance(action, dict) or not {"verb", "target", "parameters"} <= action.keys():
                    repaired = None
            except ValueError:
                repaired = None
        if repaired is not None:
            result = repaired
            if repairs is not None:
                repairs.append({
                    "strategy": MODEL_OUTPUT_POLICY,
                    "appended": suffix,
                    "original_error": str(error),
                    "repaired_content": stripped + suffix,
                })
        else:
            start, end = stripped.find("{"), stripped.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("model response does not contain a JSON object") from error
            result = json.loads(stripped[start:end + 1])
    if not isinstance(result, dict):
        raise ValueError("model response must be a JSON object")
    if not isinstance(result.get("action"), dict) and not (
        result.get("decision_type") in ("navigate", "return_to_visited", "gt_navigate") and result.get("action") is None
        and isinstance(result.get({"navigate":"navigation_request","return_to_visited":"return_request","gt_navigate":"gt_navigation_request"}.get(result.get("decision_type"))), dict)):
        raise ValueError("model response requires an action object")
    return result


@dataclass(frozen=True)
class RunConfig:
    scenario_id: str
    scenario_digest: str
    seed: int
    interface_mode: InterfaceMode
    run_mode: RunMode
    provider_name: str
    provider_model: str
    focused_timeout_seconds: float = 300
    environment_backend: str = "mock"
    house_index: int | None = None
    agent_mode: str | None = None
    apc_auto: bool = True   # False = 只计时,超时不自动交接(仅人工「立即交接」)
    spatial_memory_enabled: bool = False
    depth_image_enabled: bool = False
    fog_gt_enabled: bool = False
    visual_frame_count: int = 1
    context_policy: str = 'legacy'
    navigation_enabled: bool = False
    gt_navigation_enabled: bool = False
    path_recall_enabled: bool = False
    # Model self-compaction policy.  These values are deliberately part of
    # the run snapshot so token-control experiments are reproducible.
    memory_compaction_enabled: bool = True
    memory_compaction_trigger_tokens: int | None = None
    memory_compaction_target_tokens: int | None = None
    memory_compaction_hard_limit_tokens: int | None = None

    def __post_init__(self):
        if type(self.visual_frame_count) is not int or self.visual_frame_count not in (1, 5, 10):
            raise ValueError('visual_frame_count must be 1, 5 or 10')
        if self.context_policy not in ('legacy', 'enhanced_v1'):
            raise ValueError('unknown context_policy')
        if type(self.depth_image_enabled) is not bool:
            raise ValueError('depth_image_enabled must be a boolean')
        if self.depth_image_enabled and self.environment_backend != 'development_inspector':
            raise ValueError('metric depth requires the Inspector native sensor')
        if type(self.fog_gt_enabled) is not bool:
            raise ValueError('fog_gt_enabled must be a boolean')
        if self.fog_gt_enabled and self.spatial_memory_enabled:
            raise ValueError('RGB estimated mapping and explored GT mapping are mutually exclusive')
        if self.fog_gt_enabled and self.environment_backend != 'development_inspector':
            raise ValueError('explored GT mapping requires the 8851 Inspector geometry protocol')
        if type(self.navigation_enabled) is not bool:
            raise ValueError('navigation_enabled must be a boolean')
        if self.navigation_enabled and self.agent_mode not in ('arm', None):
            raise ValueError('pixel navigation requires the calibrated arm embodiment')
        if type(self.path_recall_enabled) is not bool:
            raise ValueError('path_recall_enabled must be a boolean')
        if self.path_recall_enabled and not self.fog_gt_enabled:
            raise ValueError('explored path recall requires explored GT BEV')
        if type(self.gt_navigation_enabled) is not bool:
            raise ValueError('gt_navigation_enabled must be boolean')
        if self.gt_navigation_enabled and (self.navigation_enabled or self.environment_backend != 'development_inspector'):
            raise ValueError('GT waypoint navigation requires Inspector and excludes legacy pixel navigation')
        if self.gt_navigation_enabled and self.agent_mode not in ('arm', None):
            raise ValueError('GT waypoint navigation requires the calibrated arm embodiment')
        if self.path_recall_enabled and self.navigation_enabled:
            raise ValueError('legacy pixel navigation and explored path recall are mutually exclusive')
        if self.path_recall_enabled and self.agent_mode not in ('arm', None):
            raise ValueError('explored path recall requires the calibrated arm embodiment')
        from agentlab.agent.compaction import budget_for
        if type(self.memory_compaction_enabled) is not bool:
            raise ValueError('memory_compaction_enabled must be boolean')
        for value in (self.memory_compaction_target_tokens,self.memory_compaction_trigger_tokens,self.memory_compaction_hard_limit_tokens):
            if value is not None and (type(value) is not int or value<=0):
                raise ValueError('memory budgets must be positive integer tokens')
        b=budget_for(self)
        if not (0 < b.target < b.trigger < b.hard):
            raise ValueError('invalid memory compaction token budgets')
        if self.spatial_memory_enabled and self.agent_mode not in ('arm', None):
            raise ValueError('RGB BEV currently requires the calibrated arm embodiment')


class EpisodeRunner:
    def __init__(
        self,
        *,
        config: RunConfig,
        provider: Provider,
        environment: EnvironmentClient,
        runs_dir: Path,
        context_builder: ContextBuilder,
        environment_profile: dict[str, Any] | None = None,
        checkpoint_enabled: bool = False,
    ):
        self.config = config
        self.provider = provider
        self.environment = environment
        self.context_builder = context_builder
        from agentlab.agent.compaction import SelfCompactor
        self.compactor = (SelfCompactor(config,context_builder.prompt_dir/'memory_compact.md')
                          if config.memory_compaction_enabled else None)
        self.run_id = f"run_{uuid.uuid4().hex[:12]}"
        self.run_dir = runs_dir / self.run_id
        self.audit = AuditStore(self.run_dir, self.run_id)
        self.memory = StructuredMemory(interface_mode=config.interface_mode.value)
        from agentlab.agent.temporal import VisualHistory
        from agentlab.agent.support import TaskSupport
        self.spatial_memory = None
        if hasattr(environment,'current_views_enabled'):
            environment.current_views_enabled = True
        if config.depth_image_enabled and not hasattr(environment, 'depth_image_enabled'):
            raise ValueError('environment does not support metric depth')
        if hasattr(environment, 'depth_image_enabled'):
            environment.depth_image_enabled = config.depth_image_enabled
        if config.spatial_memory_enabled:
            from agentlab.agent.estimated_map import EstimatedSpatialMemory
            self.spatial_memory = EstimatedSpatialMemory(config.interface_mode)
        if hasattr(environment, 'fog_gt_enabled'):
            environment.fog_gt_enabled = config.fog_gt_enabled
        if config.fog_gt_enabled:
            if not hasattr(environment, 'fog_gt_enabled'):
                raise ValueError('environment does not support privileged fog geometry')
            from agentlab.agent.fpv_fog_bev import ThreeViewFogBev
            environment.fog_gt_first_person = True
            self.spatial_memory = ThreeViewFogBev()
        self.visual_history = VisualHistory(config.interface_mode) if config.visual_frame_count > 1 else None
        self.task_support = TaskSupport() if config.context_policy == 'enhanced_v1' else None
        self.navigation = None
        if config.navigation_enabled:
            from agentlab.agent.pixel_navigation import PixelNavigationController
            self.navigation = PixelNavigationController()
        self._gt_navigation_pending = None
        self.last_gt_navigation = None
        if config.gt_navigation_enabled and not hasattr(environment,'prepare_gt_navigation'):
            raise ValueError('environment has no GT waypoint endpoint')
        if hasattr(environment,'gt_navigation_enabled'):
            environment.gt_navigation_enabled=config.gt_navigation_enabled
        self.path_recall = bool(config.path_recall_enabled)
        self.path_recall_reconciliation_required = False
        self.last_recall = None
        if self.path_recall:
            self.spatial_memory.recall_enabled = True
            if not callable(getattr(environment,'return_to_visited',None)):
                raise ValueError('environment has no verified path recall endpoint')
            environment.path_recall_enabled = True
        self.apc = APCMonitor(config.focused_timeout_seconds)
        self.observation: dict[str, Any] | None = None
        self.observer_views: dict[str, Any] | None = None
        self.recent_results: list[dict[str, Any]] = []
        self.step_stats: list[dict[str, Any]] = []
        self.status = "created"
        self.boot_error: str | None = None
        self.step_number = 0
        self.primitive_action_count = 0
        self.navigation_reconciliation_required = False
        self.environment_profile = dict(environment_profile or {
            "backend": config.environment_backend,
            "watermark": "MOCK",
            "formal_sr_eligible": True,
        })
        self.backend_formal_eligible = bool(
            self.environment_profile.get("formal_sr_eligible", True))
        self.formal_sr_eligible = self.backend_formal_eligible and not config.fog_gt_enabled
        self.assist_count = 0
        self.model_waiting = False
        self.provider_retry = None
        self._request_cancel_event = threading.Event()
        self.auto_running = False
        self.auto_step_limit = None
        self.auto_stop_reason: str | None = None
        self.auto_error: dict[str, Any] | None = None
        # 整局墙钟(与 APC 同步：首个 step 启动,停止时冻结)
        self._episode_active_seconds = 0.0
        self._episode_started_at: float | None = None
        self.tainted_capabilities: set[str] = set()
        self.capability_status = {item["id"]: "not_reached" for item in CAPABILITIES}
        self._lock = threading.RLock()
        self._step_lock = threading.RLock()
        self._checkpoint_enabled = checkpoint_enabled
        self._checkpoint_dispatch = False
        self.checkpoint_reconciliation_required = False
        self.checkpoint_save_pending = False
        self.checkpoint_info = None
        self.maintenance_hold = None
        if self._checkpoint_enabled:
            from agentlab.checkpoints import journal
            environment._checkpoint_before_dispatch = lambda op: journal(self, 'dispatching', op)
        self._write_manifest()

    def _write_manifest(self) -> None:
        from agentlab.information import contract
        manifest = {
            'information_contract': contract(self.config.interface_mode),
            "run_id": self.run_id,
            "scenario_id": self.config.scenario_id,
            "scenario_digest": self.config.scenario_digest,
            "seed": self.config.seed,
            "interface_mode": self.config.interface_mode.value,
            "run_mode": self.config.run_mode.value,
            "provider_name": self.config.provider_name,
            "provider_model": self.config.provider_model,
            "focused_timeout_seconds": self.config.focused_timeout_seconds,
            "environment_backend": self.config.environment_backend,
            "environment_profile": self.environment_profile,
            "agentlab_version": __version__,
            "python": platform.python_version(),
            "protocol_version": "1.0",
            "step_counting_protocol": "model_decision_v2",
            "safety_policy_version": ('decision_safety_v2' if self.config.environment_backend == 'development_inspector' else None),
            "motion_contract": self.environment.capabilities().get('motion'),
            "current_view_protocol": ('left_front_right_v1' if getattr(self.environment,'current_views_enabled',False) else 'single_front_legacy'),
            "model_output_policy": MODEL_OUTPUT_POLICY,
            "max_output_retries": MAX_OUTPUT_RETRIES,
            'provider_retry_policy': (retry_policy(self.provider.profile)
                if getattr(self.provider, 'supports_retry_control', False) else None),
            "output_budget_policy": OUTPUT_BUDGET_POLICY,
            "waypoint_output_budget_policy": "length_flag_bounded_1_2_16_v1",
            "provider_output_acceptance": "length_never_committed_v1",
            "summary_transport_policy": "pause_preserve_pending_v1",
            "agent_mode": self.config.agent_mode,
            "house_index": self.config.house_index,
            "apc_auto": self.config.apc_auto,
            "spatial_memory_enabled": self.config.spatial_memory_enabled,
            "depth_image_enabled": self.config.depth_image_enabled,
            "depth_image_version": "front_metric_depth_v1" if self.config.depth_image_enabled else None,
            "sensor_condition": "RGB-D" if self.config.depth_image_enabled else "RGB",
            "fog_gt_enabled": self.config.fog_gt_enabled,
            "map_assistance": self.spatial_memory.state()['assistance'] if self.config.fog_gt_enabled else None,
            "spatial_memory_version": self.spatial_memory.version if self.spatial_memory else None,
            "visual_frame_count": self.config.visual_frame_count,
            "context_policy": self.config.context_policy,
            "navigation_enabled": self.config.navigation_enabled,
            "gt_navigation_enabled": self.config.gt_navigation_enabled,
            "gt_navigation_version": "visible_gt_waypoint_v2_triplet" if self.config.gt_navigation_enabled else None,
            "navigation_version": self.navigation.state['version'] if self.navigation else None,
            "path_recall_enabled": self.config.path_recall_enabled,
            "memory_compaction": {
                "enabled": self.config.memory_compaction_enabled,
                "trigger_tokens": budget_for(self.config).trigger,
                "target_tokens": budget_for(self.config).target,
                "hard_limit_tokens": budget_for(self.config).hard,
                "mode": "model_self_compaction_v1",
                "text_projection_version": "public_text_projection_v1" if self.compactor else None,
                "budget_policy": "soft_limits_preserve_pending_v2" if self.compactor else None,
            },
            "path_recall_version": "explored_path_recall_v1" if self.config.path_recall_enabled else None,
            "provider_parameters": redact(dict(self.provider.profile.parameters))
                                   if getattr(self.provider, "profile", None) else {},
            "apc_handoff_policy": "bound_verified_intermediate_v1",
        }
        (self.run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _record_observation(self, raw: dict[str, Any], *, navigation_substep=None) -> dict[str, Any]:
        raw = terminal_observation(dict(raw))
        public = public_observation(raw, self.config.interface_mode)
        self.audit.append(Stage.OBSERVED, Visibility.AGENT_VISIBLE,
            {**public, **({'navigation_substep': navigation_substep} if navigation_substep is not None else {})}, step=self.step_number)
        private = raw.get("_private")
        if private:
            self.audit.append(Stage.OBSERVED, Visibility.SERVER_PRIVATE, private, step=self.step_number)
        # Observer-only views (TPV/BEV): shown to the researcher, audited as
        # evaluator-private, and NEVER part of the model-facing observation.
        observer = raw.get("_observer")
        self.observer_views = observer or None
        if observer:
            self.audit.append(Stage.OBSERVED, Visibility.EVALUATOR_PRIVATE, {
                "observer_views": observer,
            }, step=self.step_number)
        self.observation = public
        if self.visual_history is not None:
            self.visual_history.observe(public, self.step_number,
                self.recent_results[-1] if self.recent_results else None)
        if self.task_support is not None:
            self.task_support.register(public['observation_id'])
        if self.spatial_memory is not None:
            if self.config.fog_gt_enabled:
                geometry = raw.get('_fog_geometry')
                self._last_fog_geometry=geometry
                self.spatial_memory.observe_geometry(geometry, public['observation_id'], self.step_number,
                    self.recent_results[-1] if self.recent_results else None)
                self.spatial_memory.sync_memory(self.memory.snapshot())
                self.audit.append(Stage.OBSERVED, Visibility.SERVER_PRIVATE,
                    {'fog_geometry': geometry}, step=self.step_number)
            else:
                self.spatial_memory.observe(public, self.step_number,
                    self.recent_results[-1] if self.recent_results else None)
            self._save_spatial_memory()
        self.memory.register_observation(public['observation_id'])
        self.apc.set_focus(raw.get("focused_subtask"))
        return public

    def _save_spatial_memory(self):
        if self.spatial_memory is None:
            return
        path = self.run_dir / 'spatial-memory.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.spatial_memory.state(), ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)

    def start(self) -> dict[str, Any]:
        with self._step_lock:
            return self._start_impl()

    def _start_impl(self) -> dict[str, Any]:
        with self._lock:
            self.status = "loading"
        try:
            raw = self.environment.reset(
                self.config.scenario_id, self.config.seed, self.config.interface_mode,
                house_index=self.config.house_index,
                agent_mode=self.config.agent_mode,
            )
        except Exception as error:
            with self._lock:
                self.status = "boot_failed"
                self.boot_error = f"{type(error).__name__}: {error}"
                self.audit.append(Stage.RUN_STATE, Visibility.EVALUATOR_PRIVATE, {
                    "status": self.status, "error": self.boot_error,
                }, step=0)
            raise
        with self._lock:
            self.status = "running"
            try:
                public = self._record_observation(raw)
            except Exception as error:
                self.status = "boot_failed"
                self.boot_error = f"{type(error).__name__}: {error}"
                self.audit.append(Stage.RUN_STATE, Visibility.EVALUATOR_PRIVATE,
                    {"status": self.status, "error": self.boot_error}, step=0)
                raise
            if public.get("episode_done"):
                self.status = "completed"
            # APC 秒表只在智能体真正开始行动后才计时：加载后先冻结,
            # 首个 step 再启动,避免"载入即空烧 300 秒"。
            self.apc.pause()
            bound_digest = getattr(self.environment, "bound_digest", None)
            self.audit.append(Stage.RUN_STATE, Visibility.EVALUATOR_PRIVATE, {
                "status": self.status, "bound_scenario_digest": bound_digest,
            }, step=0)
            if bound_digest:
                self._update_manifest({"scenario_digest_bound": bound_digest})
            self._checkpoint_dispatch = False
            self._save_checkpoint('initial')
            return public

    def start_async(self) -> None:
        """Boot the environment off-request; slow Unity binds must not block HTTP."""
        def boot() -> None:
            try:
                self.start()
            except Exception:
                pass  # status/boot_error already recorded by start()
        threading.Thread(target=boot, daemon=True, name=f"boot-{self.run_id}").start()

    def _update_manifest(self, extra: dict[str, Any]) -> None:
        path = self.run_dir / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest.update(extra)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _apply_apc_if_due(self, force: bool = False) -> dict[str, Any] | None:
        if self.config.run_mode != RunMode.ADAPTIVE:
            return None
        # 自动交接开关：关闭时只计时,超时不自动交接;人工 force 始终生效。
        if not force and not self.config.apc_auto:
            return None
        if not force and not self.apc.should_intervene():
            return None
        subtask = self.apc.focus.subtask_id if self.apc.focus else None
        if not subtask:
            return None
        self.audit.append(Stage.ASSIST_TRIGGERED, Visibility.EVALUATOR_PRIVATE, {
            "subtask_id": subtask,
            "monitor": self.apc.snapshot(),
            "reason": "manual_debug" if force else "focused_subtask_timeout",
            "pre_observation_id": self.observation.get("observation_id") if self.observation else None,
        }, step=self.step_number)
        handoff = self.environment.apply_handoff(subtask)
        if handoff.get("applied") is False:
            self.audit.append(Stage.HANDOFF_SKIPPED, Visibility.EVALUATOR_PRIVATE,
                              handoff, step=self.step_number)
            self.apc.set_focus(None)
            self.apc.set_focus(handoff.get("next_subtask") or subtask)
            return handoff
        refreshed = handoff.pop("observation", None)
        self.formal_sr_eligible = False
        self.assist_count += 1
        assist_id = str(handoff["assist_id"])
        memory_facts = self.memory.add_handoff(
            assist_id, list(handoff.get("information_grants", [])), self.step_number,
        )
        for capability_id in handoff.get("capabilities", []):
            if capability_id in self.capability_status:
                self.capability_status[capability_id] = "failed_or_timed_out"
        self.tainted_capabilities.update(str(item) for item in handoff.get("taint_lineage", []))
        self.audit.append(Stage.HANDOFF_APPLIED, Visibility.PRIVILEGED_ASSIST, handoff, step=self.step_number)
        self.audit.append(Stage.MEMORY_UPDATED, Visibility.PRIVILEGED_ASSIST, {
            "assist_id": assist_id, "facts": [asdict(fact) for fact in memory_facts],
        }, step=self.step_number)
        # Force a FRESH stopwatch even when the next focus is the same
        # subtask — otherwise the expired timer re-triggers a handoff on
        # every subsequent step (observed: 21 back-to-back assists).
        self.apc.set_focus(None)
        self.apc.set_focus(handoff.get("next_subtask"))
        if refreshed is not None:
            if self.spatial_memory is not None:
                self.spatial_memory.new_chart()  # privileged relocation has no public transform
            self._record_observation(refreshed)
        return handoff

    def terminate(self, reason: str = "user_terminated") -> None:
        self.cancel_provider_request()
        with self._lock:
            if self.status in ("completed", "stopped", "boot_failed"):
                return
            self.status = "stopped"
            self.apc.pause()
            self._episode_pause()
            self.audit.append(Stage.RUN_STATE, Visibility.EVALUATOR_PRIVATE, {
                "status": self.status, "reason": reason,
            }, step=self.step_number)

    def _episode_elapsed(self) -> float:
        total = self._episode_active_seconds
        if self._episode_started_at is not None:
            total += time.monotonic() - self._episode_started_at
        return total

    def _episode_resume(self) -> None:
        if self._episode_started_at is None:
            self._episode_started_at = time.monotonic()

    def _episode_pause(self) -> None:
        if self._episode_started_at is not None:
            self._episode_active_seconds += time.monotonic() - self._episode_started_at
            self._episode_started_at = None

    def pause_clock(self) -> None:
        """Freeze both stopwatches (e.g. when the auto-run loop is stopped)."""
        with self._lock:
            self.apc.pause()
            self._episode_pause()

    def set_auto_state(self, running: bool, *, reason: str | None = None,
                       error: Exception | None = None) -> None:
        """Keep background-loop state distinct from a resumable episode."""
        with self._lock:
            if isinstance(error, ModelOutputRejected):
                error = error.original_error
            self.auto_running = running
            self.auto_stop_reason = reason
            self.auto_error = (redact({"type": type(error).__name__,
                                       "message": str(error),
                                       "step": self.step_number})
                               if error is not None else None)
            if not running:
                self.apc.pause()
                self._episode_pause()
            self.audit.append(Stage.RUN_STATE, Visibility.EVALUATOR_PRIVATE, {
                "status": self.status, "auto_running": running,
                "auto_stop_reason": reason, "auto_error": self.auto_error,
            }, step=self.step_number)
        if not running and not self.checkpoint_reconciliation_required:
            self._save_checkpoint('auto_paused')

    def force_debug_handoff(self) -> dict[str, Any]:
        with self._step_lock:
            if self.maintenance_hold:
                raise RuntimeError('正在准备维护，暂不接受新动作')
            try:
                result = self._force_debug_handoff_impl()
            except Exception:
                if self._checkpoint_dispatch:
                    self.checkpoint_reconciliation_required = True
                raise
            self._checkpoint_dispatch = False
            self._save_checkpoint('handoff_committed')
            return result

    def _force_debug_handoff_impl(self) -> dict[str, Any]:
        with self._lock:
            handoff = self._apply_apc_if_due(force=True)
            if not handoff:
                raise RuntimeError("handoff is only available in adaptive_probe with an active subtask")
            return handoff

    def cancel_provider_request(self):
        self._request_cancel_event.set()

    def _record_provider_retry(self, state):
        with self._lock:
            self.provider_retry = {**state, 'step': self.step_number,
                'observation_id': self.observation['observation_id']}
        self.audit.append(Stage.PROVIDER_HTTP_RETRY, Visibility.EVALUATOR_PRIVATE,
            self.provider_retry, step=self.step_number)

    def _compaction_wait(self, seconds):
        self.model_waiting=True
        self.provider_retry={'phase':'memory_schema_retry','error_type':'记忆压缩格式',
            'delay_seconds':seconds,'retry_at_unix':time.time()+seconds,
            'retry':1,'max_retries':1,'step':self.step_number}
        try:
            if self._request_cancel_event.wait(seconds):
                raise ProviderRequestCancelled('memory compaction retry cancelled')
        finally:
            self.model_waiting=False
            self.provider_retry=None

    def _compact_complete(self, messages, *, output_token_multiplier=1):
        # Exactly the same provider instance/model/effort as environment decisions.
        self.model_waiting=True
        try:
            if getattr(self.provider,'supports_retry_control',False):
                return self.provider.complete(messages,cancel_event=self._request_cancel_event,
                    on_retry=self._record_provider_retry,output_token_multiplier=output_token_multiplier)
            if output_token_multiplier != 1:
                return self.provider.complete(messages,output_token_multiplier=output_token_multiplier)
            return self.provider.complete(messages)
        finally:
            self.model_waiting=False
            self.provider_retry=None

    def step_once(self, *, output_token_multiplier: int = 1, cancel_event=None) -> dict[str, Any]:
        from agentlab.checkpoints import journal
        with self._step_lock:
            if self.maintenance_hold:
                raise RuntimeError('正在准备维护，暂不接受新决策')
            if self.checkpoint_reconciliation_required:
                raise RuntimeError('最后动作或检查点待核对；禁止盲目重放，请先检查现场与事务记录')
            journal(self, 'deciding')
            try:
                result = self._step_once_impl(output_token_multiplier=output_token_multiplier, cancel_event=cancel_event)
            except Exception as error:
                known_no_execution = (isinstance(error, TargetGroundingError)
                    and error.resolution.get('execution_started') is False)
                pending_selection = (self._gt_navigation_pending is not None
                    and not self.navigation_reconciliation_required
                    and not self.path_recall_reconciliation_required)
                if known_no_execution or pending_selection:
                    self._checkpoint_dispatch = False
                if self._checkpoint_dispatch:
                    self.checkpoint_reconciliation_required = True
                else:
                    self._save_checkpoint('paused_after_error')
                raise
            self._checkpoint_dispatch = False
            self._save_checkpoint('decision_committed')
            return result

    def _save_checkpoint(self, reason):
        if not self._checkpoint_enabled:
            return
        from agentlab.checkpoints import save
        try:
            save(self, reason, internal=True)
        except Exception as error:
            self.checkpoint_info = {'error': str(error), 'step': self.step_number,
                                    'mode': 'live_world_only', 'cold_restore_supported': False}
            self.checkpoint_save_pending = not (
                self._checkpoint_dispatch or self.navigation_reconciliation_required
                or self.path_recall_reconciliation_required)
            # A successful action must not be repeated after a failed disk write.
            self.checkpoint_reconciliation_required = True
            raise RuntimeError('现场保存失败；已暂停，禁止重复最后一步: ' + str(error)) from error

    def save_checkpoint(self):
        from agentlab.checkpoints import save
        if not self._step_lock.acquire(blocking=False):
            raise RuntimeError('本步仍在执行；请先停止并等待动作回执')
        try:
            # Retry only a failed disk save of an already known outcome. This
            # never clears an uncertain environment dispatch or executes it.
            retry = self.checkpoint_save_pending and not self._checkpoint_dispatch
            if retry:
                self.checkpoint_reconciliation_required = False
                self.checkpoint_save_pending = False
            try:
                return save(self)
            except Exception:
                if retry:
                    self.checkpoint_save_pending = True
                    self.checkpoint_reconciliation_required = True
                raise
        finally:
            self._step_lock.release()

    def prepare_maintenance(self):
        if not self._step_lock.acquire(blocking=False):
            raise RuntimeError('本步尚未结束，不能重启评测服务')
        try:
            if self.auto_running or self.model_waiting or self.maintenance_hold:
                raise RuntimeError('运行仍活跃或正在维护，不能重复接管')
            self.maintenance_hold = uuid.uuid4().hex
            try:
                info = self.save_checkpoint()
                return dict(checkpoint=info, maintenance_token=self.maintenance_hold)
            except Exception:
                self.maintenance_hold = None
                raise
        finally:
            self._step_lock.release()

    def release_maintenance(self, token):
        with self._step_lock:
            if not token or token != self.maintenance_hold:
                raise RuntimeError('维护令牌不匹配')
            self.maintenance_hold = None
            self._save_checkpoint('maintenance_cancelled')

    def _step_once_impl(self, *, output_token_multiplier: int = 1, cancel_event=None) -> dict[str, Any]:
        # `_step_lock` serializes agent steps; `_lock` only guards short state
        # mutations so snapshot() polling stays responsive during the (slow)
        # provider call and environment execution.
        with self._step_lock:
            self._request_cancel_event = cancel_event if cancel_event is not None else threading.Event()
            if self._request_cancel_event.is_set():
                raise ProviderRequestCancelled('model request cancelled before step')
            if self.path_recall_reconciliation_required:
                raise RuntimeError('path recall interrupted; reconcile native pose and receipt before resuming')
            if self.navigation_reconciliation_required:
                raise RuntimeError('navigation dispatch interrupted; reconcile native receipt before resuming')
            health_check = getattr(self.environment, "assert_ready", None)
            if callable(health_check):
                health_check()
            if self._gt_navigation_pending is not None:
                if self.status != 'running':raise RuntimeError('run is not active')
                self.auto_error=None;self.auto_stop_reason=None
                self.apc.resume();self._episode_resume()
                return self._execute_gt_navigation(*self._gt_navigation_pending)
            with self._lock:
                if self.status == "loading":
                    raise RuntimeError("environment is still loading; wait for status=running")
                if self.status != "running" or not self.observation:
                    raise RuntimeError("run is not active")
                if not self.auto_running:
                    self.auto_error = None
                    self.auto_stop_reason = None
                # 首个 step 起(以及每次停止后再走)启动/恢复两个秒表。
                self.apc.resume()
                self._episode_resume()
                self._apply_apc_if_due()
                current = self.observation
                controller_action = None  # Every primary step starts with exactly one model decision.
                map_started = time.perf_counter()
                spatial_context = self.spatial_memory.context() if self.spatial_memory else None
                map_ms = (time.perf_counter() - map_started) * 1000
                messages, context_manifest = self.context_builder.build(
                    current, self.memory.context_view(query=str((self.task_support.goal or {}).get('goal','')) if self.task_support else ''), self.config.interface_mode, self.recent_results,
                    **({'gt_navigation_enabled': True} if self.config.gt_navigation_enabled else {}),
                    **({'model_compaction_enabled': True} if self.compactor else {}),
                    **({'spatial_memory': spatial_context} if spatial_context is not None else {}),
                    **({'history_frames': self.visual_history.recent(current['observation_id'], self.config.visual_frame_count),
                        'visual_frame_count': self.config.visual_frame_count} if self.visual_history else {}),
                    **({'task_support': {**self.task_support.context(self.recent_results),
                        'remaining_decision_steps': max(0,self.auto_step_limit-self.step_number) if self.auto_step_limit else None}} if self.task_support else {}),
                    **({'navigation_state': dict(self.navigation.state)} if self.navigation else {}),
                    **({'path_recall_state': {'version':'explored_path_recall_v1',
                        'enabled': True, 'last_return': self.last_recall, 'current_map_observation_id':current['observation_id']}}
                       if self.path_recall else {}),
                )
                if spatial_context is not None:
                    context_manifest['spatial_build_ms'] = round(map_ms, 2)
            if not controller_action:
                if self.compactor:
                    messages,context_manifest=self.compactor.prepare(messages,context_manifest,self.step_number,
                        self._compact_complete,lambda stage,payload:self.audit.append(stage,Visibility.AGENT_VISIBLE,
                            payload,step=self.step_number),wait=self._compaction_wait,
                        output_budget=getattr(self.provider,'output_budget',None))
                self.audit.append(Stage.CONTEXT_BUILT, Visibility.AGENT_VISIBLE, context_manifest, step=self.step_number)
            budget_reader = getattr(self.provider, "output_budget", None)
            output_budget = (budget_reader(output_token_multiplier)
                             if callable(budget_reader) else {})
            if not controller_action:
                self.audit.append(Stage.API_REQUESTED, Visibility.EVALUATOR_PRIVATE, {
                    "provider": self.config.provider_name,
                    "model": self.config.provider_model,
                    "message_count": len(messages),
                    "observation_id": current["observation_id"],
                    "output_budget": output_budget,
                }, step=self.step_number)
            step_stat = None
            parsing_output = False
            parsed_json = False
            finish_reason = None
            try:
                self.model_waiting = not bool(controller_action)
                try:
                    if controller_action:
                        from agentlab.providers.base import ProviderResult
                        result = ProviderResult(content=json.dumps(dict(action=controller_action,
                            decision_summary=_runner_message("feedback_01"),
                            actor='navigation_controller', navigation_state=self.navigation.state),ensure_ascii=False),
                            raw={'actor':'navigation_controller','no_provider_call':True})
                    elif getattr(self.provider, 'supports_retry_control', False):
                        result = self.provider.complete(messages, output_token_multiplier=output_token_multiplier,
                            cancel_event=self._request_cancel_event, on_retry=self._record_provider_retry)
                    elif output_token_multiplier != 1 and output_budget:
                        result = self.provider.complete(
                            messages, output_token_multiplier=output_token_multiplier)
                    else:
                        result = self.provider.complete(messages)
                finally:
                    self.model_waiting = False
                    self.provider_retry = None
                raw_ref = self.audit.save_json_artifact(result.raw)
                choices = result.raw.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    finish_reason = choices[0].get("finish_reason")
                self.audit.append(Stage.CONTROLLER_DECIDED if controller_action else Stage.MODEL_RESPONDED, Visibility.AGENT_OUTPUT, {
                    "content": result.content,
                    "usage": result.usage,
                    "latency_ms": round(result.latency_ms, 1),
                    "raw_response_artifact": raw_ref,
                    "finish_reason": finish_reason,
                    "actor": 'navigation_controller' if controller_action else 'model',
                    "navigation_action": bool(controller_action),
                }, step=self.step_number)
                transport = result.raw.get("_transport_audit", {})
                if transport:
                    self.audit.append(Stage.PROVIDER_TRANSPORT_COMPLETED, Visibility.EVALUATOR_PRIVATE, {
                        "transport_completed": True, "transport_audit": transport,
                        "observation_id": current["observation_id"],
                    }, step=self.step_number)
                usage = result.usage or {}
                if self.compactor and not controller_action:
                    self.compactor.meter.calibrate(messages,usage)
                step_stat = {
                    "step": self.step_number,
                    "observation_id": current["observation_id"],
                    "latency_ms": round(result.latency_ms, 1),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "context_characters": context_manifest.get("character_count"),
                    "output_budget": output_budget,
                    "finish_reason": finish_reason,
                    "transport_audit": transport,
                    "actor": 'navigation_controller' if controller_action else 'model',
                    "navigation_action": bool(controller_action),
                    **({'spatial_build_ms': round(map_ms, 2)} if spatial_context is not None else {}),
                }
                with self._lock:
                    self.step_stats.append(step_stat)
                parsing_output = True
                repairs: list[dict[str, Any]] = []
                parsed = parse_model_json(result.content, repairs=repairs)
                parsed_json = True
                if finish_reason == 'length':
                    raise ValueError('provider marked decision output as truncated; no action executed')
                if not controller_action:
                    from agentlab.agent.output_contract import required_feature_fields
                    required = required_feature_fields(
                        task_support=self.task_support is not None,
                        rgb_mapping=self.config.spatial_memory_enabled,
                        navigation=self.navigation is not None,
                        path_recall=self.path_recall, gt_navigation=self.config.gt_navigation_enabled)
                    missing=[key for key in required if key not in parsed]
                    if missing:
                        raise ValueError('enabled feature output fields missing: '+', '.join(missing))
                    if self.task_support and not isinstance(parsed['plan_update'],dict):
                        raise ValueError('plan_update must be an object when task support is enabled')
                    step_stat['feature_adherence']={'required_fields':required,'present':True}
                for repair in repairs:
                    self.audit.append(Stage.MODEL_OUTPUT_REPAIRED, Visibility.EVALUATOR_PRIVATE, {
                        **repair, "raw_response_artifact": raw_ref,
                    }, step=self.step_number)
                    with self._lock:
                        step_stat["format_repair"] = {
                            "strategy": repair["strategy"], "appended": repair["appended"],
                        }
                self.audit.append(Stage.RESPONSE_PARSED, Visibility.AGENT_OUTPUT, parsed, step=self.step_number)
                decision_type = parsed.get('decision_type', 'action')
                if decision_type not in ('action', 'navigate', 'return_to_visited', 'gt_navigate'):
                    raise ValueError('decision_type must be action, navigate, return_to_visited or gt_navigate')
                if decision_type != 'gt_navigate' and parsed.get('gt_navigation_request') is not None:
                    raise ValueError('non-GT decisions require gt_navigation_request=null')
                if decision_type == 'gt_navigate':
                    req=parsed.get('gt_navigation_request')
                    if not self.config.gt_navigation_enabled or parsed.get('action') is not None:
                        raise ValueError('GT navigation must be enabled and action=null')
                    if (not isinstance(req,dict) or set(req)!={'intent'} or
                            not isinstance(req['intent'],str) or not 0<len(req['intent'])<=500):
                        raise ValueError('gt_navigation_request requires a short intent only')
                    if parsed.get('return_request') is not None or parsed.get('navigation_request') is not None:
                        raise ValueError('GT navigation cannot be combined with another request')
                    action=None
                elif decision_type == 'navigate':
                    if self.navigation is None:
                        raise ValueError('navigation is disabled in this run')
                    if parsed.get('action') is not None or not isinstance(parsed.get('navigation_request'), dict):
                        raise ValueError('navigate requires action=null and a navigation_request object; no fallback action')
                    action = None
                elif decision_type == 'return_to_visited':
                    if not self.path_recall:
                        raise ValueError('path recall is disabled in this run')
                    if parsed.get('action') is not None or not isinstance(parsed.get('return_request'), dict):
                        raise ValueError('return_to_visited requires action=null and return_request')
                    req=parsed['return_request']
                    if set(req)!={'node_id','map_observation_id','reason'} or any(
                            not isinstance(req[k],str) or not req[k].strip() or len(req[k])>500 for k in req):
                        raise ValueError('return_request requires only nonempty node_id, map_observation_id and reason strings (max 500)')
                    if parsed.get('navigation_request') is not None:
                        raise ValueError('return_to_visited cannot contain a navigation_request')
                    action = None
                else:
                    if parsed.get('return_request') is not None:
                        raise ValueError('action requires return_request=null')
                    if parsed.get('navigation_request') is not None:
                        raise ValueError('action requires navigation_request=null; action and navigation are mutually exclusive')
                    action = AgentAction.from_dict(parsed["action"])
                    validate_action_for_interface(action, self.config.interface_mode, current["observation_id"])
                    if 'available_actions' in current and action.verb not in current['available_actions']:
                        raise ValueError('action is not implemented by the current global interface')
                step_stat['decision_type'] = decision_type
                self.audit.append(Stage.ACTION_VALIDATED, Visibility.EVALUATOR_PRIVATE, {
                    "valid": True, "decision_type": decision_type,
                    "action": action.to_dict() if action else None,
                }, step=self.step_number)
            except Exception as error:
                self.audit.append(Stage.ACTION_VALIDATED, Visibility.EVALUATOR_PRIVATE, {
                    "valid": False, "error_type": type(error).__name__, "message": str(error),
                    "transport_audit": getattr(error, "transport_audit", None),
                }, step=self.step_number)
                with self._lock:
                    if step_stat is not None:
                        step_stat["validation_error"] = redact({
                            "error_type": type(error).__name__, "message": str(error),
                        })
                    if parsing_output and finish_reason != 'length':
                        self.recent_results.append({"success": False, "message": f"Agent output rejected: {error}"})
                if parsing_output and isinstance(error, (ValueError, TypeError, KeyError)):
                    raise ModelOutputRejected(
                        error, output_truncated=(finish_reason == "length")
                    ) from error
                raise

            if self._request_cancel_event.is_set():
                raise ProviderRequestCancelled('stopped before environment dispatch')
            begin_safety = getattr(self.environment, 'begin_safety_decision', None)
            if begin_safety:
                try:
                    begin_safety(f'{self.run_id}:{self.step_number}',
                                 action.verb if action is not None else decision_type)
                except TargetGroundingError as error:
                    self.audit.append(Stage.TARGET_RESOLVED, Visibility.SERVER_PRIVATE, {
                        **error.resolution, 'observation_id': current['observation_id'],
                        'counts_as_model_action_failure': False,
                    }, step=self.step_number)
                    raise
            if decision_type == 'return_to_visited':
                return self._execute_path_recall(parsed, current, step_stat)
            if decision_type == 'gt_navigate':
                return self._execute_gt_navigation(parsed,current,step_stat)
            if action is None:
                return self._execute_navigation(parsed, current, step_stat)
            if self.navigation:
                self.navigation.clear_recent_views()
            try:
                outcome = self.environment.step(action)
            except TargetGroundingError as error:
                self.audit.append(Stage.TARGET_RESOLVED, Visibility.SERVER_PRIVATE, {
                    **error.resolution, "observation_id": current["observation_id"],
                    "execution_started": False, "counts_as_model_action_failure": False,
                }, step=self.step_number)
                raise
            return self._commit_outcome(action, current, parsed, outcome, step_stat)

    def _commit_outcome(self, action, current, parsed, outcome, step_stat, *, advance_step=True, navigation_substep=None, navigation_kind='navigate'):
        """Record one verified outcome, then advance memory and observation.

        The caller owns _step_lock. This method never invokes the model or
        dispatches an environment action, including during receipt recovery.
        """
        # Validate before changing counters or emitting a committed action event.
        outcome = dict(outcome, observation=terminal_observation(dict(outcome["observation"])))
        self.audit.append(Stage.TARGET_RESOLVED, Visibility.SERVER_PRIVATE, {
            **outcome.get("target_resolution", {}),
            "observation_id": current["observation_id"],
        }, step=self.step_number)
        public_result = dict(outcome.get("public_result", {}))
        public_result["action"] = action.to_dict()
        self.primitive_action_count += 1
        public_result['primitive_action_index'] = self.primitive_action_count
        if navigation_substep is not None:
            public_result['navigation_substep'] = navigation_substep
            public_result['decision_type'] = navigation_kind
        if step_stat is not None:
            step_stat['primitive_actions'] = step_stat.get('primitive_actions', 0) + 1
        if self.navigation:
            public_result['navigation_state'] = dict(self.navigation.state)
        public_result["ego_state_before"] = dict(current.get("ego_state") or {})
        public_result["ego_state_after"] = dict(public_observation(
            outcome["observation"], self.config.interface_mode).get("ego_state") or {})
        result_event = self.audit.append(Stage.ACTION_EXECUTED, Visibility.AGENT_VISIBLE, public_result, step=self.step_number)
        with self._lock:
            if step_stat is not None:
                step_stat.update({
                    "verb": action.verb,
                    "success": bool(public_result.get("success")),
                })
        self.audit.append(Stage.WORLD_STATE_UPDATED, Visibility.SERVER_PRIVATE, {
            "diff": outcome.get("world_state_diff", {}),
        }, step=self.step_number)
        with self._lock:
            if self.visual_history:
                self.visual_history.annotate(current['observation_id'], parsed, public_result)
            if self.task_support:
                self.task_support.update(parsed, current['observation_id'], public_result)
            spatial_updates = (self.spatial_memory.update_semantics(parsed,
                current['observation_id'], self.step_number) if self.spatial_memory else [])
            memory_facts = self.memory.apply_agent_updates(
                list(parsed.get("memory_updates", [])), current["observation_id"], self.step_number,
            )
            memory_facts.extend(self.memory.record_action_result(
                result_event, action.verb, current['observation_id'], self.step_number,
            ))
        if self.config.fog_gt_enabled:
            with self._lock:
                spatial_updates = self.spatial_memory.sync_memory(self.memory.snapshot(), parsed.get('map_links', []))
        self.audit.append(Stage.MEMORY_UPDATED, Visibility.EVALUATOR_PRIVATE, {
            "accepted_facts": [asdict(fact) for fact in memory_facts],
            **({'accepted_spatial_updates': spatial_updates} if self.spatial_memory else {}),
        }, step=self.step_number)
        with self._lock:
            for evidence in outcome.get("capability_evidence", []):
                capability_id = evidence.get("capability_id")
                if capability_id in self.capability_status:
                    status = evidence.get("status", "not_reached")
                    if capability_id in self.tainted_capabilities and status == "passed_unassisted":
                        status = "passed_after_assist"
                    self.capability_status[capability_id] = status
            metrics = dict(outcome.get("metrics", {}))
            task_success = (metrics["task_success"] is True
                            if "task_success" in metrics else
                            metrics.get("true_goal_completion") == 1.0)
            metrics.update({
                "formal_sr_eligible": self.formal_sr_eligible,
                "formal_success": task_success and self.formal_sr_eligible,
                "assisted_goal_completion": (
                    (float(metrics["true_goal_completion"]) if metrics.get("true_goal_completion") is not None else None) if self.assist_count else 0.0
                ),
                "assist_count": self.assist_count,
                "tainted_capabilities": sorted(self.tainted_capabilities),
                "capabilities": self.capability_status,
            })
        self.audit.append(Stage.CAPABILITY_SCORED, Visibility.EVALUATOR_PRIVATE, metrics, step=self.step_number)
        with self._lock:
            self.recent_results.append(public_result)
            if advance_step:
                self.step_number += 1
            next_raw = outcome["observation"]
            next_public = self._record_observation(next_raw, navigation_substep=navigation_substep)
            if self.spatial_memory and step_stat is not None:
                step_stat['perception_ms']=self.spatial_memory.last_inference_ms
            if next_public.get("episode_done"):
                self.status = "completed"
                self.apc.pause()
                self._episode_pause()
                self.audit.append(Stage.RUN_STATE, Visibility.EVALUATOR_PRIVATE, {
                    "status": self.status, "formal_sr_eligible": self.formal_sr_eligible,
                }, step=self.step_number)
        return {"observation": next_public, "public_result": public_result, "metrics": metrics}

    def _execute_navigation(self, parsed, initial, step_stat):
        """One accepted model decision, bounded audited primitive executions.

        No fallback ordinary action, no extra provider call, no environment
        retries. Any ambiguous environment exception stops this decision and
        propagates; only verified receipts are committed.
        """
        from agentlab.agent.pixel_navigation import NavigationView, MAX_ACTIONS, ARRIVAL_RADIUS_M
        decision_step = self.step_number
        receipts = []
        outcome_result = None
        summary = None
        dispatch_pending = False
        step_stat.update(navigation_action=True, decision_committed=True)
        try:
            # Input validation may fail before request() is entered.
            self.navigation.reset_request()
            try:
                view = NavigationView.from_public(initial)
                accepted = self.navigation.request(parsed['navigation_request'], view)
            except (ValueError, OSError) as error:
                self.navigation.stop('navigation_input_unavailable: ' + str(error))
                accepted = False
            for index in range(MAX_ACTIONS) if accepted else ():
                if self._request_cancel_event.is_set():
                    self.navigation.stop('user_stopped'); break
                action_dict = self.navigation.next_action(NavigationView.from_public(self.observation))
                if not action_dict:
                    break
                action = AgentAction.from_dict(action_dict)
                validate_action_for_interface(action, self.config.interface_mode, self.observation['observation_id'])
                if action.verb not in self.observation.get('available_actions', {action.verb}):
                    self.navigation.stop('primitive_unavailable'); break
                self.audit.append(Stage.CONTROLLER_DECIDED, Visibility.AGENT_OUTPUT, {
                    'actor': 'navigation_controller', 'version': self.navigation.state['version'],
                    'navigation_substep': index+1, 'source_observation_id': self.observation['observation_id'],
                    'action': action_dict, 'navigation_state': dict(self.navigation.state),
                }, step=decision_step)
                current = self.observation
                # Deliberately outside all retry loops. A thrown dispatch must
                # be reconciled against the native receipt before any resume.
                dispatch_pending = True
                outcome = self.environment.step(action)
                self.navigation.accept_receipt(outcome.get('public_result') or {})
                outcome_result = self._commit_outcome(action, current, parsed if index == 0 else {'actor':'navigation_controller', 'decision_summary':_runner_message("feedback_02")},
                    outcome, step_stat, advance_step=False, navigation_substep=index+1)
                receipts.append(outcome_result['public_result'])
                dispatch_pending = False
                if self.status != 'running':
                    self.navigation.stop('episode_ended'); break
            if accepted and self.navigation.state.get('estimated_destination_m') is not None:
                import math
                final_view = NavigationView.from_public(self.observation)
                destination = self.navigation.state['estimated_destination_m']
                remaining = math.hypot(destination[0]-final_view.x, destination[1]-final_view.z)
                self.navigation.state['estimated_remaining_m'] = round(remaining, 3)
            if self.navigation.goal is not None:
                reached = remaining <= ARRIVAL_RADIUS_M
                self.navigation.stop('waypoint_reached_not_task_completion' if reached else 'bounded_horizon_completed')
            summary = dict(success=self.navigation.state.get('reason') == 'waypoint_reached_not_task_completion',
                action={'verb':'navigate', 'target':parsed['navigation_request'], 'parameters':{}},
                feedback_code=self.navigation.state.get('reason'),
                message=_runner_message("feedback_03")+str(self.navigation.state.get('reason'))+_runner_message("feedback_04"),
                accepted=accepted, primitive_actions=len(receipts),
                navigation_state=dict(self.navigation.state),
                actual={'distance_m': round(self.navigation.state.get('travelled_m', 0.), 3)},
                source_observation_id=initial['observation_id'],
                final_observation_id=self.observation['observation_id'])
            event = self.audit.append(Stage.NAVIGATION_COMPLETED, Visibility.AGENT_VISIBLE, summary, step=decision_step)
            with self._lock:
                if not receipts:
                    facts = self.memory.apply_agent_updates(list(parsed.get('memory_updates', [])), initial['observation_id'], decision_step)
                    if self.spatial_memory:
                        self.spatial_memory.update_semantics(parsed, initial['observation_id'], decision_step)
                    if self.visual_history:
                        self.visual_history.annotate(initial['observation_id'], parsed, summary)
                else:
                    facts = []
                facts.extend(self.memory.record_action_result(event, 'navigate', initial['observation_id'], decision_step))
                self.audit.append(Stage.MEMORY_UPDATED, Visibility.EVALUATOR_PRIVATE,
                    {'accepted_facts': [asdict(f) for f in facts]}, step=decision_step)
                if self.task_support:
                    self.task_support.update({} if receipts else parsed, initial['observation_id'], summary)
                self.recent_results.append(summary)
                step_stat.update(verb='navigate', success=summary['success'], navigation_summary=summary)
            return {'observation': self.observation, 'public_result': summary,
                    'metrics': outcome_result['metrics'] if outcome_result else {}}
        except Exception as error:
            self.navigation_reconciliation_required = dispatch_pending
            self.navigation.stop('execution_interrupted_requires_receipt_reconciliation' if dispatch_pending else 'controller_interrupted_before_dispatch')
            self.audit.append(Stage.NAVIGATION_COMPLETED, Visibility.EVALUATOR_PRIVATE, {
                'interrupted': True, 'verified_primitive_actions':len(receipts),
                'error':str(error), 'requires_receipt_reconciliation':dispatch_pending,
            }, step=decision_step)
            raise
        finally:
            with self._lock:
                # Accepted choices, including rejected/blocked floor goals,
                # consume one decision. Never count each internal primitive.
                self.step_number = decision_step+1
                self.audit.append(Stage.OBSERVED, Visibility.AGENT_VISIBLE, self.observation, step=self.step_number)
                if self.observer_views:
                    self.audit.append(Stage.OBSERVED, Visibility.EVALUATOR_PRIVATE,
                        {'observer_views': self.observer_views}, step=self.step_number)

    def _select_gt_navigation(self, messages, prepared, decision_step):
        """Recover truncation using the same views/ticket, before planning or motion."""
        from agentlab.agent.gt_navigation import validate_selection
        multipliers=(1,2,16)
        budget_reader=getattr(self.provider,'output_budget',None)
        for index,multiplier in enumerate(multipliers):
            result=(self._compact_complete(messages) if multiplier==1 else
                    self._compact_complete(messages,output_token_multiplier=multiplier))
            choices=result.raw.get('choices') or []
            finish_reason=choices[0].get('finish_reason') if choices else None
            selector_stat=dict(step=decision_step,actor='waypoint_selector',
                latency_ms=result.latency_ms,output_token_multiplier=multiplier,
                finish_reason=finish_reason,
                **{k:result.usage.get(k) for k in ('prompt_tokens','completion_tokens')})
            self.step_stats.append(selector_stat)
            self.audit.append(Stage.GT_NAVIGATION_MODEL_OUTPUT,Visibility.AGENT_OUTPUT,
                {'content':result.content,'usage':result.usage,'latency_ms':result.latency_ms,
                 'attempt':index+1,'output_token_multiplier':multiplier,'finish_reason':finish_reason,
                 'raw_response_artifact':self.audit.save_json_artifact(result.raw)},step=decision_step)
            transport=result.raw.get('_transport_audit')
            if transport:self.audit.append(Stage.PROVIDER_TRANSPORT_COMPLETED,Visibility.EVALUATOR_PRIVATE,
                {'transport_completed':True,'transport_audit':transport,'actor':'waypoint_selector'},step=decision_step)
            if finish_reason=='length':
                selector_stat['validation_error']={'message':'provider output truncated before selection'}
                next_budget=(budget_reader(multipliers[index+1]) if index<2 and callable(budget_reader) else {})
                if not next_budget:
                    raise ProviderOutputUnavailable(
                        'GT导航选点输出在有限预算重试后仍被截断；已暂停并保留待执行决策，'
                        '未选点、未移动、未消耗决策步。不能据此判定模型选点错误。')
                self.audit.append(Stage.GT_NAVIGATION_OUTPUT_RETRY,Visibility.EVALUATOR_PRIVATE,
                    {'retry':index+1,'max_retries':2,'next_output_budget':next_budget,
                     'output_truncated':True,'same_input':True,'environment_action_executed':False},
                    step=decision_step)
                self.model_waiting=True
                self.provider_retry={'phase':'navigation_selection_output_retry','error_type':'选点输出截断',
                    'retry':index+1,'max_retries':2,'delay_seconds':20,'retry_at_unix':time.time()+20,
                    'step':decision_step}
                try:
                    if self._request_cancel_event.wait(20):
                        raise ProviderRequestCancelled('navigation selection recovery cancelled')
                finally:
                    self.model_waiting=False;self.provider_retry=None
                continue
            try:
                text=result.content.strip()
                fence=re.fullmatch(r'```(?:json)?\s*(.*?)\s*```',text,re.DOTALL|re.IGNORECASE)
                return validate_selection(json.loads(fence.group(1) if fence else text,object_pairs_hook=_unique_json_object),
                    prepared['ticket'],[v['view_id'] for v in prepared['views']] if prepared.get('views') else None)
            except ValueError as error:
                selector_stat['validation_error']={'message':str(error)}
                return None

    def _execute_gt_navigation(self, parsed, initial, step_stat):
        """One decision, an independent selector call, then audited real walking."""
        from agentlab.agent.gt_navigation import selection_messages, VERSION
        decision_step=self.step_number
        self._gt_navigation_pending=(parsed,initial,step_stat)
        dispatch_pending=False;committed=False;receipts=[];outcome_result=None
        public_plan=None;selection_reason=None
        try:
            prepared=self.environment.prepare_gt_navigation(initial['observation_id'])
            self.audit.append(Stage.GT_NAVIGATION_PREPARED,Visibility.SERVER_PRIVATE,
                {**prepared,'source_observation_id':initial['observation_id']},step=decision_step)
            if not prepared.get('success') or not prepared.get('selectable_pixels'):
                code=prepared.get('feedback_code','gt_navigation_no_selectable_floor')
                selected=None
            else:
                messages=selection_messages(prepared,parsed,initial['observation_id'])
                self.audit.append(Stage.GT_NAVIGATION_INPUT,Visibility.AGENT_VISIBLE,
                    {'version':VERSION,'messages':messages,**({'views':prepared['views']} if prepared.get('views') else {'image':prepared['image']}),
                     'source_observation_id':initial['observation_id']},step=decision_step)
                selected=self._select_gt_navigation(messages,prepared,decision_step)
                if selected is None:code='gt_navigation_invalid_selection'
                if selected and selected.get('cancel'):
                    selection_reason=selected['reason']
                    selected=None;code='gt_navigation_cancelled'
            if self._request_cancel_event.is_set():
                raise ProviderRequestCancelled('stopped before navigation planning')
            plan=self.environment.plan_gt_navigation(selected) if selected else {'success':False,'feedback_code':code}
            committed=True;self._gt_navigation_pending=None
            step_stat.update(decision_committed=True,primitive_actions=0)
            if plan.get('success'):
                public_plan={k:plan[k] for k in ('version','target_image','pixel','floor','distance_m','planner','view_id') if k in plan}
                self.audit.append(Stage.GT_NAVIGATION_PLANNED,Visibility.AGENT_VISIBLE,public_plan,step=decision_step)
                self.audit.append(Stage.GT_NAVIGATION_PLANNED,Visibility.SERVER_PRIVATE,plan,step=decision_step)
                self.formal_sr_eligible=False;self.assist_count+=1
                self.tainted_capabilities.update(('D3.2','D3.3'))
                code='gt_navigation_primitive_limit'
                for index in range(64):
                    if self._request_cancel_event.is_set():code='gt_navigation_user_stopped';break
                    action=self.environment.next_gt_navigation_action()
                    if action is None:code='gt_navigation_arrived';break
                    validate_action_for_interface(action,self.config.interface_mode,self.observation['observation_id'])
                    if action.verb not in self.observation.get('available_actions',{action.verb}):
                        code='gt_navigation_primitive_unavailable';break
                    self.audit.append(Stage.CONTROLLER_DECIDED,Visibility.AGENT_OUTPUT,
                        {'actor':'gt_navigation_controller','version':VERSION,'navigation_substep':index+1,
                         'action':action.to_dict()},step=decision_step)
                    current=self.observation;dispatch_pending=True
                    outcome=self.environment.step(action)
                    outcome_result=self._commit_outcome(action,current,parsed if index==0 else {},outcome,step_stat,
                        advance_step=False,navigation_substep=index+1,navigation_kind='gt_navigate')
                    dispatch_pending=False;receipt=outcome_result['public_result'];receipts.append(receipt)
                    if not receipt.get('success') or receipt.get('collision'):
                        code='gt_navigation_blocked';break
                    if self.status!='running':code='gt_navigation_episode_ended';break
                    before_health=current.get('health');after_health=self.observation.get('health')
                    if (isinstance(before_health,(int,float)) and isinstance(after_health,(int,float))
                            and after_health<before_health):
                        code='gt_navigation_health_changed';break
                else:
                    if self.environment.next_gt_navigation_action() is None:code='gt_navigation_arrived'
            else:code=plan.get('feedback_code','gt_navigation_rejected')
            summary=dict(version=VERSION,decision_type='gt_navigate',success=code=='gt_navigation_arrived',
                feedback_code=code,message={'gt_navigation_arrived':_runner_message("feedback_05"),
                    'gt_navigation_blocked':_runner_message("feedback_06"),
                    'gt_navigation_health_changed':_runner_message("feedback_07"),
                    'gt_navigation_cancelled':_runner_message("feedback_08")+str(selection_reason or _runner_message("feedback_09"))+_runner_message("feedback_10"),
                    'gt_navigation_invalid_selection':_runner_message("feedback_11"),
                    'gt_navigation_endpoint_clearance_rejected':_runner_message("feedback_12"),
                    'gt_navigation_native_route_blocked':_runner_message("feedback_13"),
                    'gt_navigation_outside_corridor':_runner_message("feedback_14"),
                    'gt_navigation_no_visible_route':_runner_message("feedback_15"),
                    'gt_navigation_start_clearance_unverified':_runner_message("feedback_16"),
                    'gt_navigation_start_blocked':_runner_message("feedback_17"),
                    'gt_navigation_no_selectable_floor':_runner_message("feedback_18")}.get(code,_runner_message("feedback_19")+code),
                action={'verb':'gt_navigate','parameters':parsed['gt_navigation_request']},
                intent=parsed['gt_navigation_request']['intent'],primitive_actions=len(receipts),
                selected_view_id=(selected or {}).get('view_id'),
                travelled_m=round(sum((r.get('actual') or {}).get('distance_m',0) for r in receipts),3),
                source_observation_id=initial['observation_id'],final_observation_id=self.observation['observation_id'],
                privileged_assistance=True,model_call_protocol='decision_then_waypoint_selection')
            if code=='gt_navigation_blocked' and receipts:
                # Relay only the already-sanitized action receipt, never native
                # object IDs, debug error strings or hidden obstacle locations.
                summary['blocking_feedback_code']=receipts[-1].get('feedback_code')
                summary['message']+=_runner_message("feedback_20")+receipts[-1].get('message','')
            event=self.audit.append(Stage.NAVIGATION_COMPLETED,Visibility.AGENT_VISIBLE,summary,step=decision_step)
            facts=[]
            if not receipts:
                facts=self.memory.apply_agent_updates(list(parsed.get('memory_updates',[])),initial['observation_id'],decision_step)
            facts.extend(self.memory.record_action_result(event,'gt_navigate',initial['observation_id'],decision_step))
            self.audit.append(Stage.MEMORY_UPDATED,Visibility.EVALUATOR_PRIVATE,
                {'accepted_facts':[asdict(f) for f in facts]},step=decision_step)
            if self.task_support:self.task_support.update({} if receipts else parsed,initial['observation_id'],summary)
            if self.visual_history:self.visual_history.annotate(initial['observation_id'],parsed,summary)
            self.recent_results.append(summary);self.last_gt_navigation=summary
            if self.config.fog_gt_enabled and self.spatial_memory:
                self.spatial_memory.record_navigation_endpoint(self._last_fog_geometry,
                    self.observation['observation_id'],decision_step,summary)
                self._save_spatial_memory()
            step_stat.update(verb='gt_navigate',success=summary['success'],navigation_summary=summary)
            return {'observation':self.observation,'public_result':summary,'metrics':outcome_result['metrics'] if outcome_result else {}}
        except Exception as error:
            self.navigation_reconciliation_required=dispatch_pending
            self.audit.append(Stage.NAVIGATION_COMPLETED,Visibility.EVALUATOR_PRIVATE,
                {'version':VERSION,'interrupted':True,'error':str(error),'requires_receipt_reconciliation':dispatch_pending,
                 'selector_pending':not committed,'verified_primitive_actions':len(receipts)},step=decision_step)
            raise
        finally:
            if committed:
                self.step_number=decision_step+1
                self.audit.append(Stage.OBSERVED,Visibility.AGENT_VISIBLE,self.observation,step=self.step_number)
                if self.observer_views:self.audit.append(Stage.OBSERVED,Visibility.EVALUATOR_PRIVATE,
                    {'observer_views':self.observer_views},step=self.step_number)

    def _execute_path_recall(self, parsed, initial, step_stat):
        """One decision; a refusal preserves the input frame and a receipt links history."""
        import copy
        decision_step=self.step_number
        req=parsed['return_request']
        target=self.spatial_memory.recall_target(req['node_id'])
        outcome={}
        step_stat.update(decision_committed=True,primitive_actions=0)
        try:
            if req['map_observation_id'] != initial['observation_id']:
                result=dict(success=False,feedback_code='recall_map_stale',message=_runner_message("feedback_21"))
            elif not target:
                result=dict(success=False,feedback_code='recall_node_not_found',message=_runner_message("feedback_22"))
            else:
                self.audit.append(Stage.PATH_RECALL_CHECKED, Visibility.SERVER_PRIVATE,
                    {'phase':'prepare_requested','node_id':req['node_id'],
                     'source_observation_id':initial['observation_id']}, step=decision_step)
                try:
                    outcome=self.environment.return_to_visited({
                        'anchor_key':target['anchor_key'],'node_id':req['node_id'],
                        'current_observation_id':initial['observation_id']})
                except Exception:
                    self.path_recall_reconciliation_required=True
                    raise
                result=dict(outcome['public_result'])
                if result.get('success') and (outcome.get('observation') or {}).get('observation_id') in (None,initial['observation_id']):
                    self.path_recall_reconciliation_required=True
                    raise RuntimeError('recall success requires a fresh observation receipt; reconcile before resuming')
            node=copy.deepcopy(target['node']) if target else {'id':req['node_id']}
            if self.task_support:
                self.task_support.update(parsed,initial['observation_id'],result)
            plan=copy.deepcopy(self.task_support.goal) if self.task_support else copy.deepcopy(parsed.get('plan_update') or {})
            summary=dict(result,decision_type='return_to_visited',
                action={'verb':'return_to_visited','target':None,'parameters':{'node_id':req['node_id']}},
                node=node,reason=req['reason'],reason_origin='agent_stated_intent',
                current_goal=plan,goal_origin='agent_plan_not_environment_fact',
                previous_observation_id=initial['observation_id'],
                source_observation_id=node.get('observation_id'),
                final_observation_id=((outcome.get('observation') or {}).get('observation_id') or initial['observation_id']),
                primitive_actions=0,privileged_assistance=True,
                completion='not_inferred_from_recall_success')
            summary['message'] += (_runner_message("feedback_23")+req['node_id']+_runner_message("feedback_24")+req['reason']+
                _runner_message("feedback_25")+str((plan or {}).get('goal',_runner_message("feedback_26")))+_runner_message("feedback_27"))
            event=self.audit.append(Stage.PATH_RECALL_COMPLETED,Visibility.AGENT_VISIBLE,summary,step=decision_step)
            facts=self.memory.apply_agent_updates(list(parsed.get('memory_updates',[])),initial['observation_id'],decision_step)
            facts.extend(self.memory.record_action_result(event,'return_to_visited',initial['observation_id'],decision_step))
            self.audit.append(Stage.MEMORY_UPDATED,Visibility.EVALUATOR_PRIVATE,
                {'accepted_facts':[asdict(f) for f in facts]},step=decision_step)
            if self.task_support:self.task_support.update({},initial['observation_id'],summary)
            if self.visual_history:self.visual_history.annotate(initial['observation_id'],parsed,summary)
            with self._lock:
                self.recent_results.append(summary)
                self.last_recall=copy.deepcopy(summary)
                step_stat.update(verb='return_to_visited',success=summary['success'],recall_summary=summary)
                self.step_number=decision_step+1
                if summary['success']:
                    self.assist_count+=1
                    self.formal_sr_eligible=False
                    self.tainted_capabilities.update(('D3.2','D3.3'))
                    self.spatial_memory.new_chart()
                if outcome.get('observation'):
                    try:self._record_observation(outcome['observation'])
                    except Exception:
                        self.path_recall_reconciliation_required=True
                        raise
            return {'observation':self.observation,'public_result':summary,
                'metrics':dict(outcome.get('metrics') or {},assist_count=self.assist_count)}
        except Exception as error:
            self.audit.append(Stage.PATH_RECALL_COMPLETED,Visibility.EVALUATOR_PRIVATE,
                {'interrupted':True,'error':str(error),
                 'requires_receipt_reconciliation':self.path_recall_reconciliation_required},step=decision_step)
            raise
        finally:
            self.step_number=decision_step+1

    def _stats_summary(self) -> dict[str, Any]:
        stats = [s for s in self.step_stats if s.get('actor') != 'navigation_controller']
        decision_calls=sum(s.get('actor')!='waypoint_selector' for s in stats)
        compaction_calls=self.compactor.calls if self.compactor else []
        stats=stats+[dict(latency_ms=c.get('latency_ms'),prompt_tokens=(c.get('usage') or {}).get('prompt_tokens'),
                         completion_tokens=(c.get('usage') or {}).get('completion_tokens')) for c in compaction_calls]
        latencies = [s["latency_ms"] for s in stats if s.get("latency_ms") is not None]
        prompt = [s["prompt_tokens"] for s in stats if isinstance(s.get("prompt_tokens"), (int, float))]
        completion = [s["completion_tokens"] for s in stats if isinstance(s.get("completion_tokens"), (int, float))]
        return {
            "model_calls": len(stats),
            "decision_model_calls": decision_calls,
            "waypoint_selection_calls": sum(s.get('actor')=='waypoint_selector' for s in stats),
            "gt_navigation_decisions": sum(s.get('decision_type')=='gt_navigate' and s.get('decision_committed',False) for s in stats),
            "compaction_model_calls": len(compaction_calls),
            "compaction_latency_seconds": round(sum(c.get('latency_ms',0)/1000 for c in compaction_calls),2),
            "controller_steps": sum(s.get('primitive_actions', 0) for s in self.step_stats if s.get('decision_type') in ('navigate','gt_navigate')),
            "navigation_primitive_actions": sum(s.get('primitive_actions', 0) for s in self.step_stats if s.get('decision_type') in ('navigate','gt_navigate')),
            "recall_decisions": sum(s.get('decision_type')=='return_to_visited' and s.get('decision_committed',False) for s in stats),
            "recall_successes": sum(s.get('decision_type')=='return_to_visited' and s.get('success',False) for s in stats),
            "recall_distance_m": round(sum((s.get('recall_summary') or {}).get('distance_m',0) or 0 for s in stats),3),
            "decision_steps": self.step_number,
            "primitive_actions": self.primitive_action_count,
            "navigation_decisions": sum(s.get('decision_type') == 'navigate' and s.get('decision_committed', False) for s in self.step_stats),
            "navigation_perception_seconds": round(self.navigation.total_inference_ms/1000, 2) if self.navigation else 0.,
            "format_repairs": sum(bool(s.get("format_repair")) for s in stats),
            "total_prompt_tokens": sum(prompt) if prompt else None,
            "total_completion_tokens": sum(completion) if completion else None,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "max_latency_ms": max(latencies) if latencies else None,
            "total_model_latency_seconds": round(sum(latencies)/1000,2),
            "total_perception_seconds": round(self.spatial_memory.total_inference_ms/1000,2) if self.spatial_memory else 0.,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            boot = {"error": self.boot_error}
            boot_status = getattr(self.environment, "boot_status", None)
            if callable(boot_status):
                try:
                    boot.update(boot_status())
                except Exception:
                    pass
            return {
                "run_id": self.run_id,
                "checkpoint": self.checkpoint_info,
                "checkpoint_reconciliation_required": self.checkpoint_reconciliation_required,
                "checkpoint_save_pending": self.checkpoint_save_pending,
                "maintenance_hold": bool(self.maintenance_hold),
                "status": self.status,
                "config": {
                    **asdict(self.config),
                    "safety_policy_version": ('decision_safety_v2' if self.config.environment_backend == 'development_inspector' else None),
                    "depth_image_version": "front_metric_depth_v1" if self.config.depth_image_enabled else None,
                    "sensor_condition": "RGB-D" if self.config.depth_image_enabled else "RGB",
                    "interface_mode": self.config.interface_mode.value,
                    "run_mode": self.config.run_mode.value,
                    "navigation_version": self.navigation.state['version'] if self.navigation else None,
                    "gt_navigation_version": "visible_gt_waypoint_v2_triplet" if self.config.gt_navigation_enabled else None,
                    "path_recall_version": "explored_path_recall_v1" if self.path_recall else None,
                    "spatial_memory_version": self.spatial_memory.version if self.spatial_memory else None,
                    "motion_contract": self.environment.capabilities().get('motion'),
                    "current_view_protocol": ('left_front_right_v1' if getattr(self.environment,'current_views_enabled',False) else 'single_front_legacy'),
                },
                "environment_profile": self.environment_profile,
                "boot": boot,
                "model_waiting": self.model_waiting,
                'provider_retry': self.provider_retry,
                'provider_retry_policy': (retry_policy(self.provider.profile)
                    if getattr(self.provider, 'supports_retry_control', False) else None),
                "auto_running": self.auto_running,
                "auto_step_limit": self.auto_step_limit,
                "auto_stop_reason": self.auto_stop_reason,
                "auto_error": self.auto_error,
                "step": self.step_number,
                "step_counting_protocol": "model_decision_v2",
                "primitive_action_count": self.primitive_action_count,
                "navigation_reconciliation_required": self.navigation_reconciliation_required,
                "gt_navigation_state": {"pending_selection": self._gt_navigation_pending is not None,"last_result":self.last_gt_navigation},
                "path_recall_reconciliation_required": self.path_recall_reconciliation_required,
                "episode_active_seconds": round(self._episode_elapsed(), 1),
                "apc_auto": self.config.apc_auto,
                "observation": self.observation,
                "history_frames": (self.visual_history.recent(self.observation['observation_id'],self.config.visual_frame_count)
                    if self.visual_history and self.observation else []),
                "observer_views": self.observer_views,
                "step_stats": list(self.step_stats),
                "stats_summary": self._stats_summary(),
                "memory": self.memory.snapshot(),
                "memory_compaction": self.compactor.snapshot() if self.compactor else {"enabled":False},
                **({'task_support': self.task_support.context(self.recent_results)} if self.task_support else {}),
                **({'navigation_state': dict(self.navigation.state)} if self.navigation else {}),
                **({'path_recall_state': {'version':'explored_path_recall_v1','enabled':True,
                    'last_return': self.last_recall, 'candidates': self.spatial_memory.recall_candidates()}} if self.path_recall and self.spatial_memory else {}),
                **({'spatial_memory': self.spatial_memory.context()} if self.spatial_memory else {}),
                "behavior_diagnostics": public_history_warnings(self.recent_results),
                "apc": self.apc.snapshot(),
                "formal_sr_eligible": self.formal_sr_eligible,
                "backend_formal_eligible": self.backend_formal_eligible,
                "assist_count": self.assist_count,
                "tainted_capabilities": sorted(self.tainted_capabilities),
                "capabilities": [
                    {**item, "status": self.capability_status[item["id"]]} for item in CAPABILITIES
                ],
            }


class RunManager:
    def __init__(self):
        self.runners: dict[str, EpisodeRunner] = {}
        self._lock = threading.Lock()

    def add(self, runner: EpisodeRunner) -> None:
        with self._lock:
            self.runners[runner.run_id] = runner

    def get(self, run_id: str) -> EpisodeRunner:
        runner = self.runners.get(run_id)
        if not runner:
            raise KeyError(f"unknown run: {run_id}")
        return runner

    def list(self) -> list[dict[str, Any]]:
        return [runner.snapshot() for runner in self.runners.values()]

    def remove(self, run_id: str) -> None:
        """Drop a run from the history list (its on-disk audit dir is kept)."""
        runner = self.get(run_id)
        runner.terminate(reason="deleted_from_history")
        with self._lock:
            self.runners.pop(run_id, None)
