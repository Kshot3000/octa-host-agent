#!/usr/bin/env python3
"""
octa-host-agent — a tiny, dependency-free monitoring + control agent for
OctaSpace GPU hosts.

Run it on each rig you host with. It serves a small authenticated JSON API
that the OctaSpace Hub "Host Console" page (and its installable PWA) talks to:

  GET  /api/health       minimal liveness check (no auth)
  GET  /api/status       GPU temps/fan/power/util/VRAM, CPU, RAM, disk,
                         uptime, node-service state, idle-job state (auth)
  POST /api/power        {"action": "shutdown"|"reboot", "confirm": "SHUTDOWN"|"REBOOT"}
  POST /api/node         {"action": "start"|"stop"|"restart"}  (node service)
  GET  /api/price        stored target price + whether it was applied
  POST /api/price        set target price; if your OctaSpace API key is in
                         the agent config, it is ALSO applied via the official
                         OctaSpace API (PATCH /nodes/{id}/prices)
  GET  /api/idle-job     current idle-job setting + runtime state
  POST /api/idle-job     {"mode": "off"|"command"|"pause", "command": "..."}
                         or {"action": "start"|"stop"} for manual control
  POST /api/wol          {"mac": "AA:BB:CC:DD:EE:FF"} — send a Wake-on-LAN
                         magic packet (the only real remote "power on")
  GET  /api/octa/nodes   your nodes from the official OctaSpace API
                         (only when octa_api_key is configured)

SECURITY: the agent binds 0.0.0.0 by default and every endpoint except
/api/health requires your bearer token. Use it on your LAN or over
Tailscale/WireGuard/a VPN — NEVER port-forward it to the raw internet.

Python 3.8+ standard library only. GPU data comes from `nvidia-smi`.

Part of an unofficial community project (OctaSpace Hub). Not affiliated
with the OctaSpace team.
"""

import argparse
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT_VERSION = "1.0.0"
DEFAULT_PORT = 8787
OCTA_API_BASE_DEFAULT = "https://api.octa.space"
DEFAULT_CORS_ORIGINS = ["https://kshot3000.github.io"]
# systemd unit names tried (in order) when none is configured explicitly:
DEFAULT_SERVICE_NAMES = ["octa-node", "octaspace-node", "octaspace", "orc"]
# docker container name patterns tried (substring match):
DEFAULT_CONTAINER_PATTERNS = ["orc", "octa-node", "octaspace"]

CONFIG_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def default_config_path():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "octa-host-agent", "config.json")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "octa-host-agent", "config.json")


class Config:
    """JSON-file backed config. Secrets (agent token, OctaSpace API key)
    live ONLY in this file on the rig — never in the web page."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data = json.load(fh)
        else:
            self.data = {}
        changed = False
        if not self.data.get("token"):
            # First run: generate the console bearer token and persist it.
            self.data["token"] = secrets.token_urlsafe(32)
            changed = True
        defaults = {
            "host": "0.0.0.0",
            "port": DEFAULT_PORT,
            "octa_api_key": "",
            "octa_api_base": OCTA_API_BASE_DEFAULT,
            "node_service": "",            # systemd unit name; "" = auto-detect
            "node_container": "",          # docker container name; "" = auto
            "node_control": "auto",        # auto | systemd | docker | none
            "allow_power_actions": True,
            "cors_origins": list(DEFAULT_CORS_ORIGINS),
            "wol_broadcast": "255.255.255.255",
            "wol_port": 9,
            "price_target": None,          # {node_id, base_cents, ...}
            "idle_job": {"mode": "off", "command": ""},
        }
        for key, val in defaults.items():
            if key not in self.data:
                self.data[key] = val
                changed = True
        if changed:
            self.save()

    def save(self):
        with CONFIG_LOCK:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass  # Windows / odd filesystems

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()


# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------

def run_cmd(args, timeout=8):
    """Run a command, return (exit_code, stdout). Never raises."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip()
    except FileNotFoundError:
        return None, ""
    except subprocess.TimeoutExpired:
        return -1, ""
    except Exception:
        return -2, ""


def read_gpus():
    """GPU telemetry via nvidia-smi. Returns (gpus, error_or_None)."""
    if not shutil.which("nvidia-smi"):
        return [], "nvidia-smi not found — no NVIDIA GPU detected by this agent"
    code, out = run_cmd([
        "nvidia-smi",
        "--query-gpu=index,name,temperature.gpu,fan.speed,power.draw,"
        "utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ])
    if code != 0 or not out:
        return [], "nvidia-smi failed (exit %s) — GPU telemetry unavailable" % code
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 8:
            continue

        def num(v):
            try:
                return float(v)
            except ValueError:
                return None  # e.g. fan.speed "N/A" on passively cooled cards

        gpus.append({
            "index": int(num(parts[0]) or 0),
            "name": parts[1],
            "temp_c": num(parts[2]),
            "fan_percent": num(parts[3]),
            "power_w": num(parts[4]),
            "util_percent": num(parts[5]),
            "vram_used_mb": num(parts[6]),
            "vram_total_mb": num(parts[7]),
        })
    if not gpus:
        return [], "nvidia-smi returned no parseable GPU rows"
    return gpus, None


def read_cpu():
    cpu = {"count": os.cpu_count(), "load_1m": None, "load_5m": None,
           "load_15m": None, "load_percent": None, "temp_c": None}
    try:
        one, five, fifteen = os.getloadavg()
        cpu["load_1m"] = round(one, 2)
        cpu["load_5m"] = round(five, 2)
        cpu["load_15m"] = round(fifteen, 2)
        if cpu["count"]:
            cpu["load_percent"] = round(min(100.0, one / cpu["count"] * 100.0), 1)
    except (OSError, AttributeError):
        pass
    # Best-effort CPU/package temp from Linux thermal zones.
    try:
        import glob
        temps = []
        for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
            try:
                with open(os.path.join(zone, "temp")) as fh:
                    milli = int(fh.read().strip())
                c = milli / 1000.0
                if 0 < c < 130:
                    temps.append(c)
            except (OSError, ValueError):
                continue
        if temps:
            cpu["temp_c"] = round(max(temps), 1)
    except Exception:
        pass
    return cpu


def read_ram():
    ram = {"total_mb": None, "used_mb": None, "available_mb": None,
           "used_percent": None}
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])  # kB
        total = info.get("MemTotal")
        avail = info.get("MemAvailable")
        if total:
            ram["total_mb"] = round(total / 1024)
            if avail is not None:
                ram["available_mb"] = round(avail / 1024)
                ram["used_mb"] = round((total - avail) / 1024)
                ram["used_percent"] = round((total - avail) / total * 100, 1)
    except (OSError, ValueError):
        pass
    return ram


def read_disk():
    try:
        usage = shutil.disk_usage(os.path.expanduser("~"))
        return {"total_gb": round(usage.total / 1e9, 1),
                "used_gb": round(usage.used / 1e9, 1),
                "free_gb": round(usage.free / 1e9, 1),
                "used_percent": round(usage.used / usage.total * 100, 1)}
    except OSError:
        return {"total_gb": None, "used_gb": None, "free_gb": None,
                "used_percent": None}


def read_uptime_s():
    try:
        with open("/proc/uptime") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


# --------------------------------------------------------------------------
# OctaSpace node service detection / control
# --------------------------------------------------------------------------

def _systemctl(args, timeout=15):
    return run_cmd(["systemctl"] + args, timeout=timeout)


def detect_node(config):
    """Best-effort detection of how the OctaSpace node runs on this rig.
    Returns dict: {state: running|stopped|unknown, via, target, detail}."""
    mode = config.get("node_control", "auto")
    if mode == "none":
        return {"state": "unknown", "via": None, "target": None,
                "detail": "node control disabled in agent config (node_control=none)"}
    # systemd
    if mode in ("auto", "systemd") and shutil.which("systemctl"):
        names = []
        if config.get("node_service"):
            names.append(config["node_service"])
        names += [n for n in DEFAULT_SERVICE_NAMES if n not in names]
        for name in names:
            code, out = _systemctl(["is-active", name])
            if code == 0 and out == "active":
                return {"state": "running", "via": "systemd", "target": name,
                        "detail": "systemd unit %s is active" % name}
            if code is not None and out in ("inactive", "failed"):
                # unit exists but is not running — confirm it exists at all
                code3, out3 = _systemctl(["status", name, "--no-pager", "-n", "0"])
                if code3 is not None and "could not be found" not in out3.lower():
                    return {"state": "stopped", "via": "systemd", "target": name,
                            "detail": "systemd unit %s is %s" % (name, out)}
    # docker
    if mode in ("auto", "docker") and shutil.which("docker"):
        code, out = run_cmd(["docker", "ps", "-a", "--format",
                             "{{.Names}}\t{{.State}}"])
        if code == 0:
            patterns = []
            if config.get("node_container"):
                patterns.append(config["node_container"].lower())
            patterns += [p for p in DEFAULT_CONTAINER_PATTERNS
                         if p not in patterns]
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) != 2:
                    continue
                name, state = parts[0], parts[1].lower()
                if any(p in name.lower() for p in patterns):
                    running = state.startswith("running")
                    return {"state": "running" if running else "stopped",
                            "via": "docker", "target": name,
                            "detail": "docker container %s is %s" % (name, state)}
    return {"state": "unknown", "via": None, "target": None,
            "detail": "no OctaSpace node service detected (tried systemd units "
                      "%s and docker containers matching %s); set node_service "
                      "or node_container in the agent config if your node "
                      "runs another way"
                      % (", ".join(DEFAULT_SERVICE_NAMES),
                         "/".join(DEFAULT_CONTAINER_PATTERNS))}


def control_node(config, action):
    """Start/stop/restart the node service. Returns (ok, detail)."""
    if action not in ("start", "stop", "restart"):
        return False, "unknown node action: %s" % action
    mode = config.get("node_control", "auto")
    if mode == "none":
        return False, "node control is disabled in agent config (node_control=none)"
    info = detect_node(config)
    if info["via"] == "systemd":
        verb = {"start": "start", "stop": "stop", "restart": "restart"}[action]
        code, out = _systemctl([verb, info["target"]], timeout=30)
        if code == 0:
            return True, "systemctl %s %s succeeded" % (verb, info["target"])
        return False, ("systemctl %s %s failed (exit %s). The agent may need "
                       "sudo/polkit rights for this unit. Output: %s"
                       % (verb, info["target"], code, out[:300]))
    if info["via"] == "docker":
        verb = {"start": "start", "stop": "stop", "restart": "restart"}[action]
        code, out = run_cmd(["docker", verb, info["target"]], timeout=60)
        if code == 0:
            return True, "docker %s %s succeeded" % (verb, info["target"])
        return False, "docker %s %s failed (exit %s): %s" % (
            verb, info["target"], code, out[:300])
    return False, ("cannot %s the node: %s" % (action, info["detail"]))


# --------------------------------------------------------------------------
# Official OctaSpace API (api.octa.space) — the host's OWN API key, held only
# in the agent config on the rig. Verified against the official API docs /
# swagger spec (https://api.octa.space/api-docs):
#   GET   /nodes                  list the account's nodes
#   PATCH /nodes/{id}/prices      {"base": cents, "storage": cents,
#                                  "traffic": cents}  — USD cents per spec
#   GET   /nodes/{id}/reboot      platform-side node reboot
# Auth: "authorization: <api key>" header.
# --------------------------------------------------------------------------

def octa_api(config, method, path, body=None, timeout=15):
    """Call the official OctaSpace API. Returns (status, parsed_or_text)."""
    key = config.get("octa_api_key") or ""
    if not key:
        return None, "no OctaSpace API key configured on the agent"
    base = (config.get("octa_api_base") or OCTA_API_BASE_DEFAULT).rstrip("/")
    url = base + path
    data = None
    headers = {"authorization": key, "accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw
    except Exception as exc:  # DNS, TLS, timeout...
        return None, "request to OctaSpace API failed: %s" % exc


def apply_price_via_api(config, target):
    """Apply a stored price target through the official API.
    Returns (applied_bool, detail). Honest: False unless the API said 2xx."""
    node_id = target.get("node_id")
    if not node_id:
        return False, "no node_id in the price target — nothing to apply to"
    body = {}
    for field in ("base_cents", "storage_cents", "traffic_cents"):
        if target.get(field) is not None:
            body[field.replace("_cents", "")] = int(target[field])
    if not body:
        return False, "price target has no price fields set"
    status, payload = octa_api(config, "PATCH", "/nodes/%s/prices" % node_id,
                               body=body)
    if status is None:
        return False, payload
    if 200 <= status < 300:
        return True, "OctaSpace API accepted the new prices (HTTP %s)" % status
    return False, "OctaSpace API rejected the price change (HTTP %s): %s" % (
        status, json.dumps(payload)[:300] if not isinstance(payload, str)
        else payload[:300])


# --------------------------------------------------------------------------
# Idle job manager
#
# The host configures an idle behaviour:
#   mode "off"     — do nothing when the rig is unrented
#   mode "command" — run this shell command while no rental is active
#                    (e.g. a miner). Started/stopped automatically ONLY when
#                    rental state can be detected via the official API
#                    (needs octa_api_key); otherwise start/stop manually
#                    from the console and the agent reports that honestly.
#   mode "pause"   — recorded preference: keep the node paused from taking
#                    new work. The OctaSpace API exposes no node pause
#                    switch, so the agent only stores + reports this; it
#                    does not pretend to enforce it.
# --------------------------------------------------------------------------

class IdleJobManager:
    def __init__(self, config):
        self.config = config
        self.proc = None
        self.started_at = None
        self.last_detection = "not checked yet"
        self.lock = threading.Lock()

    def setting(self):
        s = self.config.get("idle_job") or {}
        return {"mode": s.get("mode", "off"), "command": s.get("command", "")}

    def is_running(self):
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def start(self, why):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "idle command already running (pid %s)" % self.proc.pid
            cmd = self.setting()["command"]
            if not cmd:
                return False, "no idle command configured"
            try:
                self.proc = subprocess.Popen(
                    cmd, shell=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                self.started_at = time.time()
                return True, "idle command started (pid %s) — %s" % (
                    self.proc.pid, why)
            except Exception as exc:
                return False, "failed to start idle command: %s" % exc

    def stop(self, why):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.proc = None
                return False, "idle command is not running"
            pid = self.proc.pid
            try:
                import signal
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
            self.proc = None
            self.started_at = None
            return True, "idle command stopped (pid %s) — %s" % (pid, why)

    def rental_state(self):
        """Detect rental activity via the official API, when possible.
        Returns (state, detail): state in rented|idle|unknown."""
        if not self.config.get("octa_api_key"):
            return "unknown", ("no OctaSpace API key on the agent — rental "
                               "activity cannot be detected automatically")
        status, payload = octa_api(self.config, "GET", "/nodes")
        if status is None or not (200 <= status < 300):
            return "unknown", "OctaSpace API node list unavailable (HTTP %s)" % status
        nodes = payload if isinstance(payload, list) else payload.get("data", [])
        if not isinstance(nodes, list):
            return "unknown", "unexpected OctaSpace API response shape"
        states = [str(n.get("state", "")).lower() for n in nodes
                  if isinstance(n, dict)]
        if any(s == "busy" for s in states):
            return "rented", "at least one node reports state=busy"
        if states:
            return "idle", "all %d node(s) report state=idle" % len(states)
        return "unknown", "account has no nodes listed"

    def watcher_tick(self):
        """One automatic-management pass (called by the watcher thread)."""
        setting = self.setting()
        if setting["mode"] != "command":
            self.last_detection = "auto-management off (mode=%s)" % setting["mode"]
            return
        state, detail = self.rental_state()
        self.last_detection = "%s — %s" % (state, detail)
        if state == "idle" and not self.is_running():
            self.start("no rental active")
        elif state == "rented" and self.is_running():
            self.stop("a rental became active")

    def status(self):
        setting = self.setting()
        running = self.is_running()
        return {
            "mode": setting["mode"],
            "command": setting["command"],
            "process_running": running,
            "pid": (self.proc.pid if running else None),
            "running_since": (int(self.started_at) if running and
                              self.started_at else None),
            "auto_detection": self.last_detection,
            "note": ("mode 'pause' is a stored preference only — the "
                     "OctaSpace API has no node pause switch, so the agent "
                     "does not enforce it"
                     if setting["mode"] == "pause" else None),
        }


def idle_watcher(manager, stop_event):
    while not stop_event.is_set():
        try:
            manager.watcher_tick()
        except Exception:
            pass  # the watcher must never kill the agent
        stop_event.wait(60)


# --------------------------------------------------------------------------
# Power actions + Wake-on-LAN
# --------------------------------------------------------------------------

def do_power(action, demo=False):
    """shutdown/reboot the rig itself. Returns (ok, detail)."""
    if demo:
        return True, ("DEMO MODE: SIMULATED %s — no real power action was "
                      "taken on any machine" % action)
    if os.name == "nt":
        flag = "/s" if action == "shutdown" else "/r"
        code, out = run_cmd(["shutdown", flag, "/t", "5"], timeout=10)
        return code == 0, "shutdown %s /t 5 (exit %s)" % (flag, code)
    # Linux: try systemd first, fall back to the classic shutdown binary.
    verb = "poweroff" if action == "shutdown" else "reboot"
    code, out = run_cmd(["systemctl", verb], timeout=10)
    if code == 0:
        return True, "systemctl %s issued" % verb
    flag = "-h" if action == "shutdown" else "-r"
    code2, out2 = run_cmd(["shutdown", flag, "now"], timeout=10)
    if code2 == 0:
        return True, "shutdown %s now issued" % flag
    return False, ("both systemctl %s (exit %s) and shutdown %s (exit %s) "
                   "failed — the agent likely lacks permission to power "
                   "off this machine (run it as root/admin or grant rights)"
                   % (verb, code, flag, code2))


MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$|"
                    r"^[0-9A-Fa-f]{12}$")


def build_magic_packet(mac):
    """102-byte WoL magic packet for a MAC, or None if the MAC is invalid."""
    if not MAC_RE.match((mac or "").strip()):
        return None
    hexmac = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(hexmac) != 12:
        return None
    return bytes.fromhex("ff" * 6 + hexmac * 16)


def send_wol(mac, broadcast, port):
    """Send a Wake-on-LAN magic packet. Returns (ok, detail)."""
    payload = build_magic_packet(mac)
    if payload is None:
        return False, "invalid MAC address: %r" % mac
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # The broadcast flag is only needed for actual broadcast targets;
            # a directed address (e.g. a specific LAN IP) works without it.
            if broadcast == "255.255.255.255" or broadcast.endswith(".255"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(payload, (broadcast, int(port)))
        return True, ("magic packet sent to %s via %s:%s — the target rig "
                      "still needs Wake-on-LAN enabled in BIOS/NIC settings "
                      "and wired power" % (mac, broadcast, port))
    except Exception as exc:
        return False, "failed to send magic packet: %s" % exc


# --------------------------------------------------------------------------
# Demo data — CLEARLY LABELED simulated rig, for trying the console with
# no hardware. Payload carries demo:true so the UI can banner it loudly.
# --------------------------------------------------------------------------

def demo_status():
    t = time.time()
    wobble = (int(t) % 7) - 3
    return {
        "demo": True,
        "data_source": "simulated-demo",
        "hostname": "demo-rig",
        "os": "Ubuntu 24.04 LTS (SIMULATED)",
        "agent_version": AGENT_VERSION,
        "uptime_s": 93784,
        "gpus": [
            {"index": 0, "name": "NVIDIA GeForce RTX 4090 (SIMULATED)",
             "temp_c": 67 + wobble, "fan_percent": 54.0, "power_w": 318.5,
             "util_percent": 96.0, "vram_used_mb": 19850.0,
             "vram_total_mb": 24564.0},
            {"index": 1, "name": "NVIDIA GeForce RTX 4090 (SIMULATED)",
             "temp_c": 61 + wobble, "fan_percent": 47.0, "power_w": 121.2,
             "util_percent": 12.0, "vram_used_mb": 2400.0,
             "vram_total_mb": 24564.0},
        ],
        "gpu_error": None,
        "cpu": {"count": 16, "load_1m": 3.42, "load_5m": 3.10,
                "load_15m": 2.87, "load_percent": 21.4, "temp_c": 58.0},
        "ram": {"total_mb": 65536, "used_mb": 18230, "available_mb": 47306,
                "used_percent": 27.8},
        "disk": {"total_gb": 2000.0, "used_gb": 812.4, "free_gb": 1187.6,
                 "used_percent": 40.6},
        "node": {"state": "running", "via": "simulated",
                 "target": "demo-node",
                 "detail": "SIMULATED node state — demo mode only"},
        "ts": int(t),
    }


def real_status(config, idle_manager):
    gpus, gpu_error = read_gpus()
    return {
        "demo": False,
        "data_source": "live-agent",
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "agent_version": AGENT_VERSION,
        "uptime_s": read_uptime_s(),
        "gpus": gpus,
        "gpu_error": gpu_error,
        "cpu": read_cpu(),
        "ram": read_ram(),
        "disk": read_disk(),
        "node": detect_node(config),
        "idle_job": idle_manager.status(),
        "price_target": config.get("price_target"),
        "ts": int(time.time()),
    }


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

class AgentState:
    def __init__(self, config, demo):
        self.config = config
        self.demo = demo
        self.idle = IdleJobManager(config)
        self.stop_event = threading.Event()
        self.watcher = None

    def start_watcher(self):
        if self.demo:
            return
        self.watcher = threading.Thread(
            target=idle_watcher, args=(self.idle, self.stop_event),
            daemon=True)
        self.watcher.start()


class Handler(BaseHTTPRequestHandler):
    server_version = "octa-host-agent/" + AGENT_VERSION
    state = None  # set in main()

    # -- plumbing -----------------------------------------------------------
    def log_message(self, fmt, *args):  # keep logs terse
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _cors_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return None
        allowed = self.state.config.get("cors_origins") or []
        if "*" in allowed or origin in allowed:
            return origin
        return None

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authed(self):
        token = self.state.config.get("token") or ""
        header = self.headers.get("Authorization") or ""
        presented = ""
        if header.lower().startswith("bearer "):
            presented = header[7:].strip()
        elif self.headers.get("X-Agent-Token"):
            presented = self.headers.get("X-Agent-Token").strip()
        return bool(token) and hmac.compare_digest(presented, token)

    def _require_auth(self):
        if self._authed():
            return True
        self._send(401, {"ok": False, "error": "unauthorized — missing or "
                                              "invalid agent token"})
        return False

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 65536))
        try:
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/api/health":
            self._send(200, {"ok": True, "agent": "octa-host-agent",
                             "version": AGENT_VERSION,
                             "demo": self.state.demo})
            return
        if not self._require_auth():
            return
        if path == "/api/status":
            if self.state.demo:
                data = demo_status()
                data["idle_job"] = self.state.idle.status()
                data["price_target"] = self.state.config.get("price_target")
                self._send(200, {"ok": True, "status": data})
            else:
                self._send(200, {"ok": True,
                                 "status": real_status(self.state.config,
                                                       self.state.idle)})
            return
        if path == "/api/price":
            self._send(200, {"ok": True,
                             "price_target": self.state.config.get("price_target"),
                             "octa_api_key_configured":
                                 bool(self.state.config.get("octa_api_key"))})
            return
        if path == "/api/idle-job":
            self._send(200, {"ok": True,
                             "idle_job": self.state.idle.status()})
            return
        if path == "/api/octa/nodes":
            if not self.state.config.get("octa_api_key"):
                self._send(400, {"ok": False, "error":
                                 "no OctaSpace API key configured on the "
                                 "agent — add octa_api_key to the agent "
                                 "config on the rig to use this"})
                return
            status, payload = octa_api(self.state.config, "GET", "/nodes")
            if status is None:
                self._send(502, {"ok": False, "error": payload})
                return
            self._send(200 if 200 <= status < 300 else status,
                       {"ok": 200 <= status < 300, "nodes": payload})
            return
        self._send(404, {"ok": False, "error": "no such endpoint: %s" % path})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if not self._require_auth():
            return
        body = self._body()
        cfg = self.state.config

        if path == "/api/power":
            action = str(body.get("action") or "").lower()
            confirm = str(body.get("confirm") or "")
            if action not in ("shutdown", "reboot"):
                self._send(400, {"ok": False, "error":
                                 "action must be 'shutdown' or 'reboot'"})
                return
            if confirm != action.upper():
                self._send(400, {"ok": False, "error":
                                 "confirmation required: send confirm='%s'"
                                 % action.upper()})
                return
            if not cfg.get("allow_power_actions", True):
                self._send(403, {"ok": False, "error":
                                 "power actions are disabled in the agent "
                                 "config (allow_power_actions=false)"})
                return
            ok, detail = do_power(action, demo=self.state.demo)
            self._send(200 if ok else 500,
                       {"ok": ok, "action": action, "detail": detail,
                        "demo": self.state.demo})
            return

        if path == "/api/node":
            action = str(body.get("action") or "").lower()
            if self.state.demo:
                self._send(200, {"ok": True, "action": action, "demo": True,
                                 "detail": "DEMO MODE: simulated node %s — "
                                           "nothing real was controlled"
                                           % action})
                return
            ok, detail = control_node(cfg, action)
            self._send(200 if ok else 422,
                       {"ok": ok, "action": action, "detail": detail})
            return

        if path == "/api/price":
            target = dict(cfg.get("price_target") or {})
            if body.get("node_id") is not None:
                target["node_id"] = body["node_id"]
            for field in ("base_cents", "storage_cents", "traffic_cents"):
                if body.get(field) is not None:
                    try:
                        target[field] = int(body[field])
                    except (TypeError, ValueError):
                        self._send(400, {"ok": False, "error":
                                         "%s must be an integer (USD cents)"
                                         % field})
                        return
            if not target:
                self._send(400, {"ok": False, "error": "empty price target"})
                return
            target["saved_at"] = int(time.time())
            applied, detail = False, ("saved on this rig only — no OctaSpace "
                                      "API key is configured on the agent, so "
                                      "nothing was sent to OctaSpace. Add "
                                      "octa_api_key to the agent config to "
                                      "apply prices via the official API, or "
                                      "apply this target in your OctaSpace "
                                      "service listing.")
            if cfg.get("octa_api_key"):
                applied, detail = apply_price_via_api(cfg, target)
            target["applied"] = applied
            target["apply_detail"] = detail
            cfg.set("price_target", target)
            self._send(200, {"ok": True, "price_target": target,
                             "applied": applied, "detail": detail})
            return

        if path == "/api/idle-job":
            if "action" in body:  # manual start/stop of the idle command
                action = str(body.get("action") or "").lower()
                if self.state.demo:
                    self._send(200, {"ok": True, "demo": True,
                                     "detail": "DEMO MODE: simulated idle "
                                               "command %s" % action,
                                     "idle_job": self.state.idle.status()})
                    return
                if action == "start":
                    ok, detail = self.state.idle.start("manual start from console")
                elif action == "stop":
                    ok, detail = self.state.idle.stop("manual stop from console")
                else:
                    self._send(400, {"ok": False, "error":
                                     "action must be 'start' or 'stop'"})
                    return
                self._send(200 if ok else 422,
                           {"ok": ok, "detail": detail,
                            "idle_job": self.state.idle.status()})
                return
            mode = str(body.get("mode") or "").lower()
            if mode not in ("off", "command", "pause"):
                self._send(400, {"ok": False, "error":
                                 "mode must be 'off', 'command' or 'pause'"})
                return
            command = str(body.get("command") or "")
            if mode == "command" and not command:
                self._send(400, {"ok": False, "error":
                                 "mode 'command' requires a command string"})
                return
            cfg.set("idle_job", {"mode": mode, "command": command})
            self._send(200, {"ok": True, "idle_job": self.state.idle.status(),
                             "detail": "idle-job setting saved on the rig"})
            return

        if path == "/api/wol":
            mac = str(body.get("mac") or "")
            ok, detail = send_wol(mac, cfg.get("wol_broadcast") or
                                  "255.255.255.255", cfg.get("wol_port") or 9)
            self._send(200 if ok else 400, {"ok": ok, "detail": detail})
            return

        if path == "/api/octa/reboot":
            node_id = body.get("node_id")
            if not node_id:
                self._send(400, {"ok": False, "error": "node_id required"})
                return
            if not cfg.get("octa_api_key"):
                self._send(400, {"ok": False, "error":
                                 "no OctaSpace API key configured on the agent"})
                return
            status, payload = octa_api(cfg, "GET",
                                       "/nodes/%s/reboot" % node_id)
            ok = status is not None and 200 <= status < 300
            self._send(200 if ok else 502,
                       {"ok": ok, "detail": "official OctaSpace API reboot "
                        "for node %s returned HTTP %s" % (node_id, status),
                        "response": payload})
            return

        self._send(404, {"ok": False, "error": "no such endpoint: %s" % path})


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="octa-host-agent — OctaSpace host monitoring/control agent")
    parser.add_argument("--config", default=None,
                        help="config file path (default: %s)"
                             % default_config_path())
    parser.add_argument("--demo", action="store_true",
                        help="serve clearly-labeled SIMULATED rig data so "
                             "anyone can try the console without a rig")
    parser.add_argument("--print-token", action="store_true",
                        help="print the console token and exit")
    args = parser.parse_args(argv)

    config = Config(args.config or default_config_path())
    if args.print_token:
        print(config.get("token"))
        return 0

    state = AgentState(config, demo=args.demo)
    Handler.state = state
    state.start_watcher()

    host = config.get("host") or "0.0.0.0"
    port = int(config.get("port") or DEFAULT_PORT)
    server = ThreadingHTTPServer((host, port), Handler)
    print("octa-host-agent %s listening on http://%s:%d (demo=%s)"
          % (AGENT_VERSION, host, port, args.demo), flush=True)
    print("Console token (keep secret): %s" % config.get("token"), flush=True)
    print("Config file: %s" % config.path, flush=True)
    if not config.get("octa_api_key"):
        print("Note: no octa_api_key in config — price changes will be "
              "saved locally only, and OctaSpace node listing/reboot via "
              "the official API is disabled.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
