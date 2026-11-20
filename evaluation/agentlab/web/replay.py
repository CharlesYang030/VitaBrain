"""Read-only replay assets and terminal snapshots, independent of Unity."""
import base64
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path

from agentlab.audit import AuditStore

TERMINAL = {'completed', 'stopped', 'boot_failed'}
_snapshot_lock = threading.Lock()


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def replay_projection(value, run_dir):
    """Use image URLs in UI transport; the original hashed audit is untouched."""
    if isinstance(value, dict):
        return {k: replay_projection(v, run_dir) for k, v in value.items()}
    if isinstance(value, list):
        return [replay_projection(v, run_dir) for v in value]
    if not isinstance(value, str) or not value.startswith('data:image/'):
        return value
    match = re.fullmatch(r'data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/=\r\n]+)', value)
    if not match:
        return value
    content = base64.b64decode(match[2])
    name = hashlib.sha256(content).hexdigest() + '.' + match[1]
    path = run_dir / 'replay-images' / name
    if not path.exists():
        atomic_write(path, content)
    return f'/api/runs/{run_dir.name}/replay-images/{name}'


def persist_terminal(runner, snapshot=None):
    if runner.status not in TERMINAL or runner.auto_running:
        return
    with _snapshot_lock:
        snapshot = snapshot if snapshot is not None else runner.snapshot()
        if snapshot.get('model_waiting') or snapshot.get('auto_running'):
            return
        audit_path = runner.run_dir / 'events.jsonl'
        stat = audit_path.stat() if audit_path.exists() else None
        identity = [stat.st_size, stat.st_mtime_ns] if stat else [0, 0]
        path = runner.run_dir / 'terminal-replay.json'
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get('audit_identity') == identity:
                return
        data = dict(version='terminal_replay_v1', audit_identity=identity,
                    snapshot=replay_projection(snapshot, runner.run_dir))
        atomic_write(path, json.dumps(data, ensure_ascii=False).encode())


class TerminalReplayRun:
    """Reviewable after service restart, with no environment/provider client."""
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.data = json.loads((run_dir/'terminal-replay.json').read_text())['snapshot']
        assert self.data['run_id'] == run_dir.name and self.data['status'] in TERMINAL
        self.data.update(archived=True, auto_running=False, model_waiting=False)
        self.run_id, self.status, self.auto_running = run_dir.name, self.data['status'], False
        self.audit = AuditStore(run_dir, self.run_id)

    def snapshot(self):
        return self.data

    def terminate(self, **kwargs):
        pass

    def pause_clock(self):
        pass

    def __getattr__(self, name):
        raise RuntimeError('历史运行仅供回放；请新建运行进行评测')


def load_terminal_runs(manager, runs_dir):
    for path in sorted(runs_dir.glob('*/terminal-replay.json')):
        if (path.parent/'history-hidden').exists():
            continue
        try:
            manager.add(TerminalReplayRun(path.parent))
        except (OSError, ValueError, KeyError, AssertionError):
            continue
