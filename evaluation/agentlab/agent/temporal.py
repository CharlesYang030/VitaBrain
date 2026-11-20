"""Causal, episode-local image/text history built only from public observations."""
from __future__ import annotations

import base64
import copy
import hashlib
from collections import deque

from agentlab.domain import public_observation

VERSION = 'interleaved_recent_fpv_v1'


class VisualHistory:
    def __init__(self, mode, capacity=10):
        self.mode = mode
        self.frames = deque(maxlen=capacity)

    def observe(self, raw, step, receipt=None):
        obs = public_observation(raw, self.mode)
        oid = obs['observation_id']
        if self.frames and self.frames[-1]['observation_id'] == oid:
            return
        rgb = (obs.get('rgb') or {}).get('data_url')
        if not isinstance(rgb, str) or not rgb.startswith('data:image/'):
            return
        try:
            sha = hashlib.sha256(base64.b64decode(rgb.split(',', 1)[1], validate=True)).hexdigest()
        except (ValueError, IndexError):
            return
        self.frames.append(dict(observation_id=oid, step=step, rgb=rgb,
            source_rgb_sha256=sha, sim_time_seconds=obs.get('sim_time_seconds'),
            ego_state=copy.deepcopy(obs.get('ego_state', {})),
            arrival_result=self._receipt(receipt),
            public_feedback=copy.deepcopy(obs.get('public_feedback')),
            agent_interpretation=None, departure_result=None))

    @staticmethod
    def _receipt(receipt):
        if not isinstance(receipt, dict):
            return None
        return copy.deepcopy({k: receipt[k] for k in (
            'action', 'success', 'feedback_code', 'message', 'actual', 'collision',
            'decision_type', 'node', 'reason', 'current_goal', 'previous_observation_id', 'final_observation_id') if k in receipt})

    def annotate(self, oid, decision, receipt):
        row = next((f for f in self.frames if f['observation_id'] == oid), None)
        if row is None:
            return
        # Historical beliefs remain beliefs; no later memory state is copied back.
        row['agent_interpretation'] = {
            'origin': 'controller_state_at_that_frame' if decision.get('actor') == 'navigation_controller' else 'agent_inference_at_that_frame',
            'goal': str(decision.get('current_goal', ''))[:350],
            'summary': str(decision.get('decision_summary', ''))[:500],
            'notes': [str(v.get('content', ''))[:240] for v in
                      (decision.get('memory_updates') or [])[:3] if isinstance(v, dict)],
        }
        row['departure_result'] = self._receipt(receipt)

    def recent(self, current_oid, total_images):
        if total_images not in (1, 5, 10):
            raise ValueError('visual_frame_count must be 1, 5 or 10 (including current FPV)')
        if total_images == 1:
            return []
        # Only already observed frames, never the current frame duplicated as history.
        past = [f for f in self.frames if f['observation_id'] != current_oid]
        return copy.deepcopy(past[-(total_images - 1):])


def frame_caption(frame):
    return {k: v for k, v in frame.items() if k != 'rgb'}
