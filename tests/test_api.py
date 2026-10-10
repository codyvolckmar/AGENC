#!/usr/bin/env python3
"""End-to-end tests for the AGENCY provisioning API.

Runs the real server with `dry_run: true` (simulated Proxmox) on an
ephemeral port and exercises the entire web flow:
  free home -> auto-provision -> running with an IP
  + paid homes -> USDC/USDT quote -> confirm-payment -> provisioned
  + auth / validation / static site.

Run:  python3 tests/test_api.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

from app import build_server, load_config  # noqa: E402

SITE_SECRET = "test-site-secret"
ADMIN_KEY = "test-admin-key"


def make_config(data_dir: str) -> dict:
    cfg = json.load(open(os.path.join(ROOT, "api", "config.example.json")))
    cfg.update({
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "website_dir": os.path.join(ROOT, "website"),
        "data_dir": data_dir,
        "dry_run": True,
        "site_secret": SITE_SECRET,
        "admin_key": ADMIN_KEY,
        "rate_limit": {"per_ip_per_hour": 1000},
    })
    cfg["container"]["poll_interval_s"] = 0.2
    cfg["billing"]["receive_addresses"] = {
        "USDC": "0x1111111111111111111111111111111111111111",
        "USDT": "0x2222222222222222222222222222222222222222",
    }
    return cfg


class Api:
    def __init__(self, base: str):
        self.base = base

    def _req(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(req, data=data, timeout=10) as resp:
                raw = resp.read().decode()
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, {"raw": raw}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}

    def get(self, path): return self._req("GET", path)
    def post(self, path, body=None, secret=None, admin=None):
        headers = {}
        if secret is not None: headers["X-Agency-Secret"] = secret
        if admin is not None: headers["X-Admin-Key"] = admin
        return self._req("POST", path, body, headers)


class AgencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="agenc-test-")
        cfg = make_config(cls.tmp)
        cls.server = build_server(cfg)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        port = cls.server.server_address[1]
        cls.api = Api(f"http://127.0.0.1:{port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def wait_status(self, agent_id, want, timeout=20):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            _, res = self.api.get(f"/api/agents/{agent_id}")
            last = res
            if res.get("agent", {}).get("status") == want:
                return res["agent"]
            time.sleep(0.3)
        self.fail(f"agent {agent_id} never reached {want!r}; last={last}")

    # ------------------------------------------------------------- tests
    def test_static_site(self):
        code, _ = self.api.get("/")
        self.assertEqual(code, 200)
        code, _ = self.api.get("/css/styles.css")
        self.assertEqual(code, 200)
        code, _ = self.api.get("/js/app.js")
        self.assertEqual(code, 200)

    def test_health(self):
        code, res = self.api.get("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(res["ok"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["free_tier"]["limit"], 1)
        self.assertIsInstance(res["agents"]["alive"], int)
        self.assertGreater(res["cpus"], 0)
        self.assertEqual(res["net_bridge"], "sandbox")
        self.assertTrue(res["net_bridge_ok"])  # dry-run pretends sandbox exists

    def test_free_home_provisions_automatically(self):
        code, res = self.api.post("/api/agents", {
            "hostname": "phoenix-42",
            "email": "dev@example.com",
            "purpose": "urns and corpus",
            "ssh_key": "ssh-ed25519 AAAAC0_test dev@example.com",
        }, secret=SITE_SECRET)
        self.assertEqual(code, 201)
        agent = res["agent"]
        self.assertTrue(agent["free_tier"])
        self.assertIn(agent["status"], ("provisioning", "running"))
        self.assertIsNone(agent.get("quote"))

        agent = self.wait_status(agent["id"], "running")
        self.assertEqual(agent["memory_mb"], 2048)
        self.assertEqual(agent["disk_gb"], 8)
        self.assertTrue(agent["vmid"])
        self.assertRegex(agent["ip"], r"^\d+\.\d+\.\d+\.\d+$")

    def test_duplicate_hostname_rejected(self):
        _, res = self.api.get("/api/agents")
        running = [a for a in res["agents"] if a["status"] == "running"]
        if not running:
            self.skipTest("no running agent from previous test order")
        code, res = self.api.post("/api/agents", {
            "hostname": running[0]["hostname"],
            "email": "x@y.com",
        }, secret=SITE_SECRET)
        self.assertEqual(code, 409)

    def test_auth_enforced(self):
        code, res = self.api.post("/api/agents", {"hostname": "hack-1", "email": "x@y.com"})
        self.assertEqual(code, 401)
        code, _ = self.api.post("/api/agents/no-such-id/confirm-payment", admin="wrong")
        self.assertEqual(code, 403)

    def test_paid_home_gets_stablecoin_quote_and_provisions_on_payment(self):
        code, res = self.api.post("/api/agents", {
            "hostname": "paid-1",
            "email": "money@example.com",
        }, secret=SITE_SECRET)
        self.assertEqual(code, 201)
        agent = res["agent"]
        self.assertEqual(agent["status"], "quote_required")
        self.assertFalse(agent["free_tier"])

        quote = agent["quote"]
        self.assertIn(quote["asset"], ("USDC", "USDT"))
        self.assertEqual(quote["amount"], "5.00")
        self.assertRegex(quote["address"], r"^0x")
        self.assertEqual(quote["reference"], agent["id"])
        self.assertEqual(quote["status"], "pending")

        # payment must be confirmed by an operator/webhook
        code, _ = self.api.post(f"/api/agents/{agent['id']}/confirm-payment")
        self.assertEqual(code, 403)
        code, res = self.api.post(
            f"/api/agents/{agent['id']}/confirm-payment", secret="irrelevant")
        self.assertEqual(code, 403)  # X-Agency-Secret is not the admin key

        code, res = self.api.post(
            f"/api/agents/{agent['id']}/confirm-payment", admin=ADMIN_KEY)
        self.assertEqual(code, 200)
        self.assertEqual(res["agent"]["quote"]["status"], "paid")
        self.assertEqual(res["agent"]["status"], "provisioning")

        agent = self.wait_status(agent["id"], "running")
        self.assertTrue(agent["ip"])
        self.assertEqual(agent["quote"]["status"], "paid")

    def test_validation(self):
        cases = [
            {"hostname": "BAD HOST!", "email": "x@y.com"},
            {"hostname": "ok-1", "email": "not-an-email"},
            {"hostname": "ok-2", "email": "x@y.com",
             "ssh_key": "not-a-key"},
        ]
        for body in cases:
            code, _ = self.api.post("/api/agents", body, secret=SITE_SECRET)
            self.assertEqual(code, 422, body)

    def test_admin_destroy(self):
        _, res = self.api.post("/api/agents", {
            "hostname": "gone-1", "email": "bye@example.com",
        }, secret=SITE_SECRET)
        agent = res["agent"]
        self.wait_status(agent["id"], "running")
        code, res = self.api.post(
            f"/api/agents/{agent['id']}/destroy", admin=ADMIN_KEY)
        self.assertEqual(code, 200)
        self.assertEqual(res["agent"]["status"], "destroyed")

    def test_public_view_never_leaks_ssh_key(self):
        _, res = self.api.get("/api/agents")
        for agent in res["agents"]:
            self.assertNotIn("ssh_key", agent)
            self.assertNotIn("request_ip", agent)

    def test_bad_json_rejected(self):
        import urllib.request as u
        req = urllib.request.Request(
            f"{self.api.base}/api/agents", data=b"{not json", method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Agency-Secret", SITE_SECRET)
        try:
            with u.urlopen(req, timeout=10) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)