---
name: deploy-box
description: Deploy the latest smart-home code from this Mac to the 24/7 home box (the agent "Menashe") and verify it actually runs. Use whenever the user says "deploy", "deploy to the box", "update the box", "push to the box", "ship it", or has just committed a change that should go live on the home agent. Handles rsync of changed code over Tailscale, dependency reinstall when manifests change, systemd restart, and a mandatory health check — never claims success without verifying the logs.
---

# Deploy to the home box

Ship the smart-home agent code from this Mac to the box that runs it 24/7, restart the
service, and **verify it's healthy**. rsync-based: no GitHub creds or git state on the box required.

## The box (facts)

- **SSH:** `ssh -i ~/.ssh/smarthome_box nathaniel@100.111.96.97` — Tailscale, reachable anywhere.
- **Repo on box:** `/home/nathaniel/smart-home` · **Service:** `home-agent` (systemd, auto-start, `Restart=always`).
- **Env:** Ubuntu 22.04, Python **3.11** venv at `~/smart-home/.venv`.
- **Box-local files — NEVER overwrite** (they differ from the Mac / hold secrets): `.env`,
  `home_agent.db` (+ any `*.db`), `secrets/`, `devices.yaml` (Linux MACs, not the Mac's macOS UUIDs),
  `roborock_userdata.json`, `collector/node_modules`, `.venv`.

## Procedure

### 1. Preflight
```bash
ssh -i ~/.ssh/smarthome_box -o ConnectTimeout=8 -o BatchMode=yes nathaniel@100.111.96.97 'echo ok' \
  || echo "box unreachable — check: tailscale status"
git -C /Users/netanelsade/smart-home status -sb
git -C /Users/netanelsade/smart-home diff --stat HEAD
```
Note what changed — you need it to decide step 3.

### 2. Sync code (rsync over Tailscale)
Excludes every box-local file and build artifact. **No `--delete`** (it would remove box-only helper
scripts and is easy to get wrong):
```bash
rsync -az \
  --exclude '.venv' --exclude 'node_modules' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.env' --exclude '*.db' --exclude 'devices.yaml' --exclude 'roborock_userdata.json' \
  --exclude 'secrets/' --exclude '.git' --exclude '.finance_sync.lock' \
  -e "ssh -i ~/.ssh/smarthome_box -o BatchMode=yes" \
  /Users/netanelsade/smart-home/ nathaniel@100.111.96.97:~/smart-home/
```

### 3. Reinstall deps — ONLY if a manifest changed
- `pyproject.toml` changed → `ssh … 'cd ~/smart-home && .venv/bin/pip install -e . -q'`
- `collector/package.json` changed → `ssh … 'cd ~/smart-home/collector && npm install'`
- Otherwise skip (faster).

### 4. Restart the service
```bash
ssh -i ~/.ssh/smarthome_box -o BatchMode=yes nathaniel@100.111.96.97 'sudo systemctl restart home-agent'
```

### 5. Verify — DO NOT SKIP, DO NOT ASSUME
```bash
ssh -i ~/.ssh/smarthome_box -o BatchMode=yes nathaniel@100.111.96.97 'bash -s' <<'EOF'
sleep 6
echo "active:   $(systemctl is-active home-agent)"
echo "instances:$(pgrep -cf "python -m home_agent")"
sudo journalctl -u home-agent -n 20 --no-pager | grep -iE "starting|Application started|error|traceback|conflict"
EOF
```
**Pass criteria (all must hold):** `active`; exactly **1** instance; log shows `Application started`;
**no** `Traceback` / `ERROR` / `Conflict`. If any fail → show the logs, say it failed, do **not** claim success.

### 6. Smoke test (optional, when the change touches agent behavior)
Drive one real agent turn (uses OpenAI):
```bash
ssh … 'cd ~/smart-home && .venv/bin/python scripts/agent_smoke.py "מה השעה?"'
```
Expect a coherent Hebrew reply.

## Gotchas (these have bitten us)
- **Exactly ONE instance.** Never launch a manual `python -m home_agent` while the service runs → Telegram
  `getUpdates` `Conflict`. The systemd service *is* the single instance.
- **Restart is mandatory.** Config, tools, device registry, and the system prompt load once at startup;
  code changes do nothing until `systemctl restart home-agent`.
- **Never overwrite box-local files** — `.env`, `devices.yaml` (Linux MACs), DBs, `secrets/`. They're excluded
  in step 2 on purpose; re-adding them would break the deployment.
- **Bluetooth LE** stays on via `ControllerMode = dual` in `/etc/bluetooth/main.conf`. If BLE ever breaks after
  an OS update, re-check that line.
- **Sudo** on the box is passwordless (`/etc/sudoers.d/nathaniel`).
