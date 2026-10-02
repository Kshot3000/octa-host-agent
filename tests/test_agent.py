"""Unit/integration tests for octa-host-agent.

Spins the real HTTP server on an ephemeral port (demo mode, plus a stub
'OctaSpace API' server for the price-apply path) and exercises the API
over HTTP — including auth failures. Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import threading
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent as agent_mod  # noqa: E402


def call(method, url, token=None, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


class StubOctaAPI(BaseHTTPRequestHandler):
    """Records requests; plays the official OctaSpace API."""
    received = []

    def log_message(self, *a):
        pass

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        StubOctaAPI.received.append({
            "method": self.command, "path": self.path,
            "auth": self.headers.get("authorization"),
            "body": json.loads(raw) if raw else None,
        })
        if self.path == "/nodes":
            payload = [{"id": 42, "state": "idle"}]
        else:
            payload = {"result": "ok"}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_PATCH = _reply


class AgentTestCase(unittest.TestCase):
    demo = True

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.mkdtemp()
        cls.config_path = os.path.join(cls.tmp, "config.json")
        cfg = agent_mod.Config(cls.config_path)
        cfg.set("token", "test-token-123")
        cfg.set("node_control", "none")
        cfg.set("wol_broadcast", "127.0.0.1")
        cls.config = cfg
        state = agent_mod.AgentState(cfg, demo=cls.demo)
        agent_mod.Handler.state = state
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), agent_mod.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    # -- health / auth ------------------------------------------------------
    def test_health_no_auth(self):
        status, body = call("GET", self.base + "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], agent_mod.AGENT_VERSION)

    def test_status_requires_auth(self):
        status, _ = call("GET", self.base + "/api/status")
        self.assertEqual(status, 401)
        status, _ = call("GET", self.base + "/api/status", token="wrong")
        self.assertEqual(status, 401)

    def test_status_shape_demo(self):
        status, body = call("GET", self.base + "/api/status",
                            token="test-token-123")
        self.assertEqual(status, 200)
        s = body["status"]
        self.assertTrue(s["demo"])  # demo rig must self-label
        self.assertEqual(s["data_source"], "simulated-demo")
        self.assertEqual(len(s["gpus"]), 2)
        gpu = s["gpus"][0]
        for field in ("name", "temp_c", "fan_percent", "power_w",
                      "util_percent", "vram_used_mb", "vram_total_mb"):
            self.assertIn(field, gpu)
        for section in ("cpu", "ram", "disk", "node", "idle_job"):
            self.assertIn(section, s)

    # -- price --------------------------------------------------------------
    def test_price_save_without_api_key_is_honest(self):
        status, body = call("POST", self.base + "/api/price",
                            token="test-token-123",
                            body={"node_id": 7, "base_cents": 55})
        self.assertEqual(status, 200)
        self.assertFalse(body["applied"])
        self.assertIn("saved on this rig only", body["detail"])
        status, body = call("GET", self.base + "/api/price",
                            token="test-token-123")
        self.assertEqual(body["price_target"]["base_cents"], 55)
        self.assertFalse(body["octa_api_key_configured"])

    def test_price_rejects_non_integer(self):
        status, _ = call("POST", self.base + "/api/price",
                         token="test-token-123", body={"base_cents": "abc"})
        self.assertEqual(status, 400)

    # -- idle job -----------------------------------------------------------
    def test_idle_job_set_and_get(self):
        status, body = call("POST", self.base + "/api/idle-job",
                            token="test-token-123",
                            body={"mode": "command",
                                  "command": "echo idle-work"})
        self.assertEqual(status, 200)
        self.assertEqual(body["idle_job"]["mode"], "command")
        status, body = call("GET", self.base + "/api/idle-job",
                            token="test-token-123")
        self.assertEqual(body["idle_job"]["command"], "echo idle-work")
        status, _ = call("POST", self.base + "/api/idle-job",
                         token="test-token-123", body={"mode": "bogus"})
        self.assertEqual(status, 400)
        status, _ = call("POST", self.base + "/api/idle-job",
                         token="test-token-123", body={"mode": "command"})
        self.assertEqual(status, 400)  # command required for command mode

    # -- power --------------------------------------------------------------
    def test_power_requires_confirm(self):
        status, _ = call("POST", self.base + "/api/power",
                         token="test-token-123", body={"action": "shutdown"})
        self.assertEqual(status, 400)
        status, _ = call("POST", self.base + "/api/power",
                         token="test-token-123",
                         body={"action": "shutdown", "confirm": "REBOOT"})
        self.assertEqual(status, 400)

    def test_power_demo_is_simulated(self):
        status, body = call("POST", self.base + "/api/power",
                            token="test-token-123",
                            body={"action": "reboot", "confirm": "REBOOT"})
        self.assertEqual(status, 200)
        self.assertTrue(body["demo"])
        self.assertIn("SIMULATED", body["detail"])

    # -- wol ----------------------------------------------------------------
    def test_wol_validation(self):
        status, body = call("POST", self.base + "/api/wol",
                            token="test-token-123", body={"mac": "not-a-mac"})
        self.assertEqual(status, 400)
        self.assertIn("invalid MAC", body["detail"])
        # Packet construction is verified exactly...
        packet = agent_mod.build_magic_packet("AA:BB:CC:DD:EE:FF")
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:12], bytes.fromhex("AABBCCDDEEFF"))
        self.assertIsNone(agent_mod.build_magic_packet("nope"))
        # ...and a valid MAC must never be rejected as invalid. The actual
        # UDP send depends on the host OS permitting broadcast UDP (some
        # sandboxes block it), so accept either a send success or an
        # OS-level send failure — but never an "invalid MAC" verdict.
        status, body = call("POST", self.base + "/api/wol",
                            token="test-token-123",
                            body={"mac": "AA:BB:CC:DD:EE:FF"})
        self.assertNotIn("invalid MAC", body.get("detail", ""))
        self.assertTrue(body["ok"] or
                        "failed to send magic packet" in body["detail"])

    # -- octa proxy guard ---------------------------------------------------
    def test_octa_nodes_requires_key(self):
        status, body = call("GET", self.base + "/api/octa/nodes",
                            token="test-token-123")
        self.assertEqual(status, 400)
        self.assertIn("no OctaSpace API key", body["error"])

    # -- cors ---------------------------------------------------------------
    def test_cors_reflects_hub_origin_only(self):
        req = urllib.request.Request(self.base + "/api/health", method="OPTIONS")
        req.add_header("Origin", "https://kshot3000.github.io")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"),
                             "https://kshot3000.github.io")
        req = urllib.request.Request(self.base + "/api/health", method="OPTIONS")
        req.add_header("Origin", "https://evil.example")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))


class PriceApplyTestCase(unittest.TestCase):
    """With an API key configured (pointed at a local stub playing the
    official API), POST /api/price must PATCH /nodes/{id}/prices in cents."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        StubOctaAPI.received = []
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), StubOctaAPI)
        cls.stub_port = cls.stub.server_address[1]
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()

        cls.tmp = tempfile.mkdtemp()
        cfg = agent_mod.Config(os.path.join(cls.tmp, "config.json"))
        cfg.set("token", "test-token-123")
        cfg.set("node_control", "none")
        cfg.set("octa_api_key", "fake-key-for-test")
        cfg.set("octa_api_base", "http://127.0.0.1:%d" % cls.stub_port)
        state = agent_mod.AgentState(cfg, demo=False)
        agent_mod.Handler.state = state
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), agent_mod.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.stub.shutdown()
        cls.stub.server_close()

    def test_price_applied_via_official_api_shape(self):
        status, body = call("POST", self.base + "/api/price",
                            token="test-token-123",
                            body={"node_id": 42, "base_cents": 60,
                                  "storage_cents": 2, "traffic_cents": 1})
        self.assertEqual(status, 200)
        self.assertTrue(body["applied"])
        patches = [r for r in StubOctaAPI.received if r["method"] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["path"], "/nodes/42/prices")
        self.assertEqual(patches[0]["auth"], "fake-key-for-test")
        self.assertEqual(patches[0]["body"],
                         {"base": 60, "storage": 2, "traffic": 1})

    def test_octa_nodes_proxy(self):
        status, body = call("GET", self.base + "/api/octa/nodes",
                            token="test-token-123")
        self.assertEqual(status, 200)
        self.assertEqual(body["nodes"][0]["id"], 42)

    def test_real_mode_status_no_gpu_honesty(self):
        # This container has no NVIDIA GPU: the agent must say so, not fake it.
        status, body = call("GET", self.base + "/api/status",
                            token="test-token-123")
        self.assertEqual(status, 200)
        s = body["status"]
        self.assertFalse(s["demo"])
        if not s["gpus"]:
            self.assertIn("nvidia", (s["gpu_error"] or "").lower())


if __name__ == "__main__":
    unittest.main()
