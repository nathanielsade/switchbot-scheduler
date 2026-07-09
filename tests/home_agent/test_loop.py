from types import SimpleNamespace
from home_agent.agent import run_turn


def _tool(name, impl):
    schema = {"type": "function", "function": {"name": name, "description": name,
              "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    return SimpleNamespace(name=name, schema=schema, impl=impl)


def test_loop_executes_tool_then_returns_final_text(make_fake_client):
    ran = []
    tool = _tool("do_it", lambda args: ran.append(args) or "tool-output")
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "do_it", "arguments": {}}]},
        {"content": "final answer"},
    ])
    reply = run_turn("hi", [], client=client, model="gpt-4o", system="S", tools=[tool])
    assert reply == "final answer"
    assert ran == [{}]                     # tool impl ran once
    # second request carried the tool result back to the model
    second_msgs = client._calls[1]["messages"]
    assert any(m["role"] == "tool" and m["content"] == "tool-output" for m in second_msgs)


def test_loop_returns_tool_error_as_result(make_fake_client):
    def boom(args):
        raise RuntimeError("kaboom")
    tool = _tool("do_it", boom)
    client = make_fake_client([
        {"tool_calls": [{"id": "c1", "name": "do_it", "arguments": {}}]},
        {"content": "handled"},
    ])
    reply = run_turn("hi", [], client=client, model="gpt-4o", system="S", tools=[tool])
    assert reply == "handled"
    tool_msg = next(m for m in client._calls[1]["messages"] if m["role"] == "tool")
    assert "kaboom" in tool_msg["content"]


def test_loop_stops_at_max_steps(make_fake_client):
    from home_agent.tools import DEFAULT_TOOLS
    # every response asks for another tool call → would loop forever unbounded
    script = [{"tool_calls": [{"id": f"c{i}", "name": "get_current_time", "arguments": {}}]}
              for i in range(3)]
    client = make_fake_client(script)
    reply = run_turn("loop", [], client=client, model="gpt-4o", system="S",
                     tools=DEFAULT_TOOLS, max_steps=3)
    assert "exceeded" in reply.lower()
    assert len(client._calls) == 3  # stopped exactly at the cap, no runaway


def test_final_step_withholds_tools_to_force_text_answer(make_fake_client):
    from home_agent.tools import DEFAULT_TOOLS
    # model keeps requesting tools; the final allowed step must stop offering them so a real
    # model is forced to answer in text instead of us discarding an uncompleted tool call.
    script = [{"tool_calls": [{"id": f"c{i}", "name": "get_current_time", "arguments": {}}]}
              for i in range(3)]
    client = make_fake_client(script)
    run_turn("loop", [], client=client, model="gpt-4o", system="S",
             tools=DEFAULT_TOOLS, max_steps=3)
    assert "tools" in client._calls[0]        # tools offered on early steps
    assert "tools" not in client._calls[-1]   # withheld on the final step
