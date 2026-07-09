import json


def run_turn(user_text, history, *, client, model, system, tools):
    """Run one agentic turn: OpenAI function-calling loop until the model stops calling tools.
    Returns the final assistant text. Tool plumbing stays internal to this turn."""
    tool_by_name = {t.name: t for t in tools}
    schemas = [t.schema for t in tools]
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_text}]
    while True:
        kwargs = {"model": model, "messages": messages}
        if schemas:
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
