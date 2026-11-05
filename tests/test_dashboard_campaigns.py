"""The Campaigns tab's list: where each campaign's first emails stand in the
Outbox, and why a draft isn't moving. Every business here is invented."""

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.state import StateManager

ORIGIN = "http://127.0.0.1:5555"
STEPS = [{"step": 1, "subject": "hi", "body": "Hi {{company}}", "delay_days": 0},
         {"step": 2, "subject": "re: hi", "body": "One more thing", "delay_days": 4}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "harvey.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_harvey_can_send", lambda: True)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    state = StateManager(db_path=str(db))

    async def _seed():
        await state.init_db()
        for n, status in enumerate(("pending_review", "pending_review", "approved", "sent")):
            await state.add_outbox_item(
                prospect_id=f"p{n}", campaign_id="c1", step=1, to_email=f"p{n}@lumenhexa.example",
                subject="hi", body="Hi", send_at="2026-11-01T09:00:00", status=status)
        # A follow-up isn't a first email.
        await state.add_outbox_item(
            prospect_id="p0", campaign_id="c1", step=2, to_email="p0@lumenhexa.example",
            subject="re: hi", body="More", send_at="2026-11-05T09:00:00")
    asyncio.run(_seed())
    with sqlite3.connect(db) as c:
        for cid, status, people in (("c1", "active", ["p0", "p1", "p2", "p3"]),
                                    ("c2", "draft", ["p4"]), ("c3", "draft", [])):
            c.execute("INSERT INTO campaigns (id, name, channel, sequence_json, prospect_ids_json, "
                      "status) VALUES (?, ?, 'email', ?, ?, ?)",
                      (cid, f"SaaS-outreach-fr-{cid}", json.dumps(STEPS), json.dumps(people), status))
    return {"db": db, "token": dash.token_store.get()}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


def _by_id(client):
    return {c["id"]: c for c in client.get("/api/campaigns").json()}


def test_first_emails_are_counted_by_status(client):
    rows = _by_id(client)
    assert rows["c1"]["first_emails"] == {"pending_review": 2, "approved": 1, "sent": 1}
    assert rows["c2"]["first_emails"] == {} and rows["c3"]["first_emails"] == {}


def test_a_draft_says_sending_is_paused(client, env):
    assert all(not c["held"] for c in _by_id(client).values())
    with sqlite3.connect(env["db"]) as c:
        c.execute("INSERT INTO settings (key, value) VALUES ('sending_paused', 'paused manually')")
    rows = _by_id(client)
    assert rows["c2"]["held"] == "paused manually" and rows["c3"]["held"] == "paused manually"
    assert rows["c1"]["held"] == ""          # already scheduled: nothing to hold


def test_resuming_clears_the_reason(client, env):
    with sqlite3.connect(env["db"]) as c:
        c.execute("INSERT INTO settings (key, value) VALUES ('sending_paused', '')")
    assert _by_id(client)["c2"]["held"] == ""
