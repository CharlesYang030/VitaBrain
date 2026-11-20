"""Deterministic warnings from completed public actions, never world truth."""
from __future__ import annotations

from collections import Counter
import json
import math

VERSION = 'public_history_warnings_v1'
NAVIGATION = {'move_forward', 'move_backward', 'rotate_left', 'rotate_right', 'look_up', 'look_down'}


def primitive_history(results: list[dict]) -> list[dict]:
    """Macro summaries describe primitives already present in this ledger.

    Keep summaries in the model/audit history, but don't count them again or
    treat them as interactions that interrupt a navigation diagnostic window.
    A rejected zero-action click also is not a completed physical action.
    """
    cut=max((i+1 for i,r in enumerate(results) if r.get('decision_type')=='return_to_visited' and r.get('success')), default=0)
    return [r for r in results[cut:] if isinstance(r.get('action'), dict)
            and r['action'].get('verb') not in ('navigate','return_to_visited','gt_navigate')]


def public_history_warnings(results: list[dict]) -> list[dict]:
    rows = primitive_history(results)[-12:]
    warnings = []
    def add(code, message, suggestion, count):
        warnings.append(dict(code=code, message=message, suggestion=suggestion,
                             evidence_action_count=count, source='completed_public_action_history',
                             version=VERSION))
    same_floor = sum(r.get('feedback_code') == 'already_at_requested_floor' for r in rows[-8:])
    if same_floor >= 2:
        add('repeated_same_floor', f'最近 8 个动作中有 {same_floor} 次反馈确认已经位于请求楼层。',
            '先核对已确认的楼层和现场指示，再选择观察或移动；不要继续重复同层换层。', same_floor)
    collisions = sum(r.get('collision') is True for r in rows[-8:])
    if collisions >= 3:
        add('repeated_collision', f'最近 8 个动作中发生 {collisions} 次碰撞。',
            '先观察障碍与可见通道；必要时小步后退获得转身空间，再尝试不同方向。', collisions)
    failures = []
    # Refused macro requests have no primitives, but their public failures
    # must still be eligible for repeated-attempt warnings.
    attempts = [r for r in results[-8:] if isinstance(r.get('action'), dict)]
    for r in attempts:
        if r.get('success') is not False:
            continue
        a = r['action']; t = a.get('target') or {}
        # Observation IDs change each frame, while handles identify the same
        # public object. Pixels remain frame-relative, so never equate them
        # across observations to assert identity.
        target = (t.get('kind'), t.get('handle')) if t.get('kind') == 'handle' else (
            t.get('kind'), t.get('observation_id'), t.get('x'), t.get('y'))
        failures.append(json.dumps([a.get('verb'), target, a.get('parameters') or {}, r.get('feedback_code')], sort_keys=True))
    repeated = max(Counter(failures).values(), default=0)
    if repeated >= 3:
        add('repeated_failed_action', f'最近 8 个动作中，同一种操作和失败反馈重复了 {repeated} 次。',
            '重新检查公开失败原因；改变位置、朝向或操作条件后再试，避免原样重试。', repeated)
    # A geometric loop is reported only when public odometry gives both ends
    # and all intervening actions are ordinary navigation, without a transit.
    nav = rows[-10:]
    if len(nav) == 10 and all(r['action'].get('verb') in NAVIGATION for r in nav):
        start = nav[0].get('ego_state_before') or {}
        end = nav[-1].get('ego_state_after') or {}
        a, b = start.get('position_relative_spawn_m'), end.get('position_relative_spawn_m')
        if a and b:
            net = math.hypot(b['x'] - a['x'], b['z'] - a['z'])
            travel = end.get('cumulative_translation_m', 0) - start.get('cumulative_translation_m', 0)
            turns = end.get('cumulative_rotation_deg', 0) - start.get('cumulative_rotation_deg', 0)
            if net < .4 and (travel > 2 or turns >= 360):
                add('navigation_loop', '最近 10 个动作进行了较多移动或转向，但公开里程计显示仍接近原位置。',
                    '核对已走过的方向与可见出口，选择不同的探索方向；这不代表环境已判定任务失败。', 10)
    return warnings
