"""Small, read-only history metadata; never return frames or provider secrets."""
import json
from pathlib import Path


def history_result_snapshot(snapshot: dict) -> dict:
    result = {k: snapshot.get(k) for k in (
        "status", "step", "assist_count", "auto_running", "model_waiting",
        "auto_stop_reason", "auto_error", "episode_active_seconds", "step_counting_protocol", "primitive_action_count", "stats_summary", "memory_compaction")}
    observation = snapshot.get("observation") or {}
    full = (snapshot.get("observer_views") or {}).get("subtask_progress_full")
    result["observation"] = {"episode_done": observation.get("episode_done"),
        "sim_time_seconds": observation.get("sim_time_seconds"),
        "subtask_progress": [{"status": t.get("status")}
                             for t in observation.get("subtask_progress", [])]}
    result["observer_views"] = {"subtask_progress_full":
        [{"status": t.get("status")} for t in full] if full is not None else None}
    return result


def history_metadata(run_dir: Path) -> dict:
    result = {"started_at": None, "time_source": "first_audit_event",
              "provider_parameters": None}
    try:
        with (run_dir / "events.jsonl").open(encoding="utf-8") as stream:
            result["started_at"] = json.loads(next(stream))["timestamp"]
    except (OSError, StopIteration, ValueError, KeyError):
        pass
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        if "provider_parameters" in manifest:
            result["provider_parameters"] = {
                k: v for k, v in manifest["provider_parameters"].items()
                if k in ("reasoning_effort", "max_tokens", "max_completion_tokens",
                         "temperature", "top_p", "seed")}
        result["bound_digest"] = manifest.get("scenario_digest_bound")
        result["memory_compaction"] = manifest.get("memory_compaction")
    except (OSError, ValueError, TypeError):
        pass
    return result
