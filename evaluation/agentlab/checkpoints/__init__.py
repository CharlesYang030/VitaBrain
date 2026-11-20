"""Verified maintenance continuation. Native Unity remains the world owner.

Files here are evaluator-private and are never passed to ContextBuilder.
This deliberately does not claim to resurrect a dead Unity process.
"""
import copy
import fcntl
import gzip
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .codec import encode, decode

VERSION = 'live_world_checkpoint_v1'
RUN_SKIP = {'provider', 'environment', 'context_builder', 'audit', 'run_dir',
            '_lock', '_step_lock', '_request_cancel_event', '_checkpoint_lease',
            'checkpoint_info', '_checkpoint_dispatch', '_checkpoint_enabled'}
ENV_SKIP = {'_transport', '_sleep', '_checkpoint_before_dispatch'}


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        with temp.open('xb') as f:
            os.chmod(temp, 0o600)
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def stamp(env):
    """Read only; never step/render/reset or disclose private state to the model."""
    health = env._json('GET', '/api/inspect/runtime-health')
    if not health.get('ready') or not health.get('pid'):
        raise RuntimeError('Unity 现场不可用；禁止重新建场冒充恢复')
    first = env._json('GET', '/api/inspect/runtime-frame-private')['observation_token']
    status = env._json('GET', '/api/inspect/status')
    last = env._json('GET', '/api/inspect/runtime-frame-private')['observation_token']
    if first != last or status.get('busy') or not status.get('ready'):
        raise RuntimeError('现场正在变化；请等待动作回执后保存')
    # The token includes a per-world random epoch, pose, objects and masks;
    # panels additionally bind game progression/health and scenario identity.
    return dict(unity_pid=health['pid'], frame_token=first,
                scenario=status.get('scenario'), house=status.get('house_index'),
                panels_sha256=hashlib.sha256(json_bytes(status.get('panels'))).hexdigest())


def prompt_hashes(builder):
    return {str(p.relative_to(builder.prompt_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(builder.prompt_dir.rglob('*.md'))}


def lease(runner):
    if getattr(runner, '_checkpoint_lease', None) is not None:
        return
    root = runner.run_dir / 'checkpoints'
    root.mkdir(parents=True, exist_ok=True)
    f = (root / 'owner.lock').open('a+')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise RuntimeError('该运行仍由另一个评测进程持有；不能双重续跑')
    runner._checkpoint_lease = f


def journal(runner, phase, operation=None):
    if not getattr(runner, '_checkpoint_enabled', False):
        return
    atomic(runner.run_dir / 'checkpoints' / 'transaction.json', json_bytes(dict(
        phase=phase, step=runner.step_number, operation=operation)))
    runner._checkpoint_dispatch = phase == 'dispatching'


def save(runner, reason='manual', *, internal=False):
    if not getattr(runner, '_checkpoint_enabled', False):
        raise RuntimeError('此后端尚无可验证现场检查点')
    # Nonblocking external save cannot wait behind an unbounded API call.
    if not runner._step_lock.acquire(blocking=internal):
        raise RuntimeError('本步仍在执行；请先停止并等待动作回执')
    try:
        if not internal and (runner.auto_running or runner.model_waiting):
            raise RuntimeError('请先停止自动运行并等待本步结束')
        if runner.status not in ('running', 'completed', 'stopped') or not runner.observation:
            raise RuntimeError('尚无可保存的运行现场')
        if (getattr(runner, '_checkpoint_dispatch', False)
                or getattr(runner, 'checkpoint_reconciliation_required', False)
                or runner.navigation_reconciliation_required
                or runner.path_recall_reconciliation_required):
            raise RuntimeError('动作执行结果待核对；保留上次检查点，禁止覆盖或盲目重放')
        lease(runner)
        world = stamp(runner.environment)
        if (hasattr(runner.environment, '_frame_token')
                and runner.environment._frame_token != world['frame_token']):
            raise RuntimeError('模型观察已落后于现场；不能将旧记忆帧与新世界打包成同一检查点')
        with runner._lock:
            state = {k: v for k, v in vars(runner).items() if k not in RUN_SKIP}
            # Save elapsed time, not obsolete monotonic start timestamps.
            state['_episode_active_seconds'] = runner._episode_elapsed()
            state['_episode_started_at'] = None
            state['auto_running'] = state['model_waiting'] = False
            state['provider_retry'] = None
            state['apc'] = copy.copy(runner.apc)
            if runner.apc.focus:
                state['apc'].focus = copy.copy(runner.apc.focus)
                state['apc'].focus.active_seconds = runner.apc.elapsed()
                state['apc'].focus.running = False
            data = dict(runner=encode(state), environment=encode({
                k: v for k, v in vars(runner.environment).items() if k not in ENV_SKIP}))
            # Provider credentials/client never serialized. Configured model and
            # non-secret request overrides are verified on restore.
            from agentlab.security import redact
            profile = getattr(runner.provider, 'profile', None)
            data['provider_parameters'] = redact(dict(profile.parameters if profile else {}))
            payload = gzip.compress(json_bytes(data), compresslevel=3, mtime=0)
            with runner.audit._lock:
                if runner.audit.events_path.exists():
                    with runner.audit.events_path.open('rb') as f:
                        os.fsync(f.fileno())
                tail = dict(seq=runner.audit._seq, hash=runner.audit._prev_hash,
                            size=runner.audit.events_path.stat().st_size)
        checkpoint_id = uuid.uuid4().hex
        info = dict(version=VERSION, checkpoint_id=checkpoint_id,
                    created_at=datetime.now(timezone.utc).isoformat(), reason=reason,
                    run_id=runner.run_id, step=runner.step_number,
                    mode='live_world_only', cold_restore_supported=False,
                    world=world, audit=tail, prompts=prompt_hashes(runner.context_builder),
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
                    payload_file=checkpoint_id+'.json.gz')
        root = runner.run_dir / 'checkpoints'
        atomic(root / info['payload_file'], payload)
        # latest.json is the commit point. A torn write never replaces it.
        atomic(root / 'latest.json', json_bytes(info))
        journal(runner, 'committed')
        runner.checkpoint_info = {k: info[k] for k in (
            'version','checkpoint_id','created_at','reason','step','mode','cold_restore_supported')}
        # Keep latest and one previous data file; logs already hold full history.
        files = sorted(root.glob('*.json.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[2:]:
            old.unlink()
        return runner.checkpoint_info
    finally:
        runner._step_lock.release()


def restore(run_dir, *, environment, provider_factory, context_builder):
    from agentlab.runner import EpisodeRunner
    from agentlab.audit import AuditStore
    from agentlab.agent.compaction import SelfCompactor
    run_dir = Path(run_dir)
    root = run_dir / 'checkpoints'
    info = json.loads((root / 'latest.json').read_text())
    if info.get('version') != VERSION or info['run_id'] != run_dir.name:
        raise ValueError('检查点版本或运行 ID 不匹配')
    if not re.fullmatch(r'[a-f0-9]{32}\.json\.gz', info['payload_file']):
        raise ValueError('invalid checkpoint filename')
    transaction = json.loads((root/'transaction.json').read_text()) if (root/'transaction.json').exists() else {}
    if transaction.get('phase') != 'committed':
        raise RuntimeError('进程在决策或动作中断；需核对事务日志，不能重放旧检查点')
    payload = (root / info['payload_file']).read_bytes()
    if hashlib.sha256(payload).hexdigest() != info['payload_sha256']:
        raise ValueError('检查点内容校验失败')
    audit = AuditStore(run_dir, info['run_id'])
    audit.resume_verified(info['audit'])
    if stamp(environment) != info['world']:
        raise RuntimeError('Unity 会话或世界状态已变化；不能按旧现场续跑')
    data = json.loads(gzip.decompress(payload))
    state = decode(data['runner'])
    provider = provider_factory(state['config'], data['provider_parameters'])
    profile = getattr(provider, 'profile', None)
    if (profile.model if profile else getattr(provider, 'model', None)) != state['config'].provider_model:
        raise ValueError('待测模型已变化；禁止静默替换模型续跑')
    runner = EpisodeRunner.__new__(EpisodeRunner)
    runner.__dict__.update(state)
    runner.run_dir, runner.audit = run_dir, audit
    lease(runner)
    try:
        if stamp(environment) != info['world']:
            raise RuntimeError('接管期间现场变化，拒绝恢复')
        # Instantiate transport externally; restore data only, never call reset.
        environment.__dict__.update(decode(data['environment']))
        runner.environment, runner.provider = environment, provider
        runner.context_builder = context_builder
        runner._lock, runner._step_lock = threading.RLock(), threading.RLock()
        runner._request_cancel_event = threading.Event()
        runner._checkpoint_enabled, runner._checkpoint_dispatch = True, False
        runner.auto_running = runner.model_waiting = False
        runner.maintenance_hold = None
        runner.auto_stop_reason = 'checkpoint_restored_paused'
        runner._episode_started_at = None
        runner.checkpoint_info = {k: info[k] for k in (
            'version','checkpoint_id','created_at','reason','step','mode','cold_restore_supported')}
        runner.environment._checkpoint_before_dispatch = lambda op: journal(runner, 'dispatching', op)
        # Latest fixed skills apply, while the already-compressed memory stays.
        if runner.compactor:
            new_skill = SelfCompactor(runner.config, context_builder.prompt_dir/'memory_compact.md')
            runner.compactor.skill = new_skill.skill
            runner.compactor.skill_sha256 = new_skill.skill_sha256
        # Fail before publishing if this code version cannot read the saved
        # feature state. A schema migration must be explicit, never a reset.
        runner.snapshot()
        runner.audit.append('CHECKPOINT_RESTORED', 'evaluator_private', dict(
            checkpoint_id=info['checkpoint_id'], world_verified=True, reset_called=False,
            model_called=False, previous_prompts=info['prompts'],
            current_prompts=prompt_hashes(context_builder), auto_running=False), step=runner.step_number)
        save(runner, 'restored_paused', internal=True)
        return runner
    except BaseException:
        runner._checkpoint_lease.close()
        raise
