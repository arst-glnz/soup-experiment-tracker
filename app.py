import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Cookie, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DATABASE_PATH", str(Path(__file__).with_name("data.db")))
app = FastAPI(title="Soup Experiment Tracker")


def db():
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_db():
    connection = db()
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY, name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_keys (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, hash TEXT NOT NULL,
          label TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS metrics (
          run_id TEXT NOT NULL, metric TEXT NOT NULL, step INTEGER NOT NULL,
          value REAL NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(run_id, metric, step), FOREIGN KEY(run_id) REFERENCES runs(id)
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          kind TEXT NOT NULL, payload TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES runs(id)
        );
        CREATE TABLE IF NOT EXISTS batches (
          run_id TEXT NOT NULL, batch_id TEXT NOT NULL,
          PRIMARY KEY(run_id, batch_id), FOREIGN KEY(run_id) REFERENCES runs(id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, project_id TEXT NOT NULL, expires_at INTEGER NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()


init_db()


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class RunIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class Metric(BaseModel):
    metric: str = Field(min_length=1, max_length=100)
    step: int = Field(ge=0)
    value: float


class Batch(BaseModel):
    batch_id: str = Field(min_length=1, max_length=200)
    metrics: list[Metric] = Field(min_length=1, max_length=1000)


def key_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer API key required")
    return authorization[7:]


def project_for_key(value: str) -> str:
    connection = db()
    row = connection.execute(
        "SELECT project_id FROM api_keys WHERE hash=? AND revoked=0", (key_hash(value),)
    ).fetchone()
    connection.close()
    if not row:
        raise HTTPException(401, "Invalid or revoked API key")
    return row["project_id"]


def project_run(connection, run_id: str, project_id: str):
    row = connection.execute(
        "SELECT id, name, project_id FROM runs WHERE id=? AND project_id=?",
        (run_id, project_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Run not found")
    return row


@app.post("/api/projects")
def create_project(payload: ProjectIn):
    project_id, raw_key = str(uuid.uuid4()), "soup_" + secrets.token_urlsafe(24)
    connection = db()
    connection.execute("INSERT INTO projects VALUES (?, ?)", (project_id, payload.name))
    connection.execute(
        "INSERT INTO api_keys VALUES (?, ?, ?, ?, 0)",
        (str(uuid.uuid4()), project_id, key_hash(raw_key), "initial"),
    )
    connection.commit()
    connection.close()
    return {"project_id": project_id, "api_key": raw_key}


@app.post("/api/projects/{project_id}/keys")
def rotate_key(project_id: str, authorization: Optional[str] = Header(None)):
    if project_for_key(bearer(authorization)) != project_id:
        raise HTTPException(404, "Project not found")
    raw_key = "soup_" + secrets.token_urlsafe(24)
    connection = db()
    connection.execute("UPDATE api_keys SET revoked=1 WHERE project_id=?", (project_id,))
    connection.execute(
        "INSERT INTO api_keys VALUES (?, ?, ?, ?, 0)",
        (str(uuid.uuid4()), project_id, key_hash(raw_key), "rotated"),
    )
    connection.commit()
    connection.close()
    return {"api_key": raw_key}


@app.post("/api/runs")
def create_run(payload: RunIn, authorization: Optional[str] = Header(None)):
    project_id = project_for_key(bearer(authorization))
    run_id = str(uuid.uuid4())
    connection = db()
    connection.execute("INSERT INTO runs VALUES (?, ?, ?)", (run_id, project_id, payload.name))
    connection.commit()
    connection.close()
    return {"id": run_id, "name": payload.name}


@app.post("/runs/{run_id}/metrics")
def ingest(run_id: str, batch: Batch, authorization: Optional[str] = Header(None)):
    project_id = project_for_key(bearer(authorization))
    connection = db()
    project_run(connection, run_id, project_id)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for metric in batch.metrics:
            connection.execute(
                "INSERT OR IGNORE INTO metrics(run_id, metric, step, value) VALUES (?, ?, ?, ?)",
                (run_id, metric.metric, metric.step, metric.value),
            )
        inserted = connection.execute(
            "INSERT OR IGNORE INTO batches(run_id, batch_id) VALUES (?, ?)",
            (run_id, batch.batch_id),
        ).rowcount
        if inserted:
            # A batch is an event only after all points are committed in the same transaction.
            connection.execute(
                "INSERT INTO events(run_id, kind, payload) VALUES (?, 'metrics', ?)",
                (run_id, json.dumps({"batch_id": batch.batch_id, "metrics": [m.model_dump() for m in batch.metrics]})),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"accepted": len(batch.metrics), "batch_id": batch.batch_id}


def read_run(run_id: str, project_id: str):
    connection = db()
    run = project_run(connection, run_id, project_id)
    rows = connection.execute(
        "SELECT metric, step, value FROM metrics WHERE run_id=? ORDER BY metric, step",
        (run_id,),
    ).fetchall()
    connection.close()
    return {"id": run["id"], "name": run["name"], "metrics": [dict(row) for row in rows]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, authorization: Optional[str] = Header(None)):
    return read_run(run_id, project_for_key(bearer(authorization)))


@app.post("/api/session")
def create_session(authorization: Optional[str] = Header(None)):
    project_id = project_for_key(bearer(authorization))
    token = secrets.token_urlsafe(32)
    connection = db()
    connection.execute("INSERT INTO sessions VALUES (?, ?, strftime('%s','now') + 3600)", (token, project_id))
    connection.commit()
    connection.close()
    response = JSONResponse({"ok": True})
    response.set_cookie("soup_session", token, httponly=True, samesite="strict")
    return response


def session_project(token: Optional[str]) -> str:
    connection = db()
    row = connection.execute(
        "SELECT project_id FROM sessions WHERE token=? AND expires_at > strftime('%s','now')", (token,)
    ).fetchone()
    connection.close()
    if not row:
        raise HTTPException(401, "Session required")
    return row["project_id"]


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str, request: Request, last_event_id: Optional[str] = Header(None), soup_session: Optional[str] = Cookie(None)):
    project_id = session_project(soup_session)
    connection = db()
    project_run(connection, run_id, project_id)
    after = int(last_event_id or 0)
    rows = connection.execute(
        "SELECT id, kind, payload FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after)
    ).fetchall()
    connection.close()

    async def stream():
        for row in rows:
            yield f"id: {row['id']}\nevent: {row['kind']}\ndata: {row['payload']}\n\n"
        # A reconnect always replays from Last-Event-ID; the client then refetches canonical state.
        yield ": heartbeat\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def page(run_id: str):
    return HTMLResponse(
        """<!doctype html><meta charset="utf-8"><title>Run</title>
        <main><h1 id="title">Loading…</h1><pre id="metrics"></pre></main>
        <script>
        const key = new URLSearchParams(location.search).get('key');
        async function start() {
          const h = {'Authorization': 'Bearer ' + key};
          const session = await fetch('/api/session', {method:'POST', headers:h});
          if (!session.ok) return document.querySelector('#title').textContent='Unauthorized';
          const data = await fetch('/api/runs/""" + run_id + """', {headers:h}).then(r=>r.json());
          render(data); const es = new EventSource('/api/runs/""" + run_id + """/events');
          es.onmessage = () => fetch('/api/runs/""" + run_id + """', {headers:h}).then(r=>r.json()).then(render);
          es.addEventListener('metrics', () => fetch('/api/runs/""" + run_id + """', {headers:h}).then(r=>r.json()).then(render));
        }
        function render(data) { document.querySelector('#title').textContent=data.name;
          document.querySelector('#metrics').textContent=JSON.stringify(data.metrics, null, 2); }
        start();
        </script>"""
    )


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(
        """<!doctype html><meta charset="utf-8"><title>Soup tracker</title>
        <main><h1>Soup experiment tracker</h1>
        <p>API is running. Create a project with <code>POST /api/projects</code>,
        then open <code>/runs/{run_id}?key={api_key}</code>.</p>
        <p>See <a href="/docs">API documentation</a> for available endpoints.</p>
        </main>"""
    )


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)
