from agentlab.agent import ContextBuilder
from agentlab.config import ROOT
from agentlab.domain import InterfaceMode, RunMode
from agentlab.environment import MockEnvironmentClient
from agentlab.providers.mock import MockProvider
from agentlab.runner import EpisodeRunner, RunConfig


def test_grounding_fault_pauses_without_model_action_failure_or_memory_update(tmp_path, monkeypatch):
    import pytest
    from agentlab.environment.base import TargetGroundingError
    runner = make_runner(tmp_path)
    before = runner.start()
    memory = runner.memory.snapshot()
    def fail(action):
        raise TargetGroundingError({'status': 'semantic_target_ambiguous',
                                    'failure_class': 'interface_error'})
    monkeypatch.setattr(runner.environment, 'step', fail)
    with pytest.raises(TargetGroundingError):
        runner.step_once()
    events = runner.audit.events()
    assert not any(e['stage'] == 'ACTION_EXECUTED' for e in events)
    assert runner.step_number == 0 and runner.observation == before
    assert runner.memory.snapshot() == memory
    proof = next(e['payload'] for e in events if e['stage'] == 'TARGET_RESOLVED')
    assert proof['counts_as_model_action_failure'] is False
    assert proof['execution_started'] is False
    assert runner.audit.verify() == (True, None)


def make_runner(tmp_path, mode=InterfaceMode.E2E, run_mode=RunMode.STRICT, timeout=300):
    env = MockEnvironmentClient()
    scenario = env.scenarios()[0]
    return EpisodeRunner(
        config=RunConfig(
            scenario_id=scenario["scenario_id"], scenario_digest=scenario["digest"], seed=5,
            interface_mode=mode, run_mode=run_mode, provider_name="mock",
            provider_model="deterministic-mock-v1", focused_timeout_seconds=timeout,
        ), provider=MockProvider(), environment=env, runs_dir=tmp_path,
        context_builder=ContextBuilder(ROOT / "prompts"),
    )


def test_runner_full_trace_has_no_public_truth_leak(tmp_path):
    runner = make_runner(tmp_path, InterfaceMode.ASSISTED)
    observation = runner.start()
    assert observation["interactables"][0]["handle"] == "h_demo_door"
    assert "objectId" not in str(observation) and "distance" not in str(observation)
    runner.step_once()
    stages = [item["stage"] for item in runner.audit.events()]
    assert "CONTEXT_BUILT" in stages and "MODEL_RESPONDED" in stages and "CAPABILITY_SCORED" in stages
    assert runner.audit.verify() == (True, None)


def test_apc_clock_paused_until_first_step(tmp_path):
    import time
    runner = make_runner(tmp_path, run_mode=RunMode.ADAPTIVE, timeout=300)
    runner.start()
    # 加载后秒表应冻结：等待真实时间不应累加 active wall time
    assert runner.apc.snapshot()["active_seconds"] == 0
    time.sleep(0.05)
    assert runner.apc.snapshot()["active_seconds"] == 0
    # 停止后再次冻结
    runner.step_once()
    runner.pause_clock()
    frozen = runner.apc.snapshot()["active_seconds"]
    time.sleep(0.05)
    assert runner.apc.snapshot()["active_seconds"] == frozen


def test_apc_permanently_revokes_formal_sr(tmp_path):
    runner = make_runner(tmp_path, run_mode=RunMode.ADAPTIVE, timeout=0)
    runner.start()
    runner.step_once()
    assert runner.assist_count == 1
    assert runner.formal_sr_eligible is False
    assert runner.capability_status["D3.1"] == "failed_or_timed_out"


def test_handoff_refreshes_observation_before_next_model_call(tmp_path):
    runner = make_runner(tmp_path, run_mode=RunMode.ADAPTIVE)
    runner.start()
    original = runner.environment.apply_handoff
    def apply(sid):
        result = original(sid)
        result['observation'] = {**runner.observation,
                                 'observation_id':'after_assist',
                                 'focused_subtask':'mock_finish'}
        result['postconditions_verified'] = False
        return result
    runner.environment.apply_handoff = apply
    runner.force_debug_handoff()
    assert runner.observation['observation_id'] == 'after_assist'
    assert runner.assist_count == 1  # failed attempts still taint and count
    assert runner.apc.focus.subtask_id == 'mock_finish'


def test_terminal_handoff_skip_keeps_agent_running_without_counting_assistance(tmp_path):
    runner = make_runner(tmp_path, run_mode=RunMode.ADAPTIVE)
    runner.start()
    old = runner.observation
    runner.environment.apply_handoff = lambda sid: {
        'applied':False, 'next_subtask':sid, 'reason':'no_intermediate_handoff_recipe'}
    runner.force_debug_handoff()
    assert runner.assist_count == 0
    assert runner.observation == old
    assert runner.status == 'running'
    assert runner.audit.events()[-1]['stage'] == 'HANDOFF_SKIPPED'
