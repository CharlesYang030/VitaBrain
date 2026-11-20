import json

import pytest

from agentlab.audit import AuditStore
from agentlab.trajectory_metrics import compute, render_html


def make_run(tmp_path):
    path = tmp_path / 'run_test'
    audit = AuditStore(path, path.name)
    def add(stage, payload, step=0):
        return audit.append(stage, 'evaluator_private', payload, step=step)
    add('OBSERVED', {'health': 100, 'sim_time_seconds': 0})
    add('API_REQUESTED', {'provider': 'test', 'model': 'test'})
    add('MODEL_RESPONDED', {'usage': {'prompt_tokens': 10, 'completion_tokens': 2}, 'latency_ms': 100})
    add('GT_NAVIGATION_MODEL_OUTPUT', {'usage': {'prompt_tokens': 5, 'completion_tokens': 1}, 'latency_ms': 50})
    for i in range(3):
        add('ACTION_EXECUTED', {'success': True, 'decision_type': 'gt_navigate', 'navigation_substep': i+1})
    add('NAVIGATION_COMPLETED', {'success': True, 'decision_type': 'gt_navigate'})
    add('PATH_RECALL_COMPLETED', {'success': False, 'decision_type': 'return_to_visited', 'feedback_code': 'blocked'}, 1)
    add('PROVIDER_HTTP_RETRY', {'http_status': 502}, 2)
    add('MEMORY_COMPACTION_RESPONDED', {'usage': {'prompt_tokens': 4, 'completion_tokens': 1}, 'latency_ms': 10}, 2)
    add('ACTION_EXECUTED', {'success': True, 'action': {'verb': 'put'}, 'collision': True,
                           'safety_event': {'health_delta': -2, 'cause': 'collision'}}, 2)
    add('CAPABILITY_SCORED', {'task_success': True, 'subtasks_done': 1, 'subtasks_total': 1}, 2)
    add('OBSERVED', {'health': 98, 'sim_time_seconds': 10, 'episode_done': True}, 3)
    add('RUN_STATE', {'status': 'completed'}, 3)
    snapshot = {'status': 'completed', 'step': 3, 'formal_sr_eligible': False, 'episode_active_seconds': 15,
                'observation': {'health': 98, 'sim_time_seconds': 10},
                'observer_views': {'subtask_progress_full': [{'id': 'a', 'status': 'done'}]}}
    save_snapshot(path, snapshot)
    return path, audit, snapshot


def save_snapshot(path, snapshot):
    stat = (path/'events.jsonl').stat()
    (path/'terminal-replay.json').write_text(json.dumps({'audit_identity': [stat.st_size, stat.st_mtime_ns], 'snapshot': snapshot}))


def test_macro_decisions_costs_and_safety(tmp_path):
    path, audit, _ = make_run(tmp_path)
    before = audit.events_path.read_bytes()
    r = compute(path)
    assert r['execution']['decision_steps'] == 3
    assert r['execution']['primitive_action_receipts'] == 4
    assert r['execution']['failed_decisions'] == 1
    assert r['execution']['decision_failure_rate'] == .333333
    assert r['cost']['responded_model_calls'] == 3
    assert r['cost']['recorded_prompt_tokens'] == 19
    assert r['cost']['recorded_http_retry_events'] == 1
    assert r['task']['observed_success'] is True
    assert r['task']['gc'] == 1
    assert r['task']['progress_curve'][-1]['decision'] == 3
    assert r['safety']['remaining_health_ratio'] == .98
    assert r['safety']['damage_events'] == 1
    assert not r['validity']['single_condition_benchmark_eligible']
    assert audit.events_path.read_bytes() == before


def test_tamper_rejected(tmp_path):
    path, audit, _ = make_run(tmp_path)
    audit.events_path.write_text(audit.events_path.read_text().replace('"health": 100', '"health": 99', 1))
    with pytest.raises(ValueError, match='审计链'):
        compute(path)


def test_stale_snapshot_not_used(tmp_path):
    path, audit, _ = make_run(tmp_path)
    audit.append('RUN_STATE', 'evaluator_private', {'status': 'running'}, step=3)
    r = compute(path)
    assert r['source']['snapshot_stale']
    assert r['execution']['decision_steps'] is None
    assert r['execution']['decision_failure_rate'] is None


def test_duplicate_receipts_not_silently_deduplicated(tmp_path):
    path, audit, snapshot = make_run(tmp_path)
    audit.append('ACTION_EXECUTED', 'agent_visible', {'success': True}, step=2)
    save_snapshot(path, snapshot)
    r = compute(path)
    assert r['execution']['ambiguous_steps'] == [2]
    assert r['execution']['decision_failure_rate'] is None


def test_interrupted_macro_without_result_not_counted_as_model_failure(tmp_path):
    path, audit, snapshot = make_run(tmp_path)
    audit.append('NAVIGATION_COMPLETED', 'evaluator_private', {'interrupted': True}, step=3)
    snapshot['step'] = 4
    save_snapshot(path, snapshot)
    r = compute(path)
    assert r['execution']['failed_decisions'] == 1
    assert r['execution']['decision_failure_rate'] is None


def test_missing_usage_and_mixed_provider_flagged(tmp_path):
    path, audit, snapshot = make_run(tmp_path)
    audit.append('MODEL_RESPONDED', 'evaluator_private', {'content': 'hello'}, step=3)
    audit.append('API_REQUESTED', 'evaluator_private', {'provider': 'another', 'model': 'different'}, step=3)
    save_snapshot(path, snapshot)
    r = compute(path)
    assert not r['cost']['usage_complete_for_recorded_responses']
    assert r['cost']['missing_usage']['prompt_tokens'] == 1
    assert '中途更换 Provider/模型' in r['validity']['notes']


def test_full_subtask_denominator_and_success_conflict(tmp_path):
    path, _, snapshot = make_run(tmp_path)
    snapshot['observer_views']['subtask_progress_full'].append({'id': 'hidden', 'status': 'locked', 'masked': True})
    save_snapshot(path, snapshot)
    r = compute(path)
    assert r['task']['gc'] == .5
    assert r['task']['observed_success'] is None


def test_report_escapes_text(tmp_path):
    path, _, _ = make_run(tmp_path)
    r = compute(path)
    r['validity']['notes'].append('<script>alert(1)</script>')
    report = render_html(r)
    assert '<script>' not in report
    assert '&lt;script&gt;' in report
    assert 'data:image' not in report
    r['task']['progress_curve'] = []
    assert '子任务进度' in render_html(r)
