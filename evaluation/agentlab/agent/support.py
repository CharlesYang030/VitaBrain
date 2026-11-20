"""Persistent goals and progress diagnostics, with no evaluator input."""
from __future__ import annotations

import copy
from agentlab.agent.behavior import primitive_history

VERSION = 'goal_evidence_recovery_v2'


class TaskSupport:
    def __init__(self):
        self.goal = None
        self.last_attempt = None
        self.known_observations = set()
        self.revisions = []

    def register(self, oid):
        self.known_observations.add(oid)

    def update(self, decision, oid, result):
        plan = decision.get('plan_update')
        if isinstance(plan, dict):
            sources = plan.get('source_observation_ids') or [oid]
            if (isinstance(sources, list) and sources and
                    all(isinstance(s, str) and s in self.known_observations for s in sources)):
                next_goal = str(plan.get('goal', '')).strip()[:500]
                if next_goal:
                    self.goal = dict(goal=next_goal, source_observation_ids=sources[-4:],
                        expected_observable_change=str(plan.get('expected_observable_change', ''))[:500],
                        change_strategy_if=str(plan.get('change_strategy_if', ''))[:350],
                        origin='agent_plan_not_evaluator', updated_at=oid)
                    self.revisions.append(copy.deepcopy(self.goal))
                    self.revisions = self.revisions[-12:]
        elif decision.get('current_goal') and self.goal is None:
            self.goal = dict(goal=str(decision['current_goal'])[:500],
                origin='agent_plan_not_evaluator', source_observation_ids=[oid])
        self.last_attempt = dict(source_observation_id=oid,
            expected=(self.goal or {}).get('expected_observable_change'),
            action=copy.deepcopy(result.get('action')), success=result.get('success'),
            feedback_code=result.get('feedback_code'), message=result.get('message'),
            completion='not_inferred_from_action_success')

    def context(self, results):
        results = primitive_history(results)
        tail = results[-6:]
        turns = [r for r in tail if (r.get('action') or {}).get('verb') in ('rotate_left', 'rotate_right')]
        recovery = None
        if len(turns) >= 5 or sum(r.get('collision') is True for r in tail) >= 2:
            recovery = dict(required=True, reason='repeated_turns' if len(turns) >= 5 else 'repeated_collision',
                source='completed_public_actions', action_count=len(tail),
                instruction='核对预期与实际；说明下一次尝试改变了什么，或给出继续原动作的新观察依据。')
        window=[r for r in results[-12:] if isinstance(r.get('action'),dict)]
        nav={'move_forward','move_backward','rotate_left','rotate_right','look_up','look_down'}
        checks={'rotate_left','rotate_right','look_up','look_down'}
        if (recovery is None and len(window)==12
                and all(r['action'].get('verb') in nav for r in window)
                and sum(r['action'].get('verb') in checks for r in window)>=6):
            recovery=dict(required=True,reason='prolonged_visual_search',
                source='completed_public_actions',action_count=12,
                instruction='连续多次移动/转头/俯仰。列出新看清的具体证据或说明仍未看清之处；若无新证据，改变接近位置、视角或近期目标。此提醒不判定视觉观察无效，也不提供隐藏解法。')
        return dict(version=VERSION, current_plan=copy.deepcopy(self.goal),
                    last_attempt=copy.deepcopy(self.last_attempt), recovery=recovery)
