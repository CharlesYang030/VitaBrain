from __future__ import annotations

import threading
from typing import Any

import io
import json
import zipfile

from flask import Flask, jsonify, render_template, request, send_file

from agentlab.agent import ContextBuilder
from agentlab.config import LabSettings, load_provider_profiles
from agentlab.domain import InterfaceMode, RunMode, Stage, Visibility
from agentlab.environment import (
    MockEnvironmentClient,
    SceneGenHttpClient,
    SceneGenInspectorBridgeClient,
)
from agentlab.environment import inspector_bridge
from agentlab.providers import ProviderRegistry
from agentlab.providers.openai_compatible import ProviderRequestCancelled
from agentlab.runner import EpisodeRunner, RunConfig, RunManager, ModelOutputRejected, MAX_OUTPUT_RETRIES
from agentlab.security import redact, scan_events_for_leaks
from agentlab.web.history import history_metadata, history_result_snapshot
from agentlab.web.replay import load_terminal_runs, persist_terminal, replay_projection


def backend_registry(settings: LabSettings) -> dict[str, dict[str, Any]]:
    """Selectable environment backends. None of them is formal yet."""
    return {
        "mock": {
            "name": "mock",
            "title": "Mock 环境（契约演示）",
            "watermark": "MOCK",
            "formal_sr_eligible": False,
            "reason": "本地确定性契约环境，仅用于数据流演示。",
            "factory": MockEnvironmentClient,
        },
        "scenegen_http": {
            "name": "scenegen_http",
            "title": "Runtime 契约模拟器（8860 /v1）",
            "watermark": "MOCK-RUNTIME",
            "formal_sr_eligible": False,
            "reason": "8860 是 /v1 契约模拟器，非真实 Unity runtime。",
            "factory": lambda: SceneGenHttpClient(settings.scenegen_url),
        },
        "development_inspector": {
            "name": "development_inspector",
            "title": "8851 Inspector 开发桥（STB 真实场景）",
            "watermark": "DEVELOPMENT-INSPECTOR",
            "formal_sr_eligible": False,
            "reason": inspector_bridge.PROFILE["reason"],
            "factory": lambda: SceneGenInspectorBridgeClient(
                settings.inspector_url),
        },
    }


def create_app(settings: LabSettings | None = None) -> Flask:
    settings = settings or LabSettings.from_env()
    app = Flask(__name__)
    # Dev workbench: the browser must always pick up freshly-edited JS/CSS —
    # stale cached app.js has already masked shipped fixes once.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    @app.after_request
    def _no_static_cache(response):
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response
    registry = ProviderRegistry(load_provider_profiles(settings.provider_config))
    manager = RunManager()
    load_terminal_runs(manager, settings.runs_dir)
    context_builder = ContextBuilder(settings.root / "prompts", settings.context_character_budget)
    auto_stop: dict[str, threading.Event] = {}
    auto_lock = threading.RLock()
    def serialize_world_owner(fn):
        from functools import wraps
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with auto_lock:
                return fn(*args, **kwargs)
        return wrapped
    backends = backend_registry(settings)

    # 8851 is a single live session; a shared client lets a repeat run reuse
    # the bound scene via the fast in-episode reset instead of a minutes-long
    # rebind.  Mock/contract backends stay per-run for episode isolation.
    shared_clients: dict[str, Any] = {}

    def make_environment(backend: str):
        if backend not in backends:
            raise ValueError(f"unknown environment backend: {backend}")
        if backend == "development_inspector":
            if backend not in shared_clients:
                shared_clients[backend] = backends[backend]["factory"]()
            return shared_clients[backend]
        return backends[backend]["factory"]()

    def backend_profile(backend: str) -> dict[str, Any]:
        meta = backends[backend]
        return {
            "backend": backend,
            "watermark": meta["watermark"],
            "formal_sr_eligible": meta["formal_sr_eligible"],
            "reason": meta["reason"],
        }

    def api_error(error: Exception, status: int = 400):
        return jsonify({"ok": False, "error": type(error).__name__, "message": str(redact(str(error)))}), status

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "service": "agentlab", "port": settings.port,
                        "checkpoint_version": "live_world_checkpoint_v1",
                        "default_backend": settings.env_backend})

    @app.get("/api/backends")
    def list_backends():
        rows = []
        for name, meta in backends.items():
            row = {key: meta[key] for key in
                   ("name", "title", "watermark", "formal_sr_eligible", "reason")}
            row["default"] = name == settings.env_backend
            rows.append(row)
        return jsonify({"backends": rows})

    @app.get("/api/providers")
    def providers():
        return jsonify({"providers": registry.public_profiles()})

    @app.post("/api/providers/<name>/test")
    def test_provider(name: str):
        try:
            return jsonify(registry.create(name).test_connection())
        except Exception as error:
            return api_error(error)

    @app.get('/api/task-progress-coverage')
    def task_progress_coverage():
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / 'task_progress_coverage.json'
        return jsonify(json.loads(path.read_text(encoding='utf-8')))

    @app.get("/api/scenarios")
    def scenarios():
        try:
            backend = request.args.get("backend", settings.env_backend)
            environment = make_environment(backend)
            return jsonify({
                "backend": backend,
                "scenarios": environment.scenarios(),
                "interface": environment.capabilities(),
            })
        except Exception as error:
            return api_error(error, 503)

    @app.get("/api/runs")
    def runs():
        return jsonify({"runs": [
            {**{key: snap[key] for key in (
                "run_id", "status", "config", "step", "formal_sr_eligible",
                "assist_count", "environment_profile")},
             "history": history_metadata(settings.runs_dir / snap["run_id"]),
             "result": history_result_snapshot(snap)}
            for snap in manager.list()
        ]})

    @app.post("/api/runs")
    @serialize_world_owner
    def create_run():
        try:
            body = request.get_json(force=True) or {}
            if 'depth_image_enabled' in body and type(body['depth_image_enabled']) is not bool:
                raise ValueError('depth_image_enabled must be a boolean')
            if 'fog_gt_enabled' in body and type(body['fog_gt_enabled']) is not bool:
                raise ValueError('fog_gt_enabled must be a boolean')
            if body.get('fog_gt_enabled') and body.get('spatial_memory_enabled'):
                raise ValueError('探索式 GT 地图与 RGB 建图互斥；导航可独立开启')
            if 'spatial_memory_enabled' in body and type(body['spatial_memory_enabled']) is not bool:
                raise ValueError('spatial_memory_enabled must be a boolean')
            if 'navigation_enabled' in body and type(body['navigation_enabled']) is not bool:
                raise ValueError('navigation_enabled must be a boolean')
            if type(body.get('visual_frame_count', 1)) is not int or body.get('visual_frame_count', 1) not in (1,5,10):
                raise ValueError('visual_frame_count must be 1, 5 or 10')
            if body.get('context_policy', 'legacy') not in ('legacy', 'enhanced_v1'):
                raise ValueError('unknown context_policy')
            backend = str(body.get("environment_backend", settings.env_backend))
            provider_name = str(body.get("provider", "mock"))
            profile = registry.profiles[provider_name]
            scenario_id = str(body.get("scenario_id", "mock_long_horizon_001"))
            environment = make_environment(backend)
            if backend == "development_inspector":
                active = [row for row in manager.list()
                          if row["config"].get("environment_backend") == backend
                          and row["status"] in ("created", "loading", "running")]
                if active:
                    raise RuntimeError(
                        "8851 Inspector 是单会话环境：请先停止运行 "
                        + active[0]["run_id"])
                # A frontend restart must not offer a fresh reset before the
                # surviving world/checkpoint has been deliberately restored.
                from agentlab.checkpoints import stamp
                saved_checkpoints = list(settings.runs_dir.glob('run_*/checkpoints/latest.json'))
                if saved_checkpoints:
                    try:
                        live_world = stamp(environment)
                    except Exception:
                        live_world = None
                    if live_world:
                        for saved_path in saved_checkpoints:
                            saved_checkpoint = json.loads(saved_path.read_text())
                            if saved_checkpoint.get('world') == live_world:
                                try:
                                    known = manager.get(saved_checkpoint['run_id'])
                                except KeyError:
                                    known = None
                                if known is None:
                                    raise RuntimeError('发现仍存活的检查点现场，请先点“恢复检查点”，避免重建丢失进度')
            scenario = next(item for item in environment.scenarios() if item["scenario_id"] == scenario_id)
            house_raw = body.get("house_index")
            house_index = (int(house_raw)
                           if house_raw not in (None, "", "auto") else None)
            effort = str(body.get("reasoning_effort") or "").strip()
            overrides = {"reasoning_effort": effort} if effort else None
            runner = EpisodeRunner(
                config=RunConfig(
                    scenario_id=scenario_id,
                    scenario_digest=str(scenario["digest"]),
                    seed=int(body.get("seed", 0)),
                    interface_mode=InterfaceMode(body.get("interface_mode", InterfaceMode.E2E.value)),
                    run_mode=RunMode(body.get("run_mode", RunMode.STRICT.value)),
                    provider_name=provider_name,
                    provider_model=profile.model,
                    focused_timeout_seconds=float(body.get("focused_timeout_seconds", settings.focused_timeout_seconds)),
                    environment_backend=backend,
                    house_index=house_index,
                    agent_mode=(str(body.get("agent_mode"))
                                if body.get("agent_mode") else None),
                    apc_auto=bool(body.get("apc_auto", True)),
                    spatial_memory_enabled=body.get('spatial_memory_enabled', False),
                    depth_image_enabled=body.get('depth_image_enabled', False),
                    fog_gt_enabled=body.get('fog_gt_enabled', False),
                    visual_frame_count=body.get('visual_frame_count', 1),
                    context_policy=body.get('context_policy', 'legacy'),
                    navigation_enabled=body.get('navigation_enabled', False),
                    gt_navigation_enabled=body.get('gt_navigation_enabled', False),
                    path_recall_enabled=body.get('path_recall_enabled', False),
                    memory_compaction_enabled=body.get('memory_compaction_enabled', True),
                    memory_compaction_trigger_tokens=body.get('memory_compaction_trigger_tokens'),
                    memory_compaction_target_tokens=body.get('memory_compaction_target_tokens'),
                    memory_compaction_hard_limit_tokens=body.get('memory_compaction_hard_limit_tokens'),
                ),
                provider=registry.create(provider_name, parameter_overrides=overrides),
                environment=environment,
                runs_dir=settings.runs_dir,
                context_builder=context_builder,
                environment_profile=backend_profile(backend),
                checkpoint_enabled=backend == 'development_inspector',
            )
            manager.add(runner)
            runner.start_async()
            return jsonify({"ok": True, "run": runner.snapshot()}), 201
        except Exception as error:
            return api_error(error)

    @app.get('/api/checkpoints')
    def checkpoint_list():
        rows = []
        for path in settings.runs_dir.glob('run_*/checkpoints/latest.json'):
            try:
                item = json.loads(path.read_text())
                rows.append({k: item[k] for k in ('run_id','step','created_at','mode','cold_restore_supported')})
            except (OSError, ValueError, KeyError):
                continue
        return jsonify({'checkpoints': sorted(rows, key=lambda r:r['created_at'], reverse=True)})

    @app.post('/api/runs/<run_id>/checkpoint')
    def checkpoint_save(run_id):
        try:
            return jsonify({'ok': True, 'checkpoint': manager.get(run_id).save_checkpoint()})
        except Exception as error:
            return api_error(error, 409)

    @app.post('/api/runs/<run_id>/maintenance/prepare')
    def maintenance_prepare(run_id):
        try:
            with auto_lock:
                return jsonify({'ok': True, **manager.get(run_id).prepare_maintenance()})
        except Exception as error:
            return api_error(error, 409)

    @app.post('/api/runs/<run_id>/maintenance/release')
    def maintenance_release(run_id):
        try:
            manager.get(run_id).release_maintenance((request.get_json(force=True) or {}).get('maintenance_token'))
            return jsonify(ok=True)
        except Exception as error:
            return api_error(error, 409)

    @app.post('/api/checkpoints/<run_id>/restore')
    def checkpoint_restore(run_id):
        from agentlab.checkpoints import restore
        import re
        try:
            if not re.fullmatch(r'run_[a-f0-9]+', run_id):
                raise ValueError('invalid run id')
            # Serialize restore/create ownership of the single Inspector world.
            with auto_lock:
                if any(row['status'] in ('created','loading','running') for row in manager.list()
                       if row['config'].get('environment_backend') == 'development_inspector'):
                    raise RuntimeError('当前服务已有活动现场；不要重复恢复或新建运行')
                def provider_factory(config, parameters):
                    return registry.create(config.provider_name, parameter_overrides=parameters)
                runner = restore(settings.runs_dir / run_id,
                    environment=SceneGenInspectorBridgeClient(settings.inspector_url),
                    provider_factory=provider_factory, context_builder=context_builder)
                shared_clients['development_inspector'] = runner.environment
                manager.add(runner)
            return jsonify({'ok': True, 'run': runner.snapshot()})
        except Exception as error:
            return api_error(error, 409)

    @app.get('/api/runs/<run_id>/task-progress')
    def task_progress_report(run_id):
        import re
        from agentlab.trajectory_metrics import compute
        if not re.fullmatch(r'run_[a-f0-9]+', run_id):
            return api_error(ValueError('invalid run id'))
        try:
            result = compute(settings.runs_dir / run_id)
            return jsonify(run_id=run_id, task=result['task'], validity=result['validity'])
        except Exception as error:
            return api_error(error)

    @app.get("/api/runs/<run_id>")
    def run_snapshot(run_id: str):
        try:
            runner = manager.get(run_id)
            snapshot = runner.snapshot()
            persist_terminal(runner, snapshot)
            if request.args.get('view') == 'replay':
                snapshot = replay_projection(snapshot, runner.run_dir)
            return jsonify(snapshot)
        except Exception as error:
            return api_error(error, 404)

    @app.get("/api/runs/<run_id>/events")
    def run_events(run_id: str):
        try:
            after = int(request.args.get("after", 0))
            runner = manager.get(run_id)
            if request.args.get('view') == 'replay':
                page = runner.audit.events_page(after)
                page['events'] = replay_projection(page['events'], runner.run_dir)
                page['image_transport'] = 'content_addressed_urls_original_audit_unchanged'
                return jsonify(page)
            valid, problem = runner.audit.verify()
            return jsonify({"events": runner.audit.events(after), "hash_chain_valid": valid, "hash_chain_problem": problem})
        except Exception as error:
            return api_error(error, 404)

    @app.get('/api/runs/<run_id>/replay-images/<name>')
    def replay_image(run_id, name):
        import re
        if not re.fullmatch(r'[0-9a-f]{64}\.(png|jpeg|webp|gif)', name):
            return api_error(ValueError('invalid replay image'), 400)
        try:
            path = manager.get(run_id).run_dir / 'replay-images' / name
            return send_file(path, conditional=True, max_age=31536000)
        except (KeyError, FileNotFoundError) as error:
            return api_error(error, 404)

    @app.get('/api/runs/<run_id>/monitor')
    def run_monitor(run_id: str):
        """Observer-only text monitoring; never an agent observation endpoint."""
        try:
            snap=manager.get(run_id).snapshot()
            result={k:snap.get(k) for k in ('run_id','status','config','step','auto_running',
                'model_waiting','auto_stop_reason','auto_error','episode_active_seconds','stats_summary',
                'auto_step_limit','step_counting_protocol','primitive_action_count','navigation_state','assist_count','provider_retry','provider_retry_policy','memory_compaction')}
            obs=snap.get('observation') or {}
            rows=(snap.get('observer_views') or {}).get('subtask_progress_full') or []
            result.update(health=obs.get('health'),collision_count=(obs.get('ego_state') or {}).get('collision_count'),
                subtasks_done=sum(r.get('status') in ('done','passed','completed','succeeded') for r in rows),
                subtasks_total=len(rows),episode_done=obs.get('episode_done'),
                recent_step_stats=(snap.get('step_stats') or [])[-3:])
            return jsonify(result)
        except Exception as error:
            return api_error(error,404)

    @app.get("/api/runs/<run_id>/leak-scan")
    def run_leak_scan(run_id: str):
        try:
            runner = manager.get(run_id)
            report = scan_events_for_leaks(runner.audit.events())
            report["run_id"] = run_id
            return jsonify(report)
        except Exception as error:
            return api_error(error, 404)

    @app.get("/api/runs/<run_id>/export")
    def run_export(run_id: str):
        try:
            runner = manager.get(run_id)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(runner.run_dir.rglob("*")):
                    if path.is_file():
                        bundle.writestr(
                            f"{run_id}/{path.relative_to(runner.run_dir)}",
                            path.read_bytes())
                bundle.writestr(
                    f"{run_id}/leak-report.json",
                    json.dumps(scan_events_for_leaks(runner.audit.events()),
                               ensure_ascii=False, indent=2))
            buffer.seek(0)
            return send_file(buffer, mimetype="application/zip",
                             as_attachment=True,
                             download_name=f"{run_id}.zip")
        except Exception as error:
            return api_error(error, 404)

    @app.post("/api/runs/<run_id>/step")
    def run_step(run_id: str):
        try:
            runner = manager.get(run_id)
            result = runner.step_once()
            snapshot = runner.snapshot()
            persist_terminal(runner, snapshot)
            return jsonify({"ok": True, "result": result, "run": snapshot})
        except Exception as error:
            return api_error(error)

    @app.post("/api/runs/<run_id>/handoff")
    def debug_handoff(run_id: str):
        try:
            runner = manager.get(run_id)
            return jsonify({"ok": True, "handoff": runner.force_debug_handoff(), "run": runner.snapshot()})
        except Exception as error:
            return api_error(error)

    @app.post("/api/runs/<run_id>/auto")
    def run_auto(run_id: str):
        try:
            runner = manager.get(run_id)
            max_steps = int((request.get_json(silent=True) or {}).get("max_steps", 0))
            if max_steps < 0:
                raise ValueError("max_steps must be non-negative")
            with auto_lock:
                if runner.auto_running:
                    return jsonify({"ok": True, "already_running": True})
                if runner.status != "running":
                    raise ValueError("run is not active")
                if getattr(runner, 'maintenance_hold', None):
                    raise ValueError('正在准备维护，暂不接受自动运行')
                stop_event = threading.Event()
                auto_stop[run_id] = stop_event
                runner.auto_step_limit = runner.step_number + max_steps if max_steps else None
                manifest_path=runner.run_dir/'manifest.json'
                saved=json.loads(manifest_path.read_text())
                saved['auto_step_limit']=runner.auto_step_limit
                pending=manifest_path.with_suffix('.tmp')
                pending.write_text(json.dumps(saved,ensure_ascii=False,indent=2));pending.replace(manifest_path)
                runner.set_auto_state(True)

            def loop() -> None:
                steps = 0
                output_retries = 0
                output_token_multiplier = 1
                reason = "completed"
                failure = None
                try:
                    while not stop_event.is_set() and runner.status == "running":
                        try:
                            runner.step_once(output_token_multiplier=output_token_multiplier, cancel_event=stop_event)
                        except ModelOutputRejected as error:
                            if output_retries >= MAX_OUTPUT_RETRIES:
                                raise
                            output_retries += 1
                            if error.output_truncated:
                                output_token_multiplier = min(4, output_token_multiplier * 2)
                            budget_reader = getattr(runner.provider, "output_budget", None)
                            runner.audit.append(Stage.MODEL_OUTPUT_RETRY, Visibility.EVALUATOR_PRIVATE, {
                                "retry": output_retries, "max_retries": MAX_OUTPUT_RETRIES,
                                "error_type": type(error.original_error).__name__,
                                "message": str(error),
                                "output_truncated": error.output_truncated,
                                "next_output_budget": (budget_reader(output_token_multiplier)
                                                       if callable(budget_reader) else {}),
                            }, step=runner.step_number)
                            stop_event.wait(30.0)
                            continue
                        output_retries = 0
                        output_token_multiplier = 1
                        steps += 1
                        if runner.status != "running":
                            break
                        if max_steps and steps >= max_steps:
                            reason = "step_limit"
                            break
                        stop_event.wait(0.4)
                    if stop_event.is_set():
                        reason = "user_stopped"
                except ProviderRequestCancelled:
                    reason = 'user_stopped'
                except Exception as error:
                    reason, failure = "error", error
                finally:
                    # A dead thread must never leave an "already running"
                    # latch behind. Preserve its error and the current frame
                    # so the user can deliberately retry without a reset.
                    with auto_lock:
                        try:
                            runner.set_auto_state(False, reason=reason, error=failure)
                            persist_terminal(runner)
                        finally:
                            stop_event.set()

            try:
                threading.Thread(target=loop, daemon=True, name=f"agentlab-{run_id}").start()
            except Exception as error:
                with auto_lock:
                    stop_event.set()
                    runner.set_auto_state(False, reason="error", error=error)
                raise
            return jsonify({"ok": True})
        except Exception as error:
            return api_error(error)

    @app.post("/api/runs/<run_id>/stop")
    def run_stop(run_id: str):
        event = auto_stop.get(run_id)
        if event:
            event.set()
        body = request.get_json(silent=True) or {}
        try:
            runner = manager.get(run_id)
            cancel = getattr(runner, 'cancel_provider_request', None)
            if callable(cancel):
                cancel()
            if body.get("terminate"):
                runner.terminate()
                persist_terminal(runner)
            else:
                # 暂停自动循环 -> 冻结 APC 秒表(停下后的空闲不计入卡关时长)
                runner.pause_clock()
        except KeyError as error:
            return api_error(error, 404)
        return jsonify({"ok": True})

    @app.delete("/api/runs/<run_id>")
    def run_delete(run_id: str):
        try:
            event = auto_stop.pop(run_id, None)
            if event:
                event.set()
            manager.remove(run_id)
            (settings.runs_dir / run_id / 'history-hidden').touch()
            return jsonify({"ok": True, "deleted": run_id,
                            "note": "runs/ 目录下的审计文件已保留"})
        except KeyError as error:
            return api_error(error, 404)

    @app.get("/api/contracts")
    def contracts():
        paths = sorted((settings.root / "contracts").glob("*.schema.json"))
        return jsonify({"contracts": [path.name for path in paths], "protocol_version": "1.0"})

    app.config["AGENTLAB_SETTINGS"] = settings
    app.config["AGENTLAB_RUN_MANAGER"] = manager
    return app


def main() -> None:
    settings = LabSettings.from_env()
    app = create_app(settings)
    print(f"Agent Lab: http://{settings.host}:{settings.port}")
    app.run(host=settings.host, port=settings.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
