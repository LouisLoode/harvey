"""A paused sender must not keep the heartbeat on send_campaign forever."""

import os
import tempfile

import pytest
import pytest_asyncio

from harvey.agents.sender import KILL_SWITCH_KEY
from harvey.config import load_config
from harvey.main import decide_next_action
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


def _native():
    config = load_config()
    email = config.channels.email.model_copy(update={"provider": "gmail"})
    return config.model_copy(update={"channels": config.channels.model_copy(update={"email": email})})


@pytest.mark.asyncio
async def test_drafts_are_sent_when_sending_is_on(state):
    summary = {"draft_campaigns": 1, "open_conversations": 0}
    assert await decide_next_action(None, state, _native(), summary=summary) == "send_campaign"


@pytest.mark.asyncio
async def test_paused_sending_moves_on_to_other_work(state):
    await state.set_setting(KILL_SWITCH_KEY, "paused manually")
    summary = {"draft_campaigns": 1, "open_conversations": 0}
    assert await decide_next_action(None, state, _native(), summary=summary) == "prospect"


@pytest.mark.asyncio
async def test_replies_still_come_first_while_paused(state):
    await state.set_setting(KILL_SWITCH_KEY, "paused manually")
    summary = {"draft_campaigns": 1, "open_conversations": 2}
    assert await decide_next_action(None, state, _native(), summary=summary) == "handle_replies"
