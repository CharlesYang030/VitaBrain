from agentlab.security import scan_events_for_leaks


def test_compaction_provider_envelope_is_not_a_scene_leak_but_output_is_scanned():
    event = {'visibility': 'agent_output', 'stage': 'MEMORY_COMPACTION_RESPONDED',
             'payload': {'content': '{"entries":[]}', 'raw': {'id': 'resp_1234abcdef'}}}
    assert scan_events_for_leaks([event])['clean']
    event['payload']['content'] = 'found Mug|12|34'
    assert not scan_events_for_leaks([event])['clean']
    event['payload']['content'] = 'found mug_1234abcdef'
    assert not scan_events_for_leaks([event])['clean']
    assert event['payload']['raw']['id'] == 'resp_1234abcdef'


def test_screen_channel_and_spawned_ids_are_detected_but_handles_are_public():
    events = [{"seq": 1, "stage": "OBSERVED", "visibility": "agent_visible",
               "payload": {"public_feedback": "screen_b345 apple_8db2 bowl_caeb fx_safe_00",
                           "observation_id": "obs_0010", "handle": "h_abcd1234"}}]
    scan = scan_events_for_leaks(events)
    assert not scan["clean"]
    assert {p["excerpt"] for p in scan["problems"]} == {
        "screen_b345", "apple_8db2", "bowl_caeb", "fx_safe_00"}
    events[0]["payload"] = {"observation_id": "obs_0010", "handle": "h_abcd1234",
                             "memory_id": "mem_c0dd56816c", "run_id": "run_abcd1234",
                             "public_feedback": "屏幕画面已切换，请观察当前图像。"}
    assert scan_events_for_leaks(events)["clean"]
