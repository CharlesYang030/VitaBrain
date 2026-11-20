"""Evaluator-only task progress. Never add these rows to model observations.

Package state is sampled after normal actions; this adapter does not execute
actions, change the original success predicate, or grant completion from prose.
"""
from __future__ import annotations

VERSION = 'task_progress_v1'
PLACEHOLDERS = {'dedicated_runtime_required', 'full_story_not_installed', 'native_table_binding_seed'}
PACKAGE_TASKS = {
    'STB_279': [('A1', '浴巾归位'), ('A2', '洗漱包归位'), ('A3', '拖鞋归位')],
    'STB_280': [('B1', '关闭电视'), ('B2', '关闭收音机'), ('B3', '风扇低档')],
    'STB_281': [('A1', '牛奶冷藏并关门'), ('A2', '面包收好并合盖'), ('A3', '杯子归位'), ('A4', '包装回收'), ('A5', '有效汇报')],
    'STB_282': [('B1', '装入打印纸'), ('B2', '打印照片'), ('B3', '照片摆放'), ('B4', '打印机收尾'), ('B5', '有效汇报')],
    'STB_283': [('A1', '关闭电视'), ('A2', '关闭灯光'), ('A3', '风扇低档'), ('A4', '关好卧室门'), ('A5', '有效汇报')],
    'STB_284': [('B1', '方块归位'), ('B2', '圆柱归位'), ('B3', '玩具车归位'), ('B4', '合盖收尾'), ('B5', '有效汇报')],
    'STB_285': [('A1', '食材冷藏'), ('A2', '关闭冷藏柜'), ('A3', '洁净盘归位'), ('A4', '脏碗归位'), ('A5', '空盒回收'), ('A6', '有效汇报')],
    'STB_286': [('B1', '枕头摆放'), ('B2', '饮水摆放'), ('B3', '关闭电视'), ('B4', '风扇低档'), ('B5', '窗帘合拢'), ('B6', '灯光调好'), ('B7', '有效汇报')],
    'STB_287': [('coffee', '制作热咖啡'), ('toast', '烤好吐司'), ('served_and_off', '摆盘并关闭电器'), ('reported', '有效汇报')],
    'STB_288': [('basil', '浇灌罗勒'), ('mint', '浇灌薄荷'), ('tools_and_off', '工具归位并关闭龙头'), ('reported', '有效汇报')],
    'STB_289': [('A1', '关闭电视'), ('A2', '关闭灯光'), ('A3', '电脑关机合盖'), ('A4', '关闭冰箱门')],
}


def package_rows(scenario_id, state):
    """Map a package's actual private judge/state to its authored task count."""
    definitions = PACKAGE_TASKS.get(scenario_id)
    if not definitions or not isinstance(state, dict):
        return []
    values = state
    for key in ('subtasks', 'nodes', 'goals'):
        if isinstance(state.get(key), dict):
            values = state[key]
            break
    values = dict(values)
    def conjunction(*keys):
        if any(values.get(k) is False for k in keys):
            return False
        return True if all(values.get(k) is True for k in keys) else None
    if scenario_id == 'STB_287':
        values['served_and_off'] = conjunction('served', 'devices_off')
    if scenario_id == 'STB_288':
        values['tools_and_off'] = conjunction('tools', 'faucet_off')
    rows = []
    for index, (key, label) in enumerate(definitions):
        value = values.get(key)
        requires = []
        if key in ('A5', 'B5') and scenario_id in ('STB_281', 'STB_282', 'STB_283', 'STB_284') or key in ('A6', 'B7', 'reported'):
            requires = [k for k, _ in definitions[:index]]
        elif scenario_id == 'STB_284' and key == 'B4':
            requires = ['B1', 'B2', 'B3']
        elif scenario_id == 'STB_285' and key == 'A2':
            requires = ['A1']
        elif scenario_id == 'STB_282' and key == 'B2':
            requires = ['B1']
        elif scenario_id == 'STB_282' and key in ('B3', 'B4'):
            requires = ['B2']
        rows.append(dict(id=key, description=label, status='done' if value is True else 'pending' if value is False else 'unknown',
                         mandatory=True, prohibited=False, masked=False, requires=requires,
                         evidence_source='package_private_state', evidence_keys=(['served', 'devices_off'] if key == 'served_and_off' else ['tools', 'faucet_off'] if key == 'tools_and_off' else [key])))
    return rows


def normalize_rows(rows):
    """Keep evaluator metadata and refuse duplicate/missing identities."""
    if not isinstance(rows, list):
        return []
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not row.get('id') or str(row['id']) in seen:
            return []
        seen.add(str(row['id']))
        placeholder = str(row['id']) in PLACEHOLDERS
        result.append({**row, 'id': str(row['id']), 'status': 'unknown' if placeholder else row.get('status') or 'unknown',
                       **({'evidence_source': 'runtime_placeholder'} if placeholder else {}),
                       'mandatory': row.get('mandatory', True), 'prohibited': row.get('prohibited', False)})
    return result


def from_panels(scenario_id, panels):
    explicit = panels.get('evaluation_progress')
    if isinstance(explicit, dict) and isinstance(explicit.get('subtasks'), list):
        return normalize_rows(explicit['subtasks']), 'evaluation_progress'
    if isinstance(explicit, dict) and isinstance(explicit.get('milestones'), list):
        return normalize_rows([dict(id=r['id'], description=r.get('name', r['id']),
                                    status=r.get('state', 'unknown'), mandatory=True,
                                    requires=r.get('requires', []),
                                    first_completed_at=r.get('first_completed_at'),
                                    revocations=r.get('revocations'), evidence_seq=r.get('evidence_seq'),
                                    evidence_source='evaluation_progress') for r in explicit['milestones']]), 'evaluation_progress'
    if panels.get('author_subtasks'):
        return normalize_rows(panels['author_subtasks']), 'author_subtasks'
    if scenario_id in PACKAGE_TASKS:
        raw = panels.get('private_state') or panels.get('private_judge') or panels.get('judge')
        if raw is None and any(isinstance(panels.get(k), dict) for k in ('nodes', 'goals', 'subtasks')):
            raw = panels
        if raw is None and isinstance(panels.get('state'), dict):
            raw = panels['state']
        if raw is not None:
            return package_rows(scenario_id, raw), 'package_private_state'
    return normalize_rows(panels.get('subtasks', [])), 'panels.subtasks'


def summary(rows, source=None):
    tasks = [r for r in rows if r.get('mandatory', True) and not r.get('prohibited', False)]
    unknown = sum(r.get('status') in (None, 'unknown', 'unobserved') for r in tasks)
    done = sum(r.get('status') == 'done' for r in tasks)
    available = bool(tasks) and unknown == 0
    return dict(version=VERSION, available=available, source=source,
                done=done if tasks else None, total=len(tasks) if tasks else None,
                unknown=unknown, gc=round(done/len(tasks), 6) if available else None,
                reason=None if available else 'unknown_task_state' if tasks else 'no_task_progress')



# Scene-owned, evaluator-only definitions also identify unknown historical rows.
import json as _json
from pathlib import Path as _Path
_catalog_path = _Path(__file__).parent / 'task_catalog.json'
# Full scene metadata is distributed separately from this source release.
TASK_CATALOG = _json.loads(_catalog_path.read_text()) if _catalog_path.exists() else {}
_from_panels_original = from_panels

def catalog_rows(scenario_id, observed):
    definitions = TASK_CATALOG.get(scenario_id)
    if not definitions:
        return observed
    by_id = {r['id']:r for r in observed}
    # Live dynamic recipes/task counts are authoritative when a full evaluator
    # interface exists. Catalog matching is used only for known stable IDs.
    if any(r.get('evidence_source') in ('evaluation_progress','package_current_predicates') for r in observed):
        labels = {r['id']:r['description'] for r in definitions}
        return [{**r, 'description': labels.get(r['id'], r.get('description', r['id']))} for r in observed]
    return [{**d, **by_id.get(d['id'], {}), 'description': d['description'],
             **({'status':'unknown','evidence_source':'definition_only'} if d['id'] not in by_id else {})}
            for d in definitions]

def from_panels(scenario_id, panels):
    rows, source = _from_panels_original(scenario_id, panels)
    expanded = catalog_rows(scenario_id, rows)
    if expanded != rows and any(r.get('evidence_source')=='definition_only' for r in expanded):
        source = 'task_definitions_missing_current_evidence'
    return normalize_rows(expanded), source
