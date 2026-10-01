"""The dev bridge exposes authenticated reads, never task/deploy mutations."""
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location("brain_live_demo", Path(__file__).resolve().parents[1] / "brain-ui/dev/live_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class Reader:
    def __init__(self):
        self.calls = []

    def call(self, op, **args):
        self.calls.append((op, args))
        if op == "poll":
            return {"events": [], "channels": {"1": "test"}, "beads": []}
        return {"task_id": args["task_id"], "log_id": "agent_task_attempt.log", "attempt_id": "attempt", "offset": 100, "log": "", "complete": True}

    def close(self):
        pass


def client_for(reader):
    initial = {"events": [{"ts": 1000, "channel_id": "1", "event": {"type": "system", "subtype": "init"}}],
               "channels": {"1": "test"}, "beads": [{"id": "task", "status": "open", "phase": "cancelled", "model": "test-model"}]}
    return TestClient(demo.create_app(reader, initial))


def headers(client):
    response = client.post("/api/brain/auth", json={"code": "live"})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["token"]}


def test_bridge_preserves_metadata_and_rejects_mutations():
    with client_for(Reader()) as client:
        assert client.get("/api/brain/beads").status_code == 401
        auth = headers(client)
        task = client.get("/api/brain/beads", headers=auth).json()["beads"][0]
        assert task["phase"] == "cancelled"
        assert task["model"] == "test-model"
        assert client.post("/api/brain/beads", headers=auth, json={}).status_code == 405
        assert client.post("/api/sites/deploy", headers=auth).status_code == 404
        assert client.post("/api/brain/auth", json={"code": "live"}, headers={"Origin": "https://unrelated.example"}).status_code == 403


def test_bridge_forwards_bounded_log_cursors_and_validates_task_ids():
    reader = Reader()
    with client_for(reader) as client:
        auth = headers(client)
        response = client.get("/api/brain/beads/task/log?offset=10&log_id=previous", headers=auth)
        assert response.json()["attempt_id"] == "attempt"
        assert reader.calls[-1] == ("task_log", {"task_id": "task", "offset": 10, "log_id": "previous"})
        assert client.get("/api/brain/beads/task*/log", headers=auth).status_code == 400


def test_bridge_replays_real_envelopes_after_local_authentication():
    with client_for(Reader()) as client:
        auth = headers(client)
        token = auth["Authorization"].removeprefix("Bearer ")
        with client.websocket_connect("/ws/brain?token=" + token, headers={"Origin": "http://127.0.0.1:5174"}) as socket:
            assert socket.receive_json()["type"] == "channels_map"
            assert socket.receive_json()["beads"][0]["phase"] == "cancelled"
            assert socket.receive_json()["event"]["subtype"] == "init"
