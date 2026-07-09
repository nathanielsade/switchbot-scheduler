import json


def run_turn(user_text, history, *, client, model, system, tools, max_steps=10):
    """Run one agentic turn: OpenAI function-calling loop until the model stops calling tools.
    Returns the final assistant text. Tool plumbing stays internal to this turn."""
    tool_by_name = {t.name: t for t in tools}
    schemas = [t.schema for t in tools]
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_text}]
    msg = None
    for step in range(max_steps):
        kwargs = {"model": model, "messages": messages}
        # On the final allowed step, stop offering tools so the model must answer in text
        # instead of requesting another (uncompleted) tool call that we'd discard.
        if schemas and step < max_steps - 1:
            kwargs["tools"] = schemas
        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        assistant = {"role": "assistant", "content": msg.content or ""}
        if tool_calls:
            assistant["tool_calls"] = [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            } for tc in tool_calls]
        messages.append(assistant)
        if not tool_calls:
            return msg.content or ""
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                tool = tool_by_name.get(tc.function.name)
                result = tool.impl(args) if tool else f"error: unknown tool {tc.function.name}"
            except Exception as e:
                result = f"error: {e}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
    return (msg.content if msg else None) or "error: exceeded max tool-call steps"
