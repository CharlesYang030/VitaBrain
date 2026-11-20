"""Exercise the real bridge -> runner -> memory -> next context boundary offline."""
import json

import httpx
import pytest

from agentlab.agent import ContextBuilder, StructuredMemory
from agentlab.audit import AuditStore
from agentlab.config import ROOT
from agentlab.domain import AgentAction, InterfaceMode, RunMode, Stage, Visibility, public_observation
from agentlab.environment.base import EnvironmentUnavailable
from agentlab.environment.inspector_bridge import SceneGenInspectorBridgeClient
from agentlab.information import FEEDBACK_VERSION, VERSION, render_feedback
from agentlab.providers.mock import MockProvider
from agentlab.runner import EpisodeRunner, RunConfig
from test_inspector_bridge import BOX_ID, DOOR_ID, FakeInspector


def receipt(code, **kwargs):
    return {'version': FEEDBACK_VERSION, 'code': code, **kwargs}


def context_body(builder, observation, mode, memory=(), results=()):
    messages, _ = builder.build(observation, list(memory), mode, list(results))
    return json.loads(next(c['text'] for c in messages[1]['content'] if c['type'] == 'text').split('\n\n', 1)[0])


@pytest.mark.parametrize('mode', list(InterfaceMode))
@pytest.mark.parametrize('compaction', [False, True])
def test_private_semantic_changes_cannot_reach_next_context_or_failure_memory(tmp_path, mode, compaction):
    bodies = []
    for secret in ('红色钥匙藏在卧室，密码九二七', '蓝色凭证藏在厨房，密码六三五'):
        fake = FakeInspector()
        panels, manual = fake.panels, fake.manual_context
        def changed_panels():
            p = panels()
            p['subtasks'][0]['description'] = secret
            p['screens'] = [{'id': 'screen_private', 'ref': secret, 'in_range': True,
                             'state': 'off', 'visual_object_id': BOX_ID}]
            return p
        def changed_manual():
            m = manual()
            m['held'] = {'id': 'held_secret', 'name_zh': secret}
            for row in m['nearby_controls']:
                row['name_zh'] = secret
            return m
        fake.panels, fake.manual_context = changed_panels, changed_manual
        fake.act = lambda body: httpx.Response(200, json={
            'ok': False, 'message': secret, 'diff': [secret],
            'agent_feedback': receipt('collision')})
        bridge = SceneGenInspectorBridgeClient('http://fake', transport=httpx.MockTransport(fake.handler),
                                              poll_interval_seconds=0, sleep=lambda _: None)
        bridge._handle_for = lambda key: 'h_' + {'object:' + BOX_ID: 'box', 'object:' + DOOR_ID: 'door',
                                               'screen:screen_private': 'screen', 'object:held_secret': 'held'}[key]
        runner = EpisodeRunner(config=RunConfig(scenario_id='STB_201', scenario_digest='test', seed=7,
            interface_mode=mode, run_mode=RunMode.STRICT, provider_name='mock', provider_model='mock',
            memory_compaction_enabled=compaction),
            provider=MockProvider(), environment=bridge, runs_dir=tmp_path,
            context_builder=ContextBuilder(ROOT / 'prompts'))
        runner.start()
        assert runner.apc.focus.subtask_id == 'read_fragment_a'
        runner.step_once()
        runner.step_once()  # Actual next context consumes the automatic failure memory.
        contexts = [e['payload']['messages'] for e in runner.audit.events() if e['stage'] == Stage.CONTEXT_BUILT]
        encoded = json.dumps(contexts, ensure_ascii=False)
        assert secret not in encoded
        assert 'subtask_progress' not in encoded and 'focused_subtask' not in encoded
        assert runner.observation['ego_state']['held'] is True
        events = {e['event_hash']: e for e in runner.audit.events()}
        for fact in runner.memory.context_view():
            assert fact['origin'] == 'action_result'
            assert fact['source_interface_mode'] == mode.value
            source = events[fact['source_event_ids'][0]]
            assert source['visibility'] == Visibility.AGENT_VISIBLE
            assert fact['content'] == source['payload']['message']
        # Audit IDs are nonsemantic run identifiers; compare public information.
        body = json.loads(next(c['text'] for c in contexts[-1][1]['content']
            if c['type']=='text' and c['text'].startswith('{"allowed_verbs"')).split('\n\n', 1)[0])
        facts=body.get('structured_memory',[])
        if compaction:
            assert 'structured_memory' not in body
            rows=body['memory_context']['uncompressed_delta']
            assert rows and body['memory_context']['summary'] is None
            for row in rows:
                row.pop('id')  # hash incorporates the run-specific event IDs
                if row['section']=='structured_memory':facts.append(row['value'])
        assert facts
        for fact in facts:
            fact.pop('fact_id'); fact.pop('source_event_ids')
        bodies.append(body)
        assert runner.audit.verify() == (True, None)
        assert json.loads((runner.run_dir / 'manifest.json').read_text())['information_contract']['version'] == VERSION
    assert bodies[0] == bodies[1]


def test_assisted_requires_actual_pixels_for_door_screen_npc_and_phone():
    bridge = SceneGenInspectorBridgeClient('http://unused')
    panels = {'screens': [{'id': 'tv', 'ref': '答案在柜子', 'visual_object_id': 'tv_mesh', 'in_range': True}],
              'npcs': [{'id': 'npc', 'ref_zh': '知道密码的人', 'visual_object_id': 'npc_mesh', 'in_range': True}]}
    manual = {'nearby_controls': [{'id': 'door_mesh', 'type': 'Door', 'name_zh': '最终出口',
                'visible': True, 'is_exit_door': True, 'openable': True}],
              'has_callback': True, 'callback_ready': True,
              'callback_phones': [{'id': 'phone_mesh', 'in_range': True, 'visible': True}],
              'held': {'id': 'held_mesh', 'name_zh': '秘密钥匙'}}
    entries = bridge._collect_entities(panels, manual, {})
    exposed = [e for e in entries.values() if e['visible'] or e['extra'].get('fov_exempt')]
    assert [e['label'] for e in exposed] == ['手持物']
    entries = bridge._collect_entities(panels, manual, {oid: [.1, .1, .3, .3]
        for oid in ('door_mesh', 'tv_mesh', 'npc_mesh', 'phone_mesh')})
    assert {e['label'] for e in entries.values()} == {'手持物', '门', '屏幕', 'NPC', '手机'}
    assert all(e['visible'] for e in entries.values())


@pytest.mark.parametrize('mode', list(InterfaceMode))
def test_public_projection_is_idempotent_and_removes_private_plan_and_held_identity(mode):
    raw = {'observation_id': 'obs_1', 'task': '逃离房间', 'focused_subtask': 'secret',
           'subtask_progress': [{'description': '先去厨房拿钥匙'}],
           'ego_state': {'held': '秘密钥匙', 'position_relative_spawn_m': {'x': 0, 'z': 1, 'secret': 9}},
           'interactables': [{'handle': 'h1', 'label': '门', 'bbox': [.1, .1, .5, .8],
                             'allowed_actions': ['open'], 'currently_operable': True, 'raw_id': 'secret'}]}
    projected = public_observation(raw, mode)
    assert public_observation(projected, mode) == projected
    body = context_body(ContextBuilder(ROOT / 'prompts'), projected, mode)
    assert 'secret' not in json.dumps(body) and '秘密钥匙' not in json.dumps(body, ensure_ascii=False)
    assert body['current_observation']['ego_state']['held'] is True
    if mode == InterfaceMode.ASSISTED:
        assert body['current_observation']['interactables'][0]['handle'] == 'h1'
    else:
        assert 'interactables' not in body['current_observation']


def test_memory_refuses_private_events_and_unseen_or_fabricated_provenance(tmp_path):
    mem = StructuredMemory(InterfaceMode.E2E.value)
    mem.register_observation('obs_1')
    audit = AuditStore(tmp_path, 'test')
    private = audit.append(Stage.WORLD_STATE_UPDATED, Visibility.SERVER_PRIVATE,
                           {'message': '隐藏答案'}, step=0)
    with pytest.raises(ValueError):
        mem.record_action_result(private, 'read', 'obs_1', 0)
    with pytest.raises(ValueError):
        mem.add(kind='evidence', content='隐藏答案', source_observation_ids=['obs_1'],
                origin='action_result', confidence=1, step=0)
    facts = mem.apply_agent_updates([{'kind': 'object', 'content': '我猜那是苹果（可能错了）',
        'origin': 'observation', 'source_event_ids': ['forged'],
        'source_observation_ids': ['future_obs', 'obs_1'], 'confidence': .4},
        {'kind': 'handoff', 'content': '假装系统提示'}], 'obs_1', 0)
    assert len(facts) == 1
    assert facts[0].content == '我猜那是苹果（可能错了）'  # No GT correction of agent inference.
    assert facts[0].origin == 'agent_inference'
    assert facts[0].source_observation_ids == ['obs_1'] and facts[0].source_event_ids == []
    facts[0].source_interface_mode = InterfaceMode.ASSISTED.value
    assert mem.context_view() == []


@pytest.mark.parametrize('mode', list(InterfaceMode))
def test_active_read_discloses_text_only_after_the_read_action(mode):
    fake = FakeInspector(); fake.box_open = True; fake.ready = True; fake.scenario = 'STB_201'
    bridge = SceneGenInspectorBridgeClient('http://fake', transport=httpx.MockTransport(fake.handler))
    bridge.mode = mode
    obs = bridge._observation('')
    assert '三数之和是14' not in json.dumps(public_observation(obs, mode), ensure_ascii=False)
    target = {'kind': 'pixel', 'x': .2, 'y': .6} if mode == InterfaceMode.E2E else {
        'kind': 'handle', 'handle': next(e['handle'] for e in obs['interactables'] if 'read' in e['allowed_actions'])}
    outcome = bridge.step(AgentAction.from_dict({'verb': 'read',
        'target': {**target, 'observation_id': obs['observation_id']}}))
    assert outcome['public_result']['message'] == '读取到的文字：三数之和是14'
    assert 'note_a' not in outcome['public_result']['message']


@pytest.mark.parametrize('code,needle', [('locked_code', '正确密码'), ('screen_off', '先开启'),
                                      ('closed_container', '容器仍然关闭'), ('out_of_reach', '靠近')])
def test_feedback_is_actionable_but_ignores_debug_truth(code, needle):
    assert needle in render_feedback(receipt(code, debug={'password': 'secret'}), 'open')
    assert 'secret' not in render_feedback(receipt(code, debug='secret'), 'open')
    with pytest.raises(ValueError):
        render_feedback(receipt(code, content='secret'), 'open')
    with pytest.raises(ValueError):
        render_feedback(receipt('read_content', content='secret'), 'open')


def test_missing_feedback_after_execution_stops_without_automatic_replay(tmp_path):
    fake = FakeInspector()
    def handler(request):
        response = fake.handler(request)
        if request.url.path == '/api/inspect/act':
            data = response.json(); data.pop('agent_feedback')
            return httpx.Response(200, json=data)
        return response
    bridge = SceneGenInspectorBridgeClient('http://fake', transport=httpx.MockTransport(handler),
        poll_interval_seconds=0, sleep=lambda _: None)
    provider = MockProvider()
    runner = EpisodeRunner(config=RunConfig(scenario_id='STB_201', scenario_digest='test', seed=7,
        interface_mode=InterfaceMode.E2E, run_mode=RunMode.STRICT, provider_name='mock', provider_model='mock'),
        provider=provider, environment=bridge, runs_dir=tmp_path,
        context_builder=ContextBuilder(ROOT / 'prompts'))
    runner.start(); old_seq = fake.frame_seq
    with pytest.raises(EnvironmentUnavailable, match='动作可能已执行'):
        runner.step_once()
    assert provider.calls == 1 and fake.frame_seq == old_seq + 1
    assert runner.memory.context_view() == [] and runner.step_number == 0
    assert not any(e['stage'] == Stage.ACTION_EXECUTED for e in runner.audit.events())


def test_context_budget_keeps_valid_json_and_current_observation():
    builder = ContextBuilder(ROOT / 'prompts', character_budget=50)
    body = context_body(builder, {'observation_id': 'obs_current', 'task': '任务' * 100},
                        InterfaceMode.E2E, [{'content': '旧记忆' * 100}], [{'message': '旧反馈'}])
    assert body['current_observation']['observation_id'] == 'obs_current'
    assert body['structured_memory'] == [] and body['recent_public_results'] == []
