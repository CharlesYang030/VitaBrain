"""Contract tests for the 8851 development bridge (no real inspector needed).

A stateful fake 8851 simulates a miniature STB_201: a closed box with note A,
a closed safe with note B, and a combo-locked exit door (code 374).  The tests
pin the strict public/private boundary and the fail-closed resolution rules.
"""
from __future__ import annotations

import json
import base64
import hashlib

import httpx
from agentlab.information import VERSION as INFORMATION_VERSION, FEEDBACK_VERSION
import pytest

from agentlab.domain import AgentAction, InterfaceMode
from agentlab.environment.inspector_bridge import SceneGenInspectorBridgeClient, GROUNDING_VERSION

BOX_ID = "Box|+00.10|+00.50|+01.20"
SAFE_ID = "Safe|+02.10|+00.40|+03.60"
DOOR_ID = "door|exit|north"


def test_mutating_request_waits_for_slow_action_and_never_replays_timeout():
    attempts = []
    def handler(request):
        attempts.append(request)
        raise httpx.ReadTimeout('late environment receipt', request=request)
    bridge = SceneGenInspectorBridgeClient('http://inspector.invalid',
        transport=httpx.MockTransport(handler))
    bridge._frame_token = 'current-frame'
    with pytest.raises(httpx.ReadTimeout):
        bridge._act({'op': 'manual_pickup', 'id': 'target'})
    assert len(attempts) == 1
    assert attempts[0].extensions['timeout']['read'] == 600
    assert attempts[0].extensions['timeout']['connect'] == 120


def test_late_receipt_finalization_reads_state_without_dispatch(monkeypatch):
    bridge = SceneGenInspectorBridgeClient('http://inspector.invalid')
    observation = {'episode_done': False, 'subtask_progress': [], 'ego_state': {'held': True}}
    reads = []
    def observe(feedback):
        reads.append(feedback)
        return observation
    def forbidden(*args, **kwargs):
        raise AssertionError('receipt reconciliation must not execute another action')
    monkeypatch.setattr(bridge, '_observation', observe)
    monkeypatch.setattr(bridge, '_act', forbidden)
    action = AgentAction.from_dict({'verb': 'pickup', 'parameters': {},
        'target': {'kind': 'handle', 'observation_id': 'obs_0001', 'handle': 'h_target'}})
    outcome = bridge.finalize_action_receipt(action,
        {'ok': True, 'message': 'private engine text', 'diff': [],
         'agent_feedback': {'version': FEEDBACK_VERSION, 'code': 'succeeded'}},
        {'status': 'resolved', 'private_object_id': 'private-target'}, {}, {}, False, {})
    assert len(reads) == 1
    assert outcome['observation']['ego_state']['held'] is True
    assert outcome['public_result']['success'] is True
    assert outcome['public_result']['message'] == reads[0]
    assert 'private' not in str(outcome['public_result'])


def test_pet_pixels_do_not_advertise_unavailable_conversation():
    bridge = SceneGenInspectorBridgeClient('http://unused')
    panels = {'npcs': [dict(id='dog', visual_object_id='dog_mesh',
                           in_range=True, queryable=False),
                        dict(id='human', visual_object_id='human_mesh',
                           in_range=True, queryable=True)]}
    entries = bridge._collect_entities(panels, {},
        {'dog_mesh': [.1,.1,.3,.4], 'human_mesh': [.5,.1,.8,.9]})
    assert [e['op_id'] for e in entries.values()] == ['human']
    assert set(next(iter(entries.values()))['verbs']) == {'ask_npc','answer_npc'}


def test_e2e_callback_dispatches_the_selected_physical_phone(monkeypatch):
    bridge = SceneGenInspectorBridgeClient('http://unused')
    bridge.mode = InterfaceMode.E2E
    entries = bridge._collect_entities({}, {'has_callback': True, 'callback_ready': True,
        'callback_phones': [{'id': 'near_phone', 'in_range': True, 'visible': True},
                            {'id': 'chosen_phone', 'in_range': True, 'visible': True}]}, {})
    selected = next(e for e in entries.values() if e['raw_id'] == 'chosen_phone')
    bodies = []
    monkeypatch.setattr(bridge, '_act', lambda body: bodies.append(body))
    action = AgentAction.from_dict({'verb': 'answer_call', 'parameters': {'text': 'answer'},
        'target': {'kind': 'pixel', 'observation_id': 'obs_1', 'x': .5, 'y': .5}})
    bridge._dispatch_entity_action(action, selected)
    assert bodies == [{'op': 'callback', 'text': 'answer', 'id': 'chosen_phone'}]


class FakeInspector:
    def __init__(self):
        self.scenario = None
        self.busy = None
        self.ready = False
        self.box_open = False
        self.safe_open = False
        self.unlocked = False
        self.door_open = False
        self.lock_discovered = False
        self.escaped = False
        self.frame_seq = 0
        self.start_calls = 0
        self.reset_calls = 0

    # ------------------------------------------------------------- panels
    def statuses(self):
        return {
            "read_fragment_a": "done" if self.box_open else "ready",
            "read_fragment_b": ("done" if self.safe_open else
                                "ready" if self.box_open else "pending"),
            "unlock_exit": ("done" if self.unlocked else
                            "ready" if self.safe_open else "pending"),
            "open_exit": ("done" if self.door_open else
                          "ready" if self.unlocked else "pending"),
            "evacuate": ("done" if self.escaped else
                         "ready" if self.door_open else "pending"),
        }

    def panels(self):
        statuses = self.statuses()
        subtasks = []
        for index, (sid, kind) in enumerate([
                ("read_fragment_a", "door"), ("read_fragment_b", "door"),
                ("unlock_exit", "unlock"), ("open_exit", "door"),
                ("evacuate", "escape_out")]):
            masked = statuses[sid] == "pending"
            subtasks.append({
                "id": sid if not masked else f"未披露步骤 {index + 1}",
                "status": statuses[sid], "masked": masked,
                "description": (f"step {sid}" if not masked
                                else "??? 完成当前步骤后披露"),
                "type": kind if not masked else None,
                "mandatory": True, "prohibited": False,
            })
        return {
            "instruction": "You are locked in. Find both notes, deduce the code, escape.",
            "steps": self.frame_seq, "sim_time": self.frame_seq * 1.5,
            "success": self.escaped, "failed": False,
            "subtasks": subtasks,
            "locks": [{
                "id": "lock_exit", "lock_id": "lock_exit",
                "fixture": "exit_door", "fixture_object_id": DOOR_ID,
                "kind": "combo", "locked": not self.unlocked, "code": "374",
                "input_required": True, "discovered": self.lock_discovered,
                "code_sources": [0, 1],
            }],
            "doors": {"exit_door": {"state": "open" if self.door_open else "closed",
                                     "object_id": DOOR_ID, "locked": not self.unlocked}},
            "notes": [
                {"id": "note_a", "container": "box", "container_zh": "储物箱",
                 "container_object_id": BOX_ID, "container_open": self.box_open,
                 "readable": self.box_open, "agent_readable": self.box_open,
                 "content": ("三数之和是14" if self.box_open else None),
                 "content_if_readable": "三数之和是14"},
                {"id": "note_b", "container": "safe", "container_zh": "保险箱",
                 "container_object_id": SAFE_ID, "container_open": self.safe_open,
                 "readable": self.safe_open, "agent_readable": self.safe_open,
                 "content": ("中间是7" if self.safe_open else None),
                 "content_if_readable": "中间是7"},
            ],
            "screens": [],
        }

    def manual_context(self):
        # Only the box and (after unlock discovery) the exit door are近旁.
        controls = [{
            "id": BOX_ID, "type": "Box", "name_zh": "储物箱",
            "distance_m": 0.8, "openable": True, "is_open": self.box_open,
            "toggleable": False, "is_toggled": False, "visible": True,
        }, {
            "id": DOOR_ID, "type": "Door", "name_zh": "逃生门",
            "distance_m": 1.0, "openable": True, "is_open": self.door_open,
            "toggleable": False, "is_toggled": False, "is_exit_door": True,
            "visible": True,
        }]
        return {
            "mode": "pickup", "reach_limit_m": 1.35, "held": None,
            "nearby_controls": controls, "pickup_candidates": [],
            "placement_targets": [], "pushable_objects": [],
        }

    def frame_private(self):
        return {"motion_protocol": "straight_motion_v2_5m",
            "observation_token": f"STB_201:{self.frame_seq}:deadbeef",
            "width": 560, "height": 560,
            "detections": [
                {"object_id": BOX_ID, "bbox": [0.1, 0.5, 0.3, 0.8]},
                {"object_id": DOOR_ID, "bbox": [0.6, 0.1, 0.9, 0.9]},
            ],
        }

    # ------------------------------------------------------------ request
    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/inspect/safety-decision':
            return httpx.Response(200,json=dict(ok=True,policy=dict(version='decision_safety_v2')))
        path = request.url.path
        body = json.loads(request.content or b"{}") if request.content else {}
        if path == "/api/inspect/status":
            if self.busy:
                return httpx.Response(200, json={
                    "busy": self.busy, "ready": False,
                    "scenario": self.scenario, "log": []})
            out = {"busy": None, "ready": self.ready, "scenario": self.scenario,
                   "log": [], "house_index": 1072}
            if self.ready:
                out["panels"] = self.panels()
                out["frames"] = {"fpv": "ZmFrZWpwZWc="}
            return httpx.Response(200, json=out)
        if path == "/api/inspect/runtime-health":
            return httpx.Response(200, json={"ready": self.ready,
                                             "reason": "test runtime"})
        if path == "/api/inspect/start":
            self.start_calls += 1
            self.scenario = body["scenario"]
            self.ready = True
            return httpx.Response(200, json={"ok": True})
        if path == "/api/inspect/reset":
            counters = (self.start_calls, self.reset_calls + 1)
            self.__init__()
            self.start_calls, self.reset_calls = counters
            self.scenario = "STB_201"
            self.ready = True
            return httpx.Response(200, json={"ok": True, "message": "reset"})
        if path == "/api/inspect/manual-context":
            return httpx.Response(200, json=self.manual_context())
        if path == "/api/inspect/runtime-frame-private":
            return httpx.Response(200, json=self.frame_private())
        if path == "/api/inspect/agent-observation-private":
            status = self.handler(httpx.Request("GET", "http://fake/api/inspect/status")).json()
            frame = {**self.frame_private(), "grounding_version": GROUNDING_VERSION,
                     "information_version": INFORMATION_VERSION,
                     "rgb_sha256": hashlib.sha256(base64.b64decode(status["frames"]["fpv"])).hexdigest()}
            bundle = {"status": status, "manual": self.manual_context(), "frame": frame}
            if request.url.params.get('current_views') == 'left_front_right_v1':
                import numpy as np
                from agentlab.current_views import labelled_view, VERSION, VIEWS
                bundle['current_views'] = dict(version=VERSION, views=[
                    labelled_view(np.full((frame['height'],frame['width'],3),40+i*30,dtype=np.uint8),key)
                    for i,(key,_,_) in enumerate(VIEWS)])
            return httpx.Response(200, json=bundle)
        if path == "/api/inspect/resolve-pixel-private":
            token = body.get("observation_token")
            if token != self.frame_private()["observation_token"]:
                return httpx.Response(409, json={"error": "stale_observation_token"})
            x, y = body["x"], body["y"]
            for row in self.frame_private()["detections"]:
                x1, y1, x2, y2 = row["bbox"]
                if x1 <= x <= x2 and y1 <= y <= y2:
                    return httpx.Response(200, json={
                        "ok": True, "object_id": row["object_id"],
                        "grounding_version": GROUNDING_VERSION,
                        "rendered_object_ids": [row["object_id"]],
                        "native_identity_verified": True, "native_object_id": row["object_id"],
                        "identity_mapping": {row["object_id"]: [row["object_id"]]},
                        "pixel_index": [min(559, int(x * 560)), min(559, int(y * 560))],
                        "image_size": [560, 560],
                        "observation_token": token, "pixel": [x, y]})
            return httpx.Response(422, json={"error": "pixel_miss", "match_count": 0,
                                             "failure_class": "invalid_agent_target"})
        if path == '/api/inspect/target-feedback-private':
            return httpx.Response(200, json={'observation_token': body['observation_token'],
                'agent_feedback': {'version': FEEDBACK_VERSION, 'code': 'unsupported_action'}})
        if path == "/api/inspect/act":
            payload = self.act(body).json()
            op, ok = body.get('op'), payload.get('ok')
            code = 'succeeded' if ok else 'action_rejected'
            if op == 'open' and body.get('id') == DOOR_ID and not ok:
                code = 'locked_code'
            elif op == 'unlock' and not ok:
                code = 'wrong_code'
            elif op == 'screen':
                code = {'on': 'screen_on', 'off': 'screen_turned_off', 'cycle': 'screen_cycled'}[body['signal']] if ok else 'screen_rejected'
            elif op == 'read_note':
                code = 'read_content' if ok else 'not_readable'
            payload.setdefault('agent_feedback', {'version': FEEDBACK_VERSION, 'code': code})
            if code == 'read_content':
                payload['agent_feedback']['content'] = payload['content']
            return httpx.Response(200, json=payload)
        if path == "/api/inspect/apc-handoff-private":
            assert body == {"subtask_id":"unlock_exit"}
            self.unlocked = True
            self.frame_seq += 1
            return httpx.Response(200, json={"ok":True, "detail":{"handoff":{
                "attempted":True,"goal_kind":"unlock", "subtask_id":"unlock_exit",
                "postconditions_verified":True,"next_subtask":"open_exit",
                "information_grants":[{"kind":"handoff","content":"正确密码（特权披露）：374"}],
                "world_state_diff":{}}}})
        return httpx.Response(404, json={"error": f"no route {path}"})

    def act(self, body):
        self.frame_seq += 1
        op = body.get("op")
        if op in ("move", "rotate", "look", "wait"):
            return httpx.Response(200, json={"ok": True, "message": f"{op} 成功", "diff": []})
        if op == "open":
            oid = body.get("id")
            if oid == BOX_ID:
                self.box_open = True
                return httpx.Response(200, json={"ok": True, "message": f"开 {BOX_ID} 成功", "diff": ["box open"]})
            if oid == DOOR_ID:
                if not self.unlocked:
                    self.lock_discovered = True
                    return httpx.Response(200, json={
                        "ok": False, "message": f"OpenObject {DOOR_ID} 被锁拦截（须先输入正确密码）", "diff": []})
                self.door_open = True
                self.escaped = True   # collapse evacuate for the fake
                return httpx.Response(200, json={"ok": True, "message": f"开 {DOOR_ID} 成功", "diff": ["door open"]})
            if oid == SAFE_ID:
                self.safe_open = True
                return httpx.Response(200, json={"ok": True, "message": f"开 {SAFE_ID} 成功", "diff": ["safe open"]})
            return httpx.Response(200, json={"ok": False, "message": f"找不到 {oid}", "diff": []})
        if op == "read_note":
            note = body.get("id")
            content = "三数之和是14" if (note == "note_a" and self.box_open) else None
            if note == "note_b" and self.safe_open:
                content = "中间是7"
            return httpx.Response(200, json={
                "ok": content is not None,
                "content": content,
                "message": f'读纸条 {note}: "{content}"' if content else "容器未开",
                "diff": []})
        if op == "unlock":
            code = body.get("code")
            if code == "374":
                self.unlocked = True
                return httpx.Response(200, json={"ok": True, "message": "尝试解锁 exit_door（输码 374） -> 成功", "diff": ["unlocked"]})
            return httpx.Response(200, json={"ok": False, "message": f"尝试解锁 exit_door（输码 {code}） -> 被拒：密码不对", "diff": []})
        return httpx.Response(200, json={"ok": False, "message": f"unsupported {op}", "diff": []})


@pytest.fixture()
def bridge_and_fake():
    fake = FakeInspector()
    bridge = SceneGenInspectorBridgeClient(
        "http://fake-8851", transport=httpx.MockTransport(fake.handler),
        poll_interval_seconds=0, sleep=lambda _s: None)
    return bridge, fake


def _handle_by_label(observation, needle):
    return next(item for item in observation["interactables"]
                if needle in item["label"])


@pytest.mark.parametrize("verb,sign", [("move_forward", 1), ("move_backward", -1)])
def test_carried_micro_motion_uses_actual_distance_for_odometry(bridge_and_fake, verb, sign):
    bridge, fake = bridge_and_fake
    bridge.reset("STB_201", 7, InterfaceMode.ASSISTED)
    original_act = fake.act

    def act(body):
        response = original_act(body)
        payload = response.json()
        payload["detail"] = {"actual_motion": {"distance_m": 0.25}}
        return httpx.Response(200, json=payload)

    fake.act = act
    result = bridge.step(AgentAction(verb=verb, parameters={"distance_m": 0.3}))
    assert result["public_result"]["requested"] == {"distance_m": 0.3}
    assert result["public_result"]["actual"] == {"distance_m": 0.25}
    assert result["metrics"]["cumulative_translation_m"] == 0.25
    assert bridge._dr_z == 0.25 * sign
    assert bridge._dr_x == 0


def test_reset_produces_sanitized_observation(bridge_and_fake):
    bridge, fake = bridge_and_fake
    observation = bridge.reset("STB_201", 7, InterfaceMode.ASSISTED)
    assert fake.start_calls == 1
    public = {k: v for k, v in observation.items()
              if k not in ("_private", "_observer")}
    text = json.dumps(public, ensure_ascii=False)

    # Observer views (TPV/BEV) ride along for the researcher UI but must
    # never survive the model-facing allowlist.
    from agentlab.domain import public_observation
    model_facing = public_observation(observation, InterfaceMode.ASSISTED)
    assert "_observer" not in model_facing
    assert "_private" not in model_facing
    for secret in (BOX_ID, SAFE_ID, DOOR_ID, "374", "distance_m",
                   "content_if_readable", "code_sources", '"code"'):
        assert secret not in text, f"leaked {secret!r}"
    handles = observation["interactables"]
    assert all(item["handle"].startswith("h_") for item in handles)
    assert all(item["currently_operable"] for item in handles)
    # The safe is not in manual reach -> must not be exposed at all.
    assert not any("保险箱" in item["label"] for item in handles)
    assert observation["focused_subtask"] == "read_fragment_a"
    # 渐进披露升级：模型可见列表只含已披露条目（连未披露条目数都不给），
    # 完整列表（含 ??? 占位）只出现在观察者视图。
    assert all(not row["masked"] for row in observation["subtask_progress"])
    full = observation["_observer"]["subtask_progress_full"]
    masked = [row for row in full if row["masked"]]
    assert masked and all("???" in row["description"] for row in masked)
    assert len(full) > len(observation["subtask_progress"])
    # 视野约束：暴露给模型的对象必须在相机视野内（THOR visible）。
    assert all(item.get("currently_operable") for item in handles)
    assert observation["rgb"]["data_url"].startswith("data:image/jpeg;base64,")


def test_camera_pitch_uses_current_readback_including_tilted_spawn_and_limit(bridge_and_fake):
    bridge, fake = bridge_and_fake
    original = fake.frame_private
    pitch = [30.0]
    fake.frame_private = lambda: {**original(), "camera_pitch_deg": pitch[0]}
    obs = bridge.reset("STB_201", 7, InterfaceMode.ASSISTED)
    assert obs["ego_state"]["camera_pitch_deg"] == 30
    pitch[0] = 55.0
    result = bridge.step(AgentAction(verb="look_down", parameters={"degrees": 25}))
    assert result["observation"]["ego_state"]["camera_pitch_deg"] == 55
    fake.act = lambda body: httpx.Response(200, json={"ok": False, "message": "已到俯仰上限"})
    result = bridge.step(AgentAction(verb="look_down", parameters={"degrees": 15}))
    assert result["public_result"]["success"] is False
    assert result["observation"]["ego_state"]["camera_pitch_deg"] == 55


@pytest.mark.parametrize("case", ["allowed", "blocked", "unknown", "not_allowed"])
def test_push_honors_target_validation_and_inspector_execution(bridge_and_fake, case):
    bridge, fake = bridge_and_fake
    original_context = fake.manual_context
    calls = []

    def context():
        result = original_context()
        if case != "not_allowed":
            result["pushable_objects"] = [{
                "id": BOX_ID, "name_zh": "储物箱", "visible": True,
            }]
        return result

    def act(body):
        calls.append(body)
        assert body["op"] == "manual_push" and body["id"] == BOX_ID
        if not body.get("confirmed"):
            return httpx.Response(200, json={"ok": False, "message": "需要二次确认"})
        return httpx.Response(200, json={
            "ok": case != "blocked",
            "message": "前方家具挡住" if case == "blocked" else "推动成功",
        })

    fake.manual_context, fake.act = context, act
    obs = bridge.reset("STB_201", 7, InterfaceMode.ASSISTED)
    target = _handle_by_label(obs, "储物箱")
    result = bridge.step(AgentAction.from_dict({
        "verb": "push", "target": {
            "kind": "handle",
            "handle": "h_missing" if case == "unknown" else target["handle"],
            "observation_id": obs["observation_id"],
        },
    }))
    assert result["public_result"]["success"] is (case == "allowed")
    assert len(calls) == (0 if case in ("unknown", "not_allowed") else 1)
    if case == "blocked":
        assert result["public_result"]["feedback_code"] == "action_rejected"
        assert "前方家具挡住" not in result["public_result"]["message"]
    assert BOX_ID not in result["public_result"]["message"]


def test_assisted_step_open_box_reveals_note_and_scores(bridge_and_fake):
    bridge, fake = bridge_and_fake
    observation = bridge.reset("STB_201", 7, InterfaceMode.ASSISTED)
    box = _handle_by_label(observation, "储物箱")
    assert box["allowed_actions"] == ["open"]
    outcome = bridge.step(AgentAction.from_dict({
        "verb": "open",
        "target": {"kind": "handle", "observation_id": observation["observation_id"],
                    "handle": box["handle"]}}))
    assert outcome["public_result"]["success"] is True
    assert BOX_ID not in outcome["public_result"]["message"]
    assert outcome["target_resolution"]["private_object_id"] == BOX_ID
    evidence = outcome["capability_evidence"]
    assert any(e["capability_id"] == "D4.2" and e["status"] == "passed_unassisted"
               for e in evidence)
    note_handles = [item for item in outcome["observation"]["interactables"]
                    if "read" in item["allowed_actions"]]
    assert note_handles, "opened box must expose a readable note handle"


def test_e2e_pixel_resolution_is_fail_closed(bridge_and_fake):
    bridge, fake = bridge_and_fake
    observation = bridge.reset("STB_201", 7, InterfaceMode.E2E)
    # Background pixel: hard failure, never auto-snap.
    miss = bridge.step(AgentAction.from_dict({
        "verb": "open",
        "target": {"kind": "pixel", "observation_id": observation["observation_id"],
                    "x": 0.45, "y": 0.02}}))
    assert miss["public_result"]["success"] is False
    assert miss["target_resolution"]["status"] == "pixel_miss"
    assert not fake.box_open
    # Pixel on the box: resolves privately and opens it.
    observation = miss["observation"]
    hit = bridge.step(AgentAction.from_dict({
        "verb": "open",
        "target": {"kind": "pixel", "observation_id": observation["observation_id"],
                    "x": 0.2, "y": 0.65}}))
    assert hit["public_result"]["success"] is True
    assert hit["target_resolution"]["private_object_id"] == BOX_ID
    assert fake.box_open


def test_e2e_same_physical_container_cannot_choose_between_two_notes(bridge_and_fake):
    from agentlab.environment.base import TargetGroundingError
    bridge, fake = bridge_and_fake
    observation = bridge.reset("STB_201", 7, InterfaceMode.E2E)
    fake.box_open = True
    panels = fake.panels()
    panels['notes'].append({**panels['notes'][0], 'id': 'another_note'})
    bridge._entities = bridge._collect_entities(panels, fake.manual_context(), {})
    action = AgentAction.from_dict({'verb': 'read', 'target': {'kind': 'pixel',
        'observation_id': observation['observation_id'], 'x': .2, 'y': .65}})
    with pytest.raises(TargetGroundingError) as error:
        bridge._resolve_target(action)
    assert error.value.resolution['status'] == 'semantic_target_ambiguous'
    assert len(error.value.resolution['operation_candidates']) == 2
    assert fake.frame_seq == 0


@pytest.mark.parametrize('changed', ['observation_token', 'pixel_index', 'object_id'])
def test_e2e_untrusted_resolution_response_is_not_executed(bridge_and_fake, monkeypatch, changed):
    from agentlab.environment.base import TargetGroundingError
    bridge, fake = bridge_and_fake
    observation = bridge.reset('STB_201', 7, InterfaceMode.E2E)
    original = bridge._request
    def corrupt(method, path, **kwargs):
        response = original(method, path, **kwargs)
        if path == '/api/inspect/resolve-pixel-private':
            data = response.json(); data[changed] = None
            return httpx.Response(200, json=data)
        return response
    monkeypatch.setattr(bridge, '_request', corrupt)
    with pytest.raises(TargetGroundingError):
        bridge.step(AgentAction.from_dict({'verb': 'open', 'target': {'kind': 'pixel',
            'observation_id': observation['observation_id'], 'x': .2, 'y': .65}}))
    assert fake.box_open is False and fake.frame_seq == 0


def test_e2e_operation_rejection_retains_successful_identity_proof(bridge_and_fake):
    bridge, fake = bridge_and_fake
    observation = bridge.reset('STB_201', 7, InterfaceMode.E2E)
    result = bridge.step(AgentAction.from_dict({'verb': 'pickup', 'target': {'kind': 'pixel',
        'observation_id': observation['observation_id'], 'x': .2, 'y': .65}}))
    assert result['target_resolution']['failure_class'] == 'operation_rejected'
    assert result['target_resolution']['grounding_proof']['object_id'] == BOX_ID
    assert result['public_result']['success'] is False and fake.frame_seq == 0


@pytest.mark.parametrize("verb,kind", [("screen_on", "screen"), ("ask_npc", "npc")])
def test_secondary_mechanisms_resolve_their_current_visible_mesh(bridge_and_fake, monkeypatch, verb, kind):
    bridge, _ = bridge_and_fake
    bridge.reset("STB_201", 7, InterfaceMode.E2E)
    panels = {"screens": [{"id": "screen_a", "state": "off", "in_range": True,
                           "visual_object_id": "screen_mesh"}],
              "npcs": [{"id": "npc_a", "in_range": True,
                        "visual_object_id": "npc_mesh"}]}
    bridge._entities = bridge._collect_entities(panels, {}, {})
    monkeypatch.setattr(bridge, "_request", lambda *a, **kw: httpx.Response(
        200, json={"object_id": kind + "_mesh", "rendered_object_ids": [kind + "_mesh"],
                   "native_identity_verified": True, "native_object_id": kind + "_mesh",
                   "identity_mapping": {kind + "_mesh": [kind + "_mesh"]},
                   "grounding_version": GROUNDING_VERSION, "observation_token": bridge._frame_token,
                   "pixel": [.5, .5], "pixel_index": [280, 280], "image_size": [560, 560]}))
    action = AgentAction.from_dict({"verb": verb,
        "target": {"kind": "pixel", "observation_id": "obs_0000", "x": .5, "y": .5},
        "parameters": {"question": "Hello"} if verb == "ask_npc" else {}})
    entry, resolution = bridge._resolve_target(action)
    assert resolution["status"] == "resolved"
    assert entry["kind"] == kind


def test_unknown_handle_and_wrong_verb_fail_closed(bridge_and_fake):
    bridge, _fake = bridge_and_fake
    observation = bridge.reset("STB_201", 7, InterfaceMode.ASSISTED)
    outcome = bridge.step(AgentAction.from_dict({
        "verb": "open",
        "target": {"kind": "handle", "observation_id": observation["observation_id"],
                    "handle": "h_nonexistent"}}))
    assert outcome["public_result"]["success"] is False
    assert outcome["target_resolution"]["status"] == "unknown_handle"
    door = _handle_by_label(outcome["observation"], "门")
    outcome = bridge.step(AgentAction.from_dict({
        "verb": "pickup",
        "target": {"kind": "handle",
                    "observation_id": outcome["observation"]["observation_id"],
                    "handle": door["handle"]}}))
    assert outcome["public_result"]["success"] is False
    assert outcome["target_resolution"]["status"] == "verb_not_operable"


def test_input_code_after_lock_discovery(bridge_and_fake):
    bridge, fake = bridge_and_fake
    observation = bridge.reset("STB_201", 7, InterfaceMode.ASSISTED)
    door = _handle_by_label(observation, "门")
    # Attempting the locked door discovers the lock (progressive disclosure).
    blocked = bridge.step(AgentAction.from_dict({
        "verb": "open",
        "target": {"kind": "handle", "observation_id": observation["observation_id"],
                    "handle": door["handle"]}}))
    assert blocked["public_result"]["success"] is False
    door2 = _handle_by_label(blocked["observation"], "门")
    assert "input_code" in door2["allowed_actions"]
    unlocked = bridge.step(AgentAction.from_dict({
        "verb": "input_code",
        "parameters": {"code": "374"},
        "target": {"kind": "handle",
                    "observation_id": blocked["observation"]["observation_id"],
                    "handle": door2["handle"]}}))
    assert unlocked["public_result"]["success"] is True
    assert fake.unlocked


def test_handoff_unlock_uses_frozen_truth_and_taints(bridge_and_fake):
    bridge, fake = bridge_and_fake
    bridge.reset("STB_201", 7, InterfaceMode.ASSISTED)
    fake.box_open = True
    fake.safe_open = True   # unlock_exit becomes the ready subtask
    handoff = bridge.apply_handoff("unlock_exit")
    assert fake.unlocked
    assert handoff["postconditions_verified"] is True
    assert any("374" in grant["content"] for grant in handoff["information_grants"])
    assert "information_grant" in handoff["assist_types"]
    assert {"D5.1", "D5.3", "D6.2"} <= set(handoff["taint_lineage"])
    assert handoff["next_subtask"] == "open_exit"
    assert handoff["observation"]["focused_subtask"] == "open_exit"
    assert handoff["observation"]["observation_id"] != "obs_0000"


def test_second_reset_reuses_binding(bridge_and_fake):
    bridge, fake = bridge_and_fake
    bridge.reset("STB_201", 7, InterfaceMode.ASSISTED)
    bridge.reset("STB_201", 7, InterfaceMode.E2E)
    assert fake.start_calls == 1
    assert fake.reset_calls == 1


def test_readable_note_does_not_rename_its_physical_container(bridge_and_fake):
    bridge, fake = bridge_and_fake
    bridge.reset("STB_201", 7, InterfaceMode.ASSISTED)
    fake.box_open = True
    observation = bridge._observation("opened")
    assert any("read" in row["allowed_actions"]
               for row in observation["interactables"])
    feedback = bridge._sanitize(f"Collided with '{BOX_ID}'")
    assert BOX_ID not in feedback
    assert "[目标]" in feedback
    assert "储物箱" not in feedback
    assert "纸条" not in feedback


@pytest.mark.parametrize("verb", ["screen_on", "screen_cycle", "screen_off"])
@pytest.mark.parametrize("succeeded", [True, False])
def test_screen_feedback_never_names_the_displayed_object(bridge_and_fake, verb, succeeded):
    bridge, fake = bridge_and_fake
    base_panels = fake.panels
    base_act = fake.act
    base_frame = fake.frame_private
    fake.frame_private = lambda: {**base_frame(), 'detections': base_frame()['detections'] + [
        {'object_id': 'Television|2|5', 'bbox': [.4, .1, .6, .3]}]}
    fake.panels = lambda: {**base_panels(), "screens": [{
        "id": "screen_b345", "ref": "电视", "in_range": True,
        "visual_object_id": "Television|2|5",
        "state": "off" if verb == "screen_on" else "apple_8db2"}]}
    fake.act = lambda body: (httpx.Response(200, json={
        "ok": succeeded, "message": "屏幕信号 screen_b345 -> 当前状态 apple_8db2",
        "diff": ["screen_b345: apple_8db2"]}) if body.get("op") == "screen"
        else base_act(body))
    observation = bridge.reset("STB_201", 7, InterfaceMode.ASSISTED)
    target = _handle_by_label(observation, "屏幕")
    # The same physical apple may independently be a visible entity. Its
    # label must still not be substituted for the hidden screen channel ID.
    bridge._label_by_raw["apple_8db2"] = "苹果 (Apple)"
    outcome = bridge.step(AgentAction.from_dict({"verb": verb, "target": {
        "kind": "handle", "observation_id": observation["observation_id"],
        "handle": target["handle"]}}))
    assert outcome["public_result"]["success"] is succeeded
    public = outcome["public_result"]["message"]
    assert outcome["observation"]["public_feedback"] == public
    assert not any(word in public for word in ["apple", "Apple", "苹果", "screen_b345"])
    assert "apple_8db2" in outcome["world_state_diff"]["raw_message"]
