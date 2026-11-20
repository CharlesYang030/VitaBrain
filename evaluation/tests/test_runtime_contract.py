from agentlab.runtime_mock.app import create_runtime_app


def test_runtime_http_contract_and_continuous_action():
    client = create_runtime_app().test_client()
    interface = client.get("/v1/interface")
    assert interface.status_code == 200
    assert interface.json["assisted_interactables"] == "currently_operable_only"
    created = client.post("/v1/episodes", json={
        "scenario_id": "mock_long_horizon_001", "seed": 5,
        "interface_mode": "affordance_assisted", "protocol_version": "1.0",
    })
    assert created.status_code == 201
    episode_id = created.json["episode_id"]
    result = client.post(f"/v1/episodes/{episode_id}/step", json={
        "action": {"verb": "rotate_right", "target": None, "parameters": {"degrees": 6.5}},
    })
    assert result.status_code == 200
    assert result.json["public_result"]["actual"]["degrees"] == 6.5

