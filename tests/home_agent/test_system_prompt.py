from home_agent.prompts import FAMILY_SYSTEM_PROMPT
from home_agent.agent import run_turn


def test_prompt_is_nonempty_and_stable():
    assert FAMILY_SYSTEM_PROMPT.strip()
    assert "Hebrew" in FAMILY_SYSTEM_PROMPT  # stable anchor content
    assert not any(ch.isdigit() for ch in FAMILY_SYSTEM_PROMPT)  # no timestamps/volatile numbers
    assert "canonical" in FAMILY_SYSTEM_PROMPT.lower()   # shopping canonicalization policy present


def test_run_turn_sends_identical_system_prompt_each_turn(make_fake_client):
    client = make_fake_client([{"content": "a"}, {"content": "b"}])
    run_turn("one", [], client=client, model="gpt-4o", system=FAMILY_SYSTEM_PROMPT, tools=[])
    run_turn("two", [], client=client, model="gpt-4o", system=FAMILY_SYSTEM_PROMPT, tools=[])
    sys1 = client._calls[0]["messages"][0]
    sys2 = client._calls[1]["messages"][0]
    assert sys1 == {"role": "system", "content": FAMILY_SYSTEM_PROMPT}
    assert sys1 == sys2  # byte-identical → OpenAI auto-cache can hit
