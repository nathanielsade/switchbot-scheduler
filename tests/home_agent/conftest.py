import json
import pytest
from types import SimpleNamespace


@pytest.fixture
def make_fake_client():
    """Fixture that returns the make_fake_client factory function."""
    def _make_fake_client(script):
        calls = []
        state = {"i": 0}

        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                r = script[state["i"]]
                state["i"] += 1
                if "tool_calls" in r:
                    tcs = [SimpleNamespace(
                        id=tc["id"], type="function",
                        function=SimpleNamespace(name=tc["name"], arguments=json.dumps(tc["arguments"])),
                    ) for tc in r["tool_calls"]]
                    msg = SimpleNamespace(role="assistant", content=None, tool_calls=tcs)
                else:
                    msg = SimpleNamespace(role="assistant", content=r["content"], tool_calls=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        return SimpleNamespace(chat=SimpleNamespace(completions=Completions()), _calls=calls)

    return _make_fake_client
