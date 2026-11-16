import json
import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    database = os.path.join(tmp_path, "test.db")
    monkeypatch.setenv("DATABASE_PATH", database)
    import app
    app.DB_PATH = database
    app.init_db()
    test_client = TestClient(app.app)
    yield test_client
    test_client.close()


def make_project(client, name):
    result = client.post("/api/projects", json={"name": name}).json()
    return result["project_id"], result["api_key"]


def make_run(client, key, name="run"):
    return client.post("/api/runs", headers={"Authorization": f"Bearer {key}"}, json={"name": name}).json()["id"]


def send(client, key, run, batch_id, metrics):
    return client.post(
        f"/runs/{run}/metrics",
        headers={"Authorization": f"Bearer {key}"},
        json={"batch_id": batch_id, "metrics": metrics},
    )


def test_out_of_order_duplicate_late_and_reconnect_converge(client):
    project, key = make_project(client, "A")
    run = make_run(client, key)
    assert send(client, key, run, "b-2", [{"metric": "loss", "step": 2, "value": 0.2}]).is_success
    assert send(client, key, run, "b-1", [{"metric": "loss", "step": 1, "value": 0.3}]).is_success
    assert send(client, key, run, "b-2", [{"metric": "loss", "step": 2, "value": 999}]).is_success
    assert send(client, key, run, "late", [{"metric": "loss", "step": 0, "value": 0.4}]).is_success
    canonical = client.get(f"/api/runs/{run}", headers={"Authorization": f"Bearer {key}"}).json()
    assert canonical["metrics"] == [
        {"metric": "loss", "step": 0, "value": 0.4},
        {"metric": "loss", "step": 1, "value": 0.3},
        {"metric": "loss", "step": 2, "value": 0.2},
    ]
    client.post("/api/session", headers={"Authorization": f"Bearer {key}"})
    replay = client.get(f"/api/runs/{run}/events", headers={"Last-Event-ID": "1"})
    assert replay.status_code == 200 and "b-1" in replay.text and "late" in replay.text
    assert json.dumps(canonical["metrics"])  # UI refetches this canonical state after replay.


def test_project_isolation_in_api_sse_and_key_rotation(client):
    project_a, key_a = make_project(client, "A")
    project_b, key_b = make_project(client, "B")
    run_a, run_b = make_run(client, key_a), make_run(client, key_b)
    assert client.get(f"/api/runs/{run_b}", headers={"Authorization": f"Bearer {key_a}"}).status_code == 404
    assert send(client, key_a, run_b, "x", [{"metric": "x", "step": 1, "value": 1}]).status_code == 404
    client.post("/api/session", headers={"Authorization": f"Bearer {key_a}"})
    assert client.get(f"/api/runs/{run_b}/events").status_code == 404
    rotated = client.post(f"/api/projects/{project_a}/keys", headers={"Authorization": f"Bearer {key_a}"}).json()["api_key"]
    assert client.get(f"/api/runs/{run_a}", headers={"Authorization": f"Bearer {key_a}"}).status_code == 401
    assert client.get(f"/api/runs/{run_a}", headers={"Authorization": f"Bearer {rotated}"}).status_code == 200
    assert client.get(f"/api/runs/{run_a}", headers={"Authorization": f"Bearer {key_b}"}).status_code == 404
