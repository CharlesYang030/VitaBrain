import json

import pytest

from agentlab.domain import InterfaceMode
from agentlab.providers.base import ProviderResult
from agentlab.runner import parse_model_json
from test_runner import make_runner


def response(verb="open", observation_id="obs_0000"):
    return {
        "decision_summary": '字符串里的括号不计数：} ] { [，以及引号 " 和反斜线 \\',
        "memory_updates": [],
        "action": {
            "verb": verb,
            "target": {"kind": "handle", "observation_id": observation_id,
                       "handle": "h_demo_door"},
            "parameters": {},
        },
    }


@pytest.mark.parametrize("missing", [1, 2])
@pytest.mark.parametrize("fenced", [False, True])
def test_missing_closers_preserve_every_value(missing, fenced):
    original = response()
    text = json.dumps(original, ensure_ascii=False)[:-missing]
    if fenced:
        text = "```json\n" + text + "\n```"
    repairs = []
    assert parse_model_json(text, repairs=repairs) == original
    assert len(repairs) == 1
    assert repairs[0]["appended"] == "}" * missing
    assert json.loads(repairs[0]["repaired_content"]) == original


def test_valid_response_needs_no_repair():
    repairs = []
    assert parse_model_json(json.dumps(response()), repairs=repairs) == response()
    assert repairs == []


@pytest.mark.parametrize("broken", [
    '{"action":{"verb":"open","target":"unfinished}}',
    '{"action":{"verb":"wait","target":null,"parameters":{},',
    '{"action":{"verb":"wait","target":null,"parameters":{}]',
    '{"action":{"verb":"wait","target":null,"parameters":{},"x":tru',
    '{"action":{"verb":"wait","target":null,"parameters":{},"x":12',
    '{"action":{"verb":"wait","verb":"done","target":null,"parameters":{}}',
    '{"action":{"verb":"wait","parameters":{}}',
])
def test_ambiguous_or_truncated_values_are_not_repaired(broken):
    repairs = []
    with pytest.raises(ValueError):
        parse_model_json(broken, repairs=repairs)
    assert repairs == []


def test_repaired_response_executes_once_and_keeps_original_audit(tmp_path):
    runner = make_runner(tmp_path, InterfaceMode.ASSISTED)
    observation = runner.start()
    broken = json.dumps(response(observation_id=observation["observation_id"]))[:-1]

    class BrokenProvider:
        calls = 0

        def complete(self, messages):
            self.calls += 1
            return ProviderResult(content=broken, raw={"content": broken})

    runner.provider = BrokenProvider()
    runner.step_once()
    events = runner.audit.events()
    assert runner.provider.calls == 1
    assert runner.step_number == 1
    assert len([e for e in events if e["stage"] == "ACTION_EXECUTED"]) == 1
    assert next(e["payload"]["content"] for e in events if e["stage"] == "MODEL_RESPONDED") == broken
    repair = next(e for e in events if e["stage"] == "MODEL_OUTPUT_REPAIRED")
    assert repair["visibility"] == "evaluator_private"
    assert repair["payload"]["appended"] == "}"
    assert runner.snapshot()["stats_summary"]["format_repairs"] == 1
    assert runner.audit.verify() == (True, None)


@pytest.mark.parametrize("overrides", [
    {"verb": "invented_action"},
    {"target": {"kind": "handle", "observation_id": "stale", "handle": "h_demo_door"}},
    {"verb": "move_forward", "target": None, "parameters": {"distance_m": 99}},
])
def test_repair_does_not_bypass_action_validation(tmp_path, overrides):
    runner = make_runner(tmp_path, InterfaceMode.ASSISTED)
    observation = runner.start()
    data = response(observation_id=observation["observation_id"])
    data["action"].update(overrides)
    broken = json.dumps(data)[:-1]

    class BrokenProvider:
        def complete(self, messages):
            return ProviderResult(content=broken, raw={"content": broken})

    runner.provider = BrokenProvider()
    with pytest.raises(ValueError):
        runner.step_once()
    assert runner.observation == observation and runner.step_number == 0
    assert not any(e["stage"] == "ACTION_EXECUTED" for e in runner.audit.events())
    assert runner.step_stats[-1]["validation_error"]["error_type"] == "ValueError"
