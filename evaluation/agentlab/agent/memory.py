from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from agentlab.domain import Stage, Visibility


MEMORY_KINDS = {"task", "evidence", "object", "spatial", "world_state", "failure", "handoff"}
MEMORY_ORIGINS = {"observation", "agent_inference", "action_result", "privileged_assist"}


@dataclass
class MemoryFact:
    fact_id: str
    kind: str
    content: str
    source_observation_ids: list[str]
    origin: str
    confidence: float
    last_confirmed_step: int
    assisted_lineage: list[str]
    status: str = "active"
    source_event_ids: list[str] = field(default_factory=list)
    source_interface_mode: str | None = None
    state_key: str | None = None
    revision_key: str | None = None
    validity: str = 'persistent'


class StructuredMemory:
    def __init__(self, interface_mode: str | None = None) -> None:
        self._facts: dict[str, MemoryFact] = {}
        self.interface_mode = interface_mode
        self._observations: set[str] = set()

    def register_observation(self, observation_id: str) -> None:
        if observation_id not in self._observations:
            for fact in self._facts.values():
                if fact.origin == 'agent_inference' and fact.validity == 'current_view' and fact.status == 'active':
                    fact.status = 'expired'
        self._observations.add(observation_id)

    def add(
        self, **kwargs: Any,
    ) -> MemoryFact:
        # Automatic facts must come through record_action_result, which copies
        # a public audit receipt. No arbitrary text may claim observed origin.
        if kwargs.get('origin') != 'agent_inference':
            raise ValueError('environment facts require a public source event')
        if kwargs.get('kind') == 'handoff':
            raise ValueError('handoff facts require privileged provenance')
        sources = kwargs.get('source_observation_ids') or []
        if not sources or any(s not in self._observations for s in sources):
            raise ValueError('memory cites an observation not delivered to this agent')
        return self._store(**kwargs)

    def _store(
        self,
        *,
        kind: str,
        content: str,
        source_observation_ids: list[str],
        origin: str,
        confidence: float,
        step: int,
        assisted_lineage: list[str] | None = None,
        fact_id: str | None = None,
        source_event_ids: list[str] | None = None,
        state_key: str | None = None,
        revision_key: str | None = None,
        validity: str = 'persistent',
    ) -> MemoryFact:
        if kind not in MEMORY_KINDS:
            raise ValueError(f"unknown memory kind: {kind}")
        if origin not in MEMORY_ORIGINS:
            raise ValueError(f"unknown memory origin: {origin}")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        if validity not in ('persistent', 'current_view'):
            raise ValueError('invalid memory validity')
        if revision_key is not None and (origin != 'agent_inference' or not isinstance(revision_key,str)
                or not revision_key.strip() or len(revision_key)>100):
            raise ValueError('revision key must be an agent-defined short string')
        content = content[:2000]
        # Coalesce exact repeats only. Never infer semantic equivalence or use
        # private truth to correct an agent's statement.
        for old in self._facts.values():
            if (old.status == 'active' and old.source_interface_mode == self.interface_mode
                    and (old.kind, old.origin, old.content, old.state_key, old.revision_key, old.validity)
                    == (kind, origin, content, state_key, revision_key, validity)):
                old.last_confirmed_step = step
                old.confidence = confidence
                old.source_observation_ids = list(dict.fromkeys(old.source_observation_ids + source_observation_ids))[-4:]
                old.source_event_ids = list(dict.fromkeys(old.source_event_ids + (source_event_ids or [])))[-4:]
                return old
        if revision_key:
            for old in self._facts.values():
                if (old.origin == 'agent_inference' and old.revision_key == revision_key
                        and old.source_interface_mode == self.interface_mode and old.status == 'active'):
                    old.status = 'superseded'
        if state_key:
            if origin != 'action_result':
                raise ValueError('durable state requires a public action receipt')
            for old in self._facts.values():
                if old.state_key == state_key and old.source_interface_mode == self.interface_mode:
                    old.status = 'superseded'
        fact = MemoryFact(
            fact_id=fact_id or f"mem_{uuid.uuid4().hex[:10]}",
            kind=kind,
            content=content[:2000],
            source_observation_ids=list(dict.fromkeys(source_observation_ids)),
            origin=origin,
            confidence=confidence,
            last_confirmed_step=step,
            assisted_lineage=list(dict.fromkeys(assisted_lineage or [])),
            source_event_ids=list(source_event_ids or []),
            source_interface_mode=self.interface_mode,
            state_key=state_key,
            revision_key=revision_key, validity=validity,
        )
        self._facts[fact.fact_id] = fact
        return fact

    def apply_agent_updates(self, updates: list[dict[str, Any]], observation_id: str, step: int) -> list[MemoryFact]:
        accepted: list[MemoryFact] = []
        for update in updates[:20]:
            if not isinstance(update, dict):
                continue
            # A model can revise its own inference, never erase public evidence.
            retire = update.get('retract') or update.get('supersedes')
            previous = self._facts.get(retire) if isinstance(retire, str) else None
            if retire and (previous is None or previous.origin != 'agent_inference'):
                continue
            if update.get('retract'):
                sources = update.get('source_observation_ids') or [observation_id]
                if isinstance(sources, list) and sources and all(s in self._observations for s in sources):
                    previous.status = 'retracted'
                continue
            content = str(update.get("content", "")).strip()
            if not content:
                continue
            sources = update.get("source_observation_ids") or [observation_id]
            if not isinstance(sources, list):
                sources = [observation_id]
            sources = [s for s in sources if isinstance(s, str) and s in self._observations]
            if observation_id not in sources:
                sources.append(observation_id)
            try:
                fact = self.add(
                    kind=str(update.get("kind", "evidence")),
                    content=content,
                    source_observation_ids=[str(item) for item in sources],
                    origin="agent_inference",
                    confidence=float(update.get("confidence", 0.5)),
                    step=step,
                    revision_key=update.get('revision_key'),
                    validity=update.get('validity','persistent'),
                )
                accepted.append(fact)
                if previous is not None and previous.fact_id != fact.fact_id:
                    previous.status = 'superseded'
            except (TypeError, ValueError):
                continue
        return accepted

    def record_action_result(self, event: dict[str, Any], verb: str,
                             observation_id: str, step: int) -> list[MemoryFact]:
        if (not (event.get('stage') == Stage.ACTION_EXECUTED or
                 (event.get('stage') == Stage.PATH_RECALL_COMPLETED and verb == 'return_to_visited' and (event.get('payload',{}).get('action') or {}).get('verb')=='return_to_visited') or
                 (event.get('stage') == Stage.NAVIGATION_COMPLETED and verb in ('navigate','gt_navigate')
                  and (event.get('payload', {}).get('action') or {}).get('verb') == verb))
                or event.get('visibility') != Visibility.AGENT_VISIBLE
                or not event.get('event_hash') or event.get('step') != step
                or observation_id not in self._observations):
            raise ValueError('memory requires the current public action event')
        payload = event['payload']
        state_key = None
        content = payload.get('message')
        kind = 'evidence' if payload.get('success') else 'failure'
        if verb == 'return_to_visited' and payload.get('success'):
            kind, state_key = 'spatial', 'last_confirmed_recall'
        if verb in ('use_stairs', 'use_elevator'):
            floor = ((payload.get('actual') or {}).get('floor') if payload.get('success')
                     else (payload.get('requested') or {}).get('floor')
                     if payload.get('feedback_code') == 'already_at_requested_floor' else None)
            if type(floor) is int and 1 <= floor <= 100:
                content = f'公开换层操作已确认当前为 {floor} 层。'
                kind, state_key = 'world_state', 'confirmed_floor'
        if payload.get('success') and not state_key and verb not in (
                'read', 'screen_cycle', 'ask_npc', 'answer_npc', 'answer_call',
                'pickup', 'put', 'drop', 'open', 'close', 'unlock', 'input_code',
                'fill_water', 'handoff_npc', 'toggle_on', 'toggle_off'):
            return []
        if not isinstance(content, str) or not content:
            return []
        # Object-operation receipts retain the model's original public target
        # reference as historical evidence, never an internal engine object ID.
        action = payload.get('action') or {}
        if payload.get('success') and not state_key and verb in (
                'pickup', 'put', 'drop', 'open', 'close', 'unlock', 'input_code',
                'fill_water', 'handoff_npc', 'toggle_on', 'toggle_off'):
            import json
            target = action.get('target')
            content = f'已执行 {verb}；当时公开目标：{json.dumps(target, ensure_ascii=False)}。{content}'
            if verb in ('pickup', 'put', 'drop', 'handoff_npc'):
                kind, state_key = 'world_state', 'confirmed_hand'
            elif verb in ('open', 'close') and isinstance(target, dict) and target.get('kind') == 'handle' and target.get('handle'):
                kind, state_key = 'world_state', 'door_state:' + str(target['handle'])

        return [self._store(
            kind=kind,
            content=content, source_observation_ids=[observation_id],
            source_event_ids=[event['event_hash']], origin='action_result',
            confidence=1.0, step=step, state_key=state_key)]

    def add_handoff(self, event_id: str, facts: list[dict[str, Any]], step: int) -> list[MemoryFact]:
        added = []
        for fact in facts:
            added.append(self._store(
                kind=str(fact.get("kind", "handoff")),
                content=str(fact["content"]),
                source_observation_ids=list(fact.get("source_observation_ids", [])),
                origin="privileged_assist",
                confidence=1.0,
                step=step,
                assisted_lineage=[event_id],
                source_event_ids=[event_id],
            ))
        return added

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        grouped = {kind: [] for kind in sorted(MEMORY_KINDS)}
        for fact in self._facts.values():
            grouped[fact.kind].append(asdict(fact))
        return grouped

    def context_view(self, max_facts: int = 40, *, query: str = '') -> list[dict[str, Any]]:
        facts = sorted((item for item in self._facts.values()
                        if item.source_interface_mode == self.interface_mode and item.status == 'active'),
                       key=lambda item: item.last_confirmed_step, reverse=True)
        if max_facts <= 0:
            return []
        selected: list[MemoryFact] = []
        def reserve(predicate, count):
            for item in [f for f in facts if predicate(f)][:count]:
                if item not in selected and len(selected) < max_facts:
                    selected.append(item)
        reserve(lambda f: f.state_key in ('confirmed_floor', 'confirmed_hand'), 2)
        reserve(lambda f: bool(f.state_key) and f.state_key not in ('confirmed_floor', 'confirmed_hand'), 6)
        reserve(lambda f: f.origin == 'action_result' and f.kind == 'evidence', 12)
        if query:
            # Goal-conditioned retrieval from the agent's own text, not task GT.
            terms={query[i:i+2] for i in range(len(query)-1) if not query[i:i+2].isspace()}
            ranked=sorted(facts,key=lambda f: (sum(t in f.content for t in terms),f.last_confirmed_step),reverse=True)
            for item in ranked[:8]:
                if item not in selected and len(selected)<max_facts:
                    selected.append(item)
        reserve(lambda f: f.kind == 'task', 4)
        reserve(lambda f: f.kind == 'world_state', 4)
        reserve(lambda f: f.kind == 'spatial', 4)
        reserve(lambda f: f.kind == 'failure', 4)
        reserve(lambda f: True, max_facts)
        return [asdict(item) for item in selected]
