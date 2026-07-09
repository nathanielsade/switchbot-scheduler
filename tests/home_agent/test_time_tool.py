from datetime import datetime

from home_agent.agent import run_turn
from home_agent.tools import DEFAULT_TOOLS, get_current_time


def test_get_current_time_returns_nonempty_string():
    out = get_current_time.impl({})
    assert isinstance(out, str) and out.strip()


def test_get_current_time_includes_weekday_so_model_need_not_guess():
    # Regression: the model invented the wrong day of week when the tool omitted it.
    out = get_current_time.impl({})
    today_weekday = datetime.now().astimezone().strftime("%A")
    assert today_weekday in out


def test_loop_can_call_get_current_time(make_fake_client):
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "get_current_time", "arguments": {}}]},
        {"content": "it is that time"},
    ])
    reply = run_turn("what time is it?", [], client=client, model="gpt-4o", system="S", tools=DEFAULT_TOOLS)
    assert reply == "it is that time"
    tool_msg = next(m for m in client._calls[1]["messages"] if m["role"] == "tool")
    assert tool_msg["content"].strip()  # the real time string was fed back
