from __future__ import annotations

import threading
import uuid

from flask import Flask, jsonify, request

from agentlab.domain import AgentAction, InterfaceMode
from agentlab.environment.mock import MockEnvironmentClient


def create_runtime_app() -> Flask:
    app = Flask(__name__)
    episodes: dict[str, MockEnvironmentClient] = {}
    lock = threading.Lock()

    @app.errorhandler(Exception)
    def error(error):
        return jsonify({"ok": False, "error": type(error).__name__, "message": str(error)}), 400

    @app.get("/v1/interface")
    def interface():
        return jsonify(MockEnvironmentClient().capabilities())

    @app.get("/v1/scenarios")
    def scenarios():
        return jsonify({"scenarios": MockEnvironmentClient().scenarios()})

    @app.post("/v1/episodes")
    def create_episode():
        body = request.get_json(force=True) or {}
        if body.get("protocol_version") != "1.0":
            raise ValueError("unsupported protocol_version")
        environment = MockEnvironmentClient()
        mode = InterfaceMode(body["interface_mode"])
        observation = environment.reset(str(body["scenario_id"]), int(body["seed"]), mode)
        episode_id = f"ep_{uuid.uuid4().hex[:12]}"
        with lock:
            episodes[episode_id] = environment
        scenario = environment.scenarios()[0]
        return jsonify({
            "episode_id": episode_id,
            "scenario_digest": scenario["digest"],
            "protocol_version": "1.0",
            "observation": observation,
        }), 201

    def get_episode(episode_id: str) -> MockEnvironmentClient:
        with lock:
            environment = episodes.get(episode_id)
        if not environment:
            raise KeyError("unknown episode or expired lease")
        return environment

    @app.post("/v1/episodes/<episode_id>/step")
    def step(episode_id: str):
        body = request.get_json(force=True) or {}
        result = get_episode(episode_id).step(AgentAction.from_dict(body["action"]))
        return jsonify(result)

    @app.post("/v1/episodes/<episode_id>/handoff")
    def handoff(episode_id: str):
        body = request.get_json(force=True) or {}
        return jsonify(get_episode(episode_id).apply_handoff(str(body["subtask_id"])))

    return app


def main() -> None:
    app = create_runtime_app()
    print("Runtime contract simulator: http://127.0.0.1:8860")
    app.run(host="127.0.0.1", port=8860, debug=False, threaded=True)


if __name__ == "__main__":
    main()

