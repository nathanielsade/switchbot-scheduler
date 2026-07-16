import base64, hashlib, hmac, json, logging, time, uuid
import urllib.error
import urllib.request

log = logging.getLogger("home_agent")
_BASE = "https://api.switch-bot.com/v1.1"
_TIMEOUT = 10
_RETRIES = 2
_COMMANDS = {"on": "turnOn", "off": "turnOff", "press": "press"}


class SwitchBotCloudError(Exception):
    pass


def to_command(action: str) -> str:
    try:
        return _COMMANDS[action]
    except KeyError:
        raise SwitchBotCloudError(f"unknown action {action!r}")


def _sign(token: str, secret: str, t: str, nonce: str) -> str:
    mac = hmac.new(secret.encode(), (token + t + nonce).encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def _headers(token: str, secret: str) -> dict:
    t = str(int(time.time() * 1000)); nonce = str(uuid.uuid4())
    return {"Authorization": token, "sign": _sign(token, secret, t, nonce),
            "t": t, "nonce": nonce, "Content-Type": "application/json"}


def _real_http(method, url, headers, body):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _call(method, url, *, token, secret, body=None, http_fn=None, sleep_fn=None):
    http_fn = http_fn or _real_http
    sleep_fn = sleep_fn or time.sleep
    last = None
    for attempt in range(_RETRIES + 1):
        try:
            status, payload = http_fn(method, url, _headers(token, secret), body)
        except Exception as e:  # transient (timeout/conn) — retry
            last = SwitchBotCloudError(f"request failed: {type(e).__name__}")
            if attempt < _RETRIES: sleep_fn(1 + attempt)
            continue
        if status // 100 == 5:  # server error — retry
            last = SwitchBotCloudError(f"HTTP {status}")
            if attempt < _RETRIES: sleep_fn(1 + attempt)
            continue
        code = payload.get("statusCode")
        if status // 100 != 2 or code != 100:
            raise SwitchBotCloudError(payload.get("message") or f"HTTP {status} statusCode {code}")
        return payload
    raise last


def send_command(device_id, command, *, token, secret, http_fn=None, sleep_fn=None):
    _call("POST", f"{_BASE}/devices/{device_id}/commands", token=token, secret=secret,
          body={"command": command, "parameter": "default", "commandType": "command"},
          http_fn=http_fn, sleep_fn=sleep_fn)
    log.info("cloud command %s -> %s ok", command, device_id)


def get_status(device_id, *, token, secret, http_fn=None):
    return _call("GET", f"{_BASE}/devices/{device_id}/status",
                 token=token, secret=secret, http_fn=http_fn).get("body", {})
