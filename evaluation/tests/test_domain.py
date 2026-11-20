import pytest

from agentlab.domain import AgentAction, InterfaceMode, public_observation, validate_action_for_interface


def test_continuous_motion_parameters_are_required_and_bounded():
    action = AgentAction.from_dict({"verb": "move_forward", "parameters": {"distance_m": 0.12}})
    assert action.parameters["distance_m"] == 0.12
    with pytest.raises(ValueError):
        AgentAction.from_dict({"verb": "move_forward", "parameters": {}})
    with pytest.raises(ValueError):
        AgentAction.from_dict({"verb": "rotate_left", "parameters": {"degrees": 180}})


def test_assisted_is_fail_closed_and_strips_private_fields():
    raw = {
        "observation_id": "obs_1", "step": 1, "rgb": {}, "task": "x", "public_feedback": "",
        "interactables": [
            {"handle": "near", "bbox": [0, 0, 1, 1], "allowed_actions": ["open"], "currently_operable": True, "objectId": "secret", "distance": 1.2},
            {"handle": "same_floor_but_far", "bbox": [0, 0, 1, 1], "allowed_actions": ["open"], "currently_operable": False},
            {"handle": "unconfirmed", "bbox": [0, 0, 1, 1], "allowed_actions": ["open"]},
        ],
    }
    result = public_observation(raw, InterfaceMode.ASSISTED)
    assert [item["handle"] for item in result["interactables"]] == ["near"]
    assert "objectId" not in str(result) and "distance" not in str(result)


def test_e2e_rejects_handle_and_stale_frames():
    action = AgentAction.from_dict({"verb": "open", "target": {"kind": "handle", "observation_id": "obs_1", "handle": "h1"}})
    with pytest.raises(ValueError):
        validate_action_for_interface(action, InterfaceMode.E2E, "obs_1")


def test_escape_room_verbs_require_target_and_code():
    target = {"kind": "handle", "observation_id": "obs_1", "handle": "h1"}
    action = AgentAction.from_dict({
        "verb": "input_code", "target": target, "parameters": {"code": "374"}})
    assert action.parameters["code"] == "374"
    with pytest.raises(ValueError):   # code is mandatory
        AgentAction.from_dict({"verb": "input_code", "target": target})
    with pytest.raises(ValueError):   # target is mandatory for read
        AgentAction.from_dict({"verb": "read"})
    for verb in ("read", "unlock", "screen_on", "screen_off"):
        assert AgentAction.from_dict({"verb": verb, "target": target}).verb == verb

