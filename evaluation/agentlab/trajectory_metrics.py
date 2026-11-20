"""Small, read-only metric set derived from hashed Agent Lab trajectories.

No model calls, hidden-world queries, thought-based judgements or oracle scores.
Metric names deliberately distinguish observed outcomes from formal benchmark SR.
"""
from __future__ import annotations

import hashlib
import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from agentlab.audit.store import _canonical

VERSION = 'trajectory_metrics_v1'
CALL_STAGES = {
    'MODEL_RESPONDED': 'decision',
    'GT_NAVIGATION_MODEL_OUTPUT': 'waypoint',
    'MEMORY_COMPACTION_RESPONDED': 'compaction',
}


def _signature(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _ratio(n, d):
    return round(n / d, 6) if d else None


def compute(run_dir: Path) -> dict:
    """Stream and verify one audit; memory use does not scale with image history."""
    run_dir = Path(run_dir)
    path = run_dir / 'events.jsonl'
    identity = _signature(path)
    manifest_path = run_dir / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    archive_path = run_dir / 'terminal-replay.json'
    archive = json.loads(archive_path.read_text()) if archive_path.exists() else {}
    snapshot = (archive.get('snapshot') or {}) if archive.get('audit_identity') == list(identity[2:4]) else {}
    snapshot_stale = bool(archive and not snapshot)
    first_ts = last_ts = None
    initial_obs, final_obs, score = {}, {}, {}
    seq, previous = 0, '0' * 64
    status = None
    receipts, macro_receipts = {}, {}
    primitives = []
    progress, maintenance, pauses = [], [], []
    calls, missing_usage, tokens, retries = Counter(), Counter(), Counter(), Counter()
    model_latency_ms, latency_missing = 0., 0
    damage, warnings, collision_events = [], [], []
    providers, prompt_hashes = set(), set()
    stage_counts = Counter()
    apc_handoffs = 0
    last_observer_tasks = None
    milestone_records = {}
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            event = json.loads(line)
            digest = event.pop('event_hash')
            if (event.get('run_id') != run_dir.name or event.get('seq') != seq + 1
                    or event.get('prev_hash') != previous
                    or hashlib.sha256(_canonical(event)).hexdigest() != digest):
                raise ValueError(f'审计链校验失败：seq {event.get("seq")}')
            seq, previous = event['seq'], digest
            stage, p, step = event['stage'], event['payload'], event.get('step')
            stage_counts[stage] += 1
            first_ts = first_ts or event['timestamp']
            last_ts = event['timestamp']
            if stage == 'OBSERVED' and 'health' in p:
                # Drop image data immediately; only sensor scalars are retained.
                observation = {k: p.get(k) for k in ('health', 'sim_time_seconds', 'episode_done', 'ego_state')}
                initial_obs = initial_obs or observation
                final_obs = observation
            observer_tasks = (p.get('observer_views') or {}).get('subtask_progress_full') if stage == 'OBSERVED' else None
            if isinstance(observer_tasks, list):
                from agentlab.task_progress import normalize_rows, summary
                last_observer_tasks = normalize_rows(observer_tasks)
                task_summary = summary(last_observer_tasks)
                n, d = task_summary['done'], task_summary['total']
                if task_summary['available'] and (not progress or (n, d) != (progress[-1]['done'], progress[-1]['total'])):
                    progress.append({'decision': step, 'done': n, 'total': d, 'gc': task_summary['gc'],
                                     'timestamp': event['timestamp'], 'seq': seq})
                for row in last_observer_tasks:
                    if row.get('masked'):
                        continue
                    key, current = row['id'], row['status']
                    record = milestone_records.setdefault(key, {'id': key, 'description': row.get('description'),
                        'mandatory': row.get('mandatory', True), 'prohibited': row.get('prohibited', False),
                        'initial_observed_state': current, 'first_completed_step': None,
                        'first_completed_timestamp': None, 'revocations': 0, 'transitions': []})
                    previous_state = record.get('current')
                    if current == 'done' and record['first_completed_step'] is None:
                        record.update(first_completed_step=step, first_completed_timestamp=event['timestamp'])
                    if previous_state == 'done' and current != 'done':
                        record['revocations'] += 1
                    if previous_state != current:
                        record['transitions'].append({'step': step, 'before': previous_state, 'after': current,
                                                      'seq': seq, 'timestamp': event['timestamp']})
                    record['current'] = current
            if stage == 'RUN_STATE':
                status = p.get('status', status)
                if p.get('maintenance'):
                    maintenance.append({'seq': seq, 'step': step, 'kind': p['maintenance']})
                if p.get('auto_error'):
                    pauses.append({'seq': seq, 'step': step, 'type': p['auto_error'].get('type')})
            if stage == 'HANDOFF_APPLIED':
                apc_handoffs += 1
            if stage == 'API_REQUESTED':
                providers.add((p.get('provider'), p.get('model')))
            if stage == 'CONTEXT_BUILT' and p.get('prompt_sha256'):
                prompt_hashes.add(p['prompt_sha256'])
            if stage == 'CAPABILITY_SCORED':
                score = p
                n, d = p.get('subtasks_done'), p.get('subtasks_total')
                if n is not None and d and (not progress or (n, d) != (progress[-1]['done'], progress[-1]['total'])):
                    progress.append({'decision': step + 1 if isinstance(step, int) else None,
                                     'done': n, 'total': d, 'gc': _ratio(n, d),
                                     'timestamp': event['timestamp'], 'seq': seq})
            if stage in ('ACTION_EXECUTED', 'NAVIGATION_COMPLETED', 'PATH_RECALL_COMPLETED'):
                row = {'seq': seq, 'step': step, 'success': p.get('success'),
                       'type': p.get('decision_type', 'action'),
                       'verb': (p.get('action') or {}).get('verb'),
                       'code': p.get('feedback_code'), 'interrupted': bool(p.get('interrupted'))}
                if stage == 'ACTION_EXECUTED':
                    primitives.append(row)
                    if not p.get('navigation_substep') and row['type'] not in ('navigate', 'gt_navigate'):
                        receipts.setdefault(step, []).append(row)
                    safety = p.get('safety_event') or {}
                    if isinstance(safety.get('health_delta'), (int, float)) and safety['health_delta'] < 0:
                        damage.append({'seq': seq, 'decision': step + 1,
                                       'lost': -safety['health_delta'], 'cause': safety.get('cause')})
                    if p.get('safety_warning'):
                        warnings.append(seq)
                    if p.get('collision') is True:
                        collision_events.append(seq)
                else:
                    macro_receipts.setdefault(step, []).append(row)
            if stage in CALL_STAGES:
                role = CALL_STAGES[stage]
                calls[role] += 1
                usage = p.get('usage') or {}
                for key in ('prompt_tokens', 'completion_tokens'):
                    if isinstance(usage.get(key), (int, float)):
                        tokens[key] += usage[key]
                    else:
                        missing_usage[key] += 1
                if isinstance(p.get('latency_ms'), (int, float)):
                    model_latency_ms += p['latency_ms']
                else:
                    latency_missing += 1
            if stage == 'PROVIDER_HTTP_RETRY':
                retries[str(p.get('http_status') or 'unknown')] += 1
    if _signature(path) != identity:
        raise ValueError('轨迹在计算过程中变化，请暂停运行后重试')
    if not seq:
        raise ValueError('空轨迹不能计算指标')

    decisions, ambiguous_steps = [], []
    for step in sorted(set(receipts) | set(macro_receipts), key=lambda x: -1 if x is None else x):
        rows = macro_receipts.get(step, receipts.get(step, []))
        resolved = [r for r in rows if isinstance(r['success'], bool) and not r['interrupted']]
        if len(resolved) == 1:
            decisions.append(resolved[0])
        else:
            ambiguous_steps.append(step)
    decision_count = snapshot.get('step')
    complete_receipts = (not ambiguous_steps and decision_count == len(decisions)
                         and {r['step'] for r in decisions} == set(range(decision_count or 0)))
    failures = [r for r in decisions if r['success'] is False]
    failed_primitives = [r for r in primitives if r['success'] is False]
    unknown_primitives = [r for r in primitives if not isinstance(r['success'], bool)]
    groups = {}
    for kind in sorted({r['type'] for r in decisions}):
        rows = [r for r in decisions if r['type'] == kind]
        groups[kind] = {'attempts': len(rows), 'successes': sum(r['success'] for r in rows),
                        'failures': sum(not r['success'] for r in rows)}
    subtasks = (snapshot.get('observer_views') or {}).get('subtask_progress_full')
    if subtasks is None:
        subtasks = last_observer_tasks
    if subtasks:
        mandatory = [s for s in subtasks if s.get('mandatory', True) and not s.get('prohibited', False)]
        done, total = sum(s.get('status') == 'done' for s in mandatory), len(mandatory)
        goal_source = 'terminal_observer_full_subtasks' if (snapshot.get('observer_views') or {}).get('subtask_progress_full') else 'last_audited_observer_subtasks'
    else:
        done, total = score.get('subtasks_done'), score.get('subtasks_total')
        goal_source = 'last_CAPABILITY_SCORED_unconfirmed_full_denominator'
    task_success = score.get('task_success')
    if not isinstance(task_success, bool):
        task_success = None
    inconsistent_goal = bool(task_success is True and total and done != total)
    if inconsistent_goal:
        task_success = None
    obs = snapshot.get('observation') or final_obs
    native_safety = (snapshot.get('observer_views') or {}).get('safety_status') or {}
    initial_health = native_safety.get('initial_health', initial_obs.get('health'))
    health = obs.get('health')
    gc = _ratio(done, total) if done is not None and total is not None else None
    if subtasks:
        from agentlab.task_progress import normalize_rows, summary
        gc = summary(normalize_rows(subtasks))['gc']
    formal_eligible = snapshot.get('formal_sr_eligible', score.get('formal_sr_eligible'))
    reasons = []
    if formal_eligible is not True:
        reasons.append('运行未取得正式 SR 资格')
    if manifest.get('mixed_provider_run') or len(providers) > 1:
        reasons.append('中途更换 Provider/模型')
    if maintenance:
        reasons.append('存在维护或提示词调整记录')
    if apc_handoffs:
        reasons.append('发生 APC 特权交接')
    if not snapshot:
        reasons.append('没有与审计匹配的终态快照')
    if not complete_receipts:
        reasons.append('决策回执不完整或存在歧义，不计算决策失败率')
    if inconsistent_goal:
        reasons.append('任务成功标记与完整子任务列表冲突')
    elapsed = ((datetime.fromisoformat(last_ts) - datetime.fromisoformat(first_ts)).total_seconds())
    return {
        'version': VERSION, 'run_id': run_dir.name,
        'source': {'event_count': seq, 'audit_sha256_tail': previous, 'hash_chain_valid': True,
                   'audit_size': identity[2], 'snapshot_matched': bool(snapshot), 'snapshot_stale': snapshot_stale},
        'condition': {k: manifest.get(k) for k in (
            'scenario_id', 'scenario_digest_bound', 'seed', 'house_index', 'interface_mode', 'run_mode',
            'agent_mode', 'current_view_protocol', 'visual_frame_count', 'fog_gt_enabled',
            'depth_image_enabled', 'gt_navigation_enabled', 'path_recall_enabled', 'apc_auto',
            'auto_step_limit', 'safety_policy_version', 'context_policy', 'memory_compaction')},
        'validity': {'formal_sr_eligible': formal_eligible,
                     'single_condition_benchmark_eligible': not reasons,
                     'notes': reasons, 'providers_observed': sorted(providers, key=str),
                     'maintenance': maintenance, 'apc_handoffs': apc_handoffs,
                     'prompt_fingerprint_count': len(prompt_hashes)},
        'task': {'status': snapshot.get('status', status), 'observed_success': task_success,
                 'gc': gc, 'subtasks_done': done, 'subtasks_total': total,
                 'denominator_source': goal_source, 'progress_curve': progress,
                 'milestones': list(milestone_records.values())},
        'execution': {'decision_steps': decision_count, 'resolved_decision_receipts': len(decisions),
                      'complete_decision_receipts': complete_receipts, 'ambiguous_steps': ambiguous_steps,
                      'failed_decisions': len(failures),
                      'decision_failure_rate': _ratio(len(failures), decision_count) if complete_receipts else None,
                      'primitive_action_receipts': len(primitives), 'failed_primitives': len(failed_primitives),
                      'primitive_failure_rate': _ratio(len(failed_primitives), len(primitives)) if not unknown_primitives else None,
                      'unknown_primitive_results': len(unknown_primitives),
                      'by_decision_type': groups, 'failure_receipts': failures,
                      'sim_time_seconds': obs.get('sim_time_seconds'),
                      'reported_walk_distance_m': (obs.get('ego_state') or {}).get('cumulative_translation_m'),
                      'reported_recall_distance_m': (snapshot.get('stats_summary') or {}).get('recall_distance_m')},
        'safety': {'initial_health': initial_health, 'final_health': health,
                   'remaining_health_ratio': _ratio(health, initial_health) if health is not None else None,
                   'damage_events': len(damage), 'recorded_damage_total': sum(d['lost'] for d in damage),
                   'damage_causes': dict(Counter(d['cause'] or 'unknown' for d in damage)),
                   'warning_receipts': len(warnings), 'collision_receipts': len(collision_events),
                   'reported_collision_count': (obs.get('ego_state') or {}).get('collision_count'),
                   'damage_receipts': damage},
        'cost': {'responded_model_calls': sum(calls.values()), 'calls_by_role': dict(calls),
                 'recorded_prompt_tokens': tokens['prompt_tokens'], 'recorded_completion_tokens': tokens['completion_tokens'],
                 'usage_complete_for_recorded_responses': not missing_usage, 'missing_usage': dict(missing_usage),
                 'recorded_model_latency_seconds': round(model_latency_ms / 1000, 3),
                 'missing_latency_responses': latency_missing,
                 'recorded_http_retry_events': sum(retries.values()), 'retry_http_statuses': dict(retries),
                 'error_pauses': pauses, 'active_wall_seconds': snapshot.get('episode_active_seconds'),
                 'audit_first_to_last_seconds': round(elapsed, 3)},
        'omitted': {'TE_AE': '尚未接入同实例、同动作接口下合格的无特权 reference',
                    'NIR_IAP_IAE': '尚未验证完整必要信息集合与统一获取事件',
                    'D1_D6': '8852 临时能力映射未对齐正式 taxonomy；缺少有效判定点时不补分',
                    'SafeSR_CVR': '未验证完整致命约束事件与约束机会数；HP>0 不能替代安全成功'},
    }


def render_html(report: dict) -> str:
    """Standalone report without scripts, external assets, image data or secrets."""
    r, t, e, s, c = report, report['task'], report['execution'], report['safety'], report['cost']
    def esc(value):
        return html.escape('N/A' if value is None else str(value))
    def pct(value):
        return 'N/A' if value is None else f'{100 * value:.1f}%'
    rows = [
        ('任务结果', '通关' if t['observed_success'] is True else '未通关' if t['observed_success'] is False else '待核验'),
        ('必做子任务', f"{t['subtasks_done']} / {t['subtasks_total']} · GC {pct(t['gc'])}"),
        ('完成决策数 / 底层动作回执数', f"{e['decision_steps']} / {e['primitive_action_receipts']}"),
        ('决策失败率（环境回执，不代表模型归因）', f"{e['failed_decisions']} / {e['decision_steps']} · {pct(e['decision_failure_rate'])}"),
        ('生命值', f"{s['final_health']} / {s['initial_health']}"),
        ('扣血事件 / 已记录累计损失', f"{s['damage_events']} 次 / {s['recorded_damage_total']} HP"),
        ('有响应的模型调用', c['responded_model_calls']),
        ('已记录输入 / 输出 tokens', f"{c['recorded_prompt_tokens']:,} / {c['recorded_completion_tokens']:,}"),
        ('已记录 HTTP 重试', c['recorded_http_retry_events']),
        ('有效运行墙钟 / 审计首尾秒数', f"{c['active_wall_seconds']} / {c['audit_first_to_last_seconds']}"),
    ]
    table = ''.join(f'<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>' for k, v in rows)
    notes = '；'.join(r['validity']['notes']) or '未发现已知的配置混用标记；正式使用仍需场景与日志覆盖验收。'
    points = t['progress_curve']
    width, height, pad = 840, 200, 30
    max_step = max([e['decision_steps'] or 1] + [p['decision'] or 1 for p in points])
    x, y, coords = pad, height-pad, [f'{pad},{height-pad}']
    labels = ''
    for p in points:
        x = pad + (width-2*pad) * (p['decision'] or 0) / max_step
        coords.append(f'{x},{y}')
        y = height-pad-(height-2*pad)*p['gc']
        coords.append(f'{x},{y}')
        if p['done']:
            labels += f'<text x="{x}" y="{y-7}" text-anchor="end">第 {esc(p["decision"])} 步：{esc(p["done"])}/{esc(p["total"])}</text>'
    coords.append(f'{width-pad},{y}')
    graph = f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="子任务完成阶梯曲线"><polyline points="{" ".join(coords)}" fill="none" stroke="#dc2626" stroke-width="3"/>{labels}</svg>'
    details = html.escape(json.dumps(r, ensure_ascii=False, indent=2))
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>轨迹自动指标 · {esc(r['run_id'])}</title><style>body{{max-width:1000px;margin:32px auto;padding:0 20px;font:16px/1.65 system-ui;color:#172033;background:#f5f7fa}}section{{background:white;border:1px solid #ddd;border-radius:12px;padding:20px;margin:18px 0}}table{{width:100%;border-collapse:collapse}}td{{padding:9px;border-bottom:1px solid #eee}}td+td{{font-weight:600}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}svg{{width:100%;font-size:12px}}.note{{color:#8a4b08}}</style>
<h1>轨迹自动指标（精简版）</h1><p>{esc(r['condition'].get('scenario_id'))} · {esc(r['run_id'])} · {VERSION}</p>
<section><b>结果适用范围</b><p class="note">{esc(notes)}</p><p>单局报告展示实际通关结果；批量同条件运行才统计 SR。所有结果来自审计与匹配的终态快照，不使用模型思考自报。</p></section>
<section><table>{table}</table></section><section><h2>子任务进度</h2>{graph}<p>横轴为完成决策数，纵轴为 GC；每次变化保留源事件序号。</p></section>
<section><h2>计算口径</h2><p>选点导航或回溯算一次主决策；导航内部动作另计。API 重试和记忆压缩不增加决策步数，但计入已记录成本。动作失败率包含接口或环境失败，不能直接解释成模型能力错误率。</p><p>扣血次数不等于违规次数，冷静期可能免扣；剩余生命值不能替代 SafeSR。模型调用与 tokens 统计覆盖有响应且记录用量的请求，未返回用量的超时请求费用未知。墙钟包含 API 等待，不能作为跨模型的具身效率排名。</p></section>
<section><details><summary>完整指标、配置、逐步证据与暂缓指标</summary><pre>{details}</pre></details></section></html>'''
