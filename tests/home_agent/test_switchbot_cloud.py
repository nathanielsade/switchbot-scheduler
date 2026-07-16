import pytest
from home_agent import switchbot_cloud as sc

def _fake_http(calls, responses):
    def http_fn(method, url, headers, body):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return responses[len(calls) - 1]
    return http_fn

def test_send_command_success_posts_signed_request():
    calls = []
    http = _fake_http(calls, [(200, {"statusCode": 100, "message": "success", "body": {}})])
    sc.send_command("EECE111B5B1C", "turnOn", token="TOK", secret="SEC", http_fn=http)
    c = calls[0]
    assert c["method"] == "POST"
    assert c["url"].endswith("/v1.1/devices/EECE111B5B1C/commands")
    assert c["body"] == {"command": "turnOn", "parameter": "default", "commandType": "command"}
    # signed headers present; secret never appears in headers values
    assert set(["Authorization", "sign", "t", "nonce", "Content-Type"]) <= set(c["headers"])
    assert "SEC" not in " ".join(map(str, c["headers"].values()))

def test_send_command_raises_on_non_100_statuscode():
    http = _fake_http([], [(200, {"statusCode": 161, "message": "device offline"})])
    with pytest.raises(sc.SwitchBotCloudError):
        sc.send_command("X", "turnOn", token="T", secret="S", http_fn=http)

def test_send_command_retries_transient_then_succeeds():
    calls = []
    def http_fn(method, url, headers, body):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("boom")
        return (200, {"statusCode": 100, "message": "success"})
    sc.send_command("X", "turnOff", token="T", secret="S", http_fn=http_fn, sleep_fn=lambda s: None)
    assert len(calls) == 2

def test_to_command_maps_actions():
    assert sc.to_command("on") == "turnOn"
    assert sc.to_command("off") == "turnOff"
    assert sc.to_command("press") == "press"
