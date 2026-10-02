# octa-host-agent

A tiny, dependency-free agent that lets an **OctaSpace GPU host** watch and
control their own rigs from the
[OctaSpace Hub Host Console](https://kshot3000.github.io/Octaspace-/console.html)
— including from a phone, via the console's installable web app (PWA).

> Unofficial community project. Not affiliated with the OctaSpace team.

A static web page can't read your GPU temperatures or reboot your machine.
This agent is the missing piece: it runs **on your rig**, serves a small
authenticated JSON API on your LAN, and the console talks to it directly
from your browser. Nothing is relayed through any third-party server.

## What it does

| Capability | How |
|---|---|
| GPU temps, fan, power, utilization, VRAM | `nvidia-smi` on the rig (no pip deps) |
| CPU load/temp, RAM, disk, uptime | `/proc` + stdlib |
| OctaSpace node state + start/stop/restart | systemd unit or docker container, auto-detected (configurable) |
| Shutdown / reboot the rig | OS power commands, guarded by an explicit confirm token |
| **Power ON** | Wake-on-LAN magic packet sent by an agent on any always-on LAN machine — the only real remote power-on (needs WoL enabled in BIOS/NIC + wired power). A smart plug + "power on after AC loss" BIOS setting is the other real-world option. |
| Rental price changes | Stored on the rig **and applied** through the official OctaSpace API `PATCH /nodes/{id}/prices` — but only if you put your OctaSpace API key in the agent config (see below). Without a key, the console honestly reports "saved on this rig only". |
| Idle behavior | `off` / run a custom command while unrented (e.g. a miner) / `pause` preference. Auto start/stop of the idle command needs rental detection via the official API (your key); manual start/stop from the console always works. `pause` is stored + reported only — **the OctaSpace API has no node pause switch**, so the agent doesn't pretend to enforce one. |
| Your OctaSpace nodes list / API reboot | Proxied from the official API when your key is configured |

## Install

Requirements: Python 3.8+ (standard library only — nothing to pip install)
and, for GPU telemetry, NVIDIA drivers with `nvidia-smi` on PATH.

```bash
git clone https://github.com/Kshot3000/octa-host-agent
cd octa-host-agent
python3 agent.py            # first run generates your console token
```

The agent prints its **console token** on startup and stores its config at:

- Linux: `~/.config/octa-host-agent/config.json`
- Windows: `%APPDATA%\octa-host-agent\config.json`

Then open the Host Console, add your rig with the agent URL
(e.g. `http://192.168.1.50:8787`) and that token. The token is saved only in
your browser's localStorage.

### Try it without a rig — demo mode

```bash
python3 agent.py --demo
```

Demo mode serves a **clearly labeled simulated rig** (`demo: true` in every
payload; the console shows a persistent "DEMO RIG" banner). Power and node
actions are simulated too — nothing real is touched.

### Run it as a service (Linux)

```bash
sudo mkdir -p /opt/octa-host-agent /etc/octa-host-agent
sudo cp agent.py /opt/octa-host-agent/
sudo cp octa-host-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now octa-host-agent
```

See comments in the unit file about permissions: reading `nvidia-smi` works
as a normal user, but starting/stopping the node unit and powering off need
root or a polkit/sudoers grant for those specific commands.

### Windows notes

The agent runs under plain `python agent.py`. GPU/CPU/disk data works;
RAM and uptime come from Linux `/proc` so they report as unavailable on
Windows in this version. Shutdown/reboot use `shutdown /s|/r /t 5`.
Node-service control targets systemd/docker, so on native Windows it
reports "unknown" honestly — run the node (and agent) under WSL2 or Docker
Desktop if you need node control there.

## Security — read this

- The agent listens on `0.0.0.0:8787` with a bearer token on every endpoint
  except a minimal `/api/health`.
- **Use it on your LAN, or reach it via Tailscale / WireGuard / an SSH
  tunnel. Never port-forward it to the open internet.** Anyone with your
  token can reboot your rig.
- The config file holds your token and (optionally) your OctaSpace API
  key. The agent chmods it `0600` on Linux. Your OctaSpace API key is
  **only** ever sent to `https://api.octa.space` — never to the console
  page, never anywhere else.
- The console page stores the agent token in your browser's localStorage
  on your device only.
- The idle-job command runs with the agent's privileges, via shell. Only
  put commands in the config/console that you would type yourself.

## Config (see `config.example.json`)

| Key | Meaning |
|---|---|
| `token` | Console bearer token (auto-generated on first run) |
| `host` / `port` | Bind address / port (default `0.0.0.0` / `8787`) |
| `octa_api_key` | Optional. Your OctaSpace API key, for price application, node listing/reboot, rental detection |
| `octa_api_base` | Default `https://api.octa.space` |
| `node_service` | systemd unit name (default auto-detect: `octa-node`, `octaspace-node`, `octaspace`, `orc`) |
| `node_container` | docker container name (default auto-detect by substring: `orc`, `octa-node`, `octaspace`) |
| `node_control` | `auto` \| `systemd` \| `docker` \| `none` |
| `allow_power_actions` | `false` disables rig shutdown/reboot entirely |
| `idle_job` | `{"mode": "off"\|"command"\|"pause", "command": "..."}` |
| `cors_origins` | Browser origins allowed to call the agent (default: the hub origin) |
| `wol_broadcast` / `wol_port` | Wake-on-LAN target (default `255.255.255.255` / `9`) |

## API

All endpoints require `Authorization: Bearer <token>` except `/api/health`.

```
GET  /api/health       {"ok": true, "version": ..., "demo": bool}
GET  /api/status       full telemetry + node + idle-job + price target
POST /api/power        {"action": "shutdown"|"reboot", "confirm": "SHUTDOWN"|"REBOOT"}
POST /api/node         {"action": "start"|"stop"|"restart"}
GET  /api/price        stored target + whether an API key is configured
POST /api/price        {"node_id": 42, "base_cents": 60, "storage_cents": 2, "traffic_cents": 1}
GET  /api/idle-job     current setting + runtime state
POST /api/idle-job     {"mode": "off"|"command"|"pause", "command": "..."}
                       or {"action": "start"|"stop"} (manual idle command control)
POST /api/wol          {"mac": "AA:BB:CC:DD:EE:FF"}
GET  /api/octa/nodes   your nodes from the official OctaSpace API (key required)
POST /api/octa/reboot  {"node_id": 42} — reboot via the official API (key required)
```

Price fields follow the official API spec
([api.octa.space/api-docs](https://api.octa.space/api-docs)): `base`,
`storage`, `traffic` are **USD cents**.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

14 tests: auth (including failures), status shape, demo self-labeling,
price save + honest "not applied" reporting, price application against a
stub playing the official API (verifies the real `PATCH /nodes/{id}/prices`
request shape and auth header), idle-job set/get + validation, power
confirm guard, WoL packet construction + validation, CORS behavior.

## Honest limitations

- **Power-on is Wake-on-LAN or a smart plug only.** No software can turn on
  a fully powered-off machine by itself; the agent can only *send* the
  magic packet (from any always-on machine on the LAN).
- Node detection is best-effort. If your node runs in a way the agent
  can't see, status says `unknown` and tells you how to configure it.
- Idle-job auto start/stop needs your OctaSpace API key (rental state
  comes from the official API). Without it, idle commands are manual.
- The OctaSpace API exposes idle-job *status* reads only — idle jobs are
  created in your OctaSpace account, not through this agent.
- No native App Store / Play Store app: the console is an installable
  **PWA** (Android Chrome "Install app", iOS Safari "Add to Home Screen"),
  which is how the phone experience is delivered.

## License

MIT — see the OctaSpace Hub repo conventions. Community-built by
[@kshot9000](https://x.com/kshot9000) ·
[GitHub Kshot3000](https://github.com/Kshot3000).
