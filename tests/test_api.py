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

    def raw(self, method, path, body=None, headers=None):
        """Fetch with arbitrary headers; return (status, headers, body)."""
        req = urllib.request.Request(self.base + path, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(req, data=data, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode()


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
        self.assertTrue(res["proxmox"]["reachable"])
        self.assertTrue(res["proxmox"]["auth_ok"])
        self.assertGreater(res["templates_available"], 0)
        self.assertEqual(res["free_tier"]["limit"], 1)
        self.assertIsInstance(res["agents"]["alive"], int)
        self.assertGreater(res["cpus"], 0)
        self.assertEqual(res["net_bridge"], "sandbox")
        self.assertTrue(res["net_bridge_ok"])  # dry-run pretends sandbox exists
        self.assertEqual(res["rootfs_storage"], "ssd120")

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
        self.assertEqual(agent["memory_mb"], 8192)   # standard plan
        self.assertEqual(agent["disk_gb"], 64)
        self.assertEqual(agent["plan"], "standard")
        self.assertEqual(agent["price_usd_per_month"], 32.0)
        self.assertEqual(agent["billing_period"], "monthly")
        self.assertEqual(agent["period_days"], 30)
        self.assertEqual(agent["periods_bought"], 1)   # first period free
        self.assertEqual(agent["billed_total_usd"], 0.0)
        self.assertGreater(agent["remaining_s"], 28 * 86400)
        self.assertLessEqual(agent["remaining_s"], 30 * 86400)
        self.assertFalse(agent["expiring"])
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
        self.assertEqual(quote["amount"], "32.00")  # standard $32/mo, monthly period
        self.assertEqual(quote["plan"], "standard")
        self.assertEqual(quote["period"], "monthly")
        self.assertEqual(quote["days"], 30)
        self.assertRegex(quote["address"], r"^0x")
        self.assertEqual(quote["reference"], agent["id"])
        self.assertEqual(quote["status"], "pending")
        self.assertEqual(agent["plan"], "standard")
        self.assertEqual(agent["price_usd_per_month"], 32.0)
        self.assertEqual(agent["billing_period"], "monthly")
        self.assertIsNone(agent.get("paid_until"))   # nothing prepaid yet

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
        self.assertAlmostEqual(res["agent"]["billed_total_usd"], 32.0, places=2)
        self.assertEqual(res["agent"]["periods_bought"], 1)
        self.assertIsNotNone(res["agent"]["paid_until"])
        self.assertGreater(res["agent"]["remaining_s"], 29 * 86400)

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

    def test_admin_retry(self):
        # simulate a failed provisioning record (real errors now get recorded)
        store = self.server.state["store"]
        agent = {
            "id": "ag_retry012345", "hostname": "retry-1", "email": "r@example.com",
            "memory_mb": 2048, "disk_gb": 8,
            "created_at": 0, "updated_at": 0,
            "free_tier": False,  # don't consume the free slot for other tests
            "status": "error", "error": "provision failed: simulated",
        }
        store.add(agent)
        code, _ = self.api.post(f"/api/agents/{agent['id']}/retry")
        self.assertEqual(code, 403)  # admin only
        code, res = self.api.post(f"/api/agents/{agent['id']}/retry", admin=ADMIN_KEY)
        self.assertEqual(code, 200)
        self.assertEqual(res["agent"]["status"], "provisioning")
        self.assertIsNone(res["agent"].get("error"))
        self.wait_status(agent["id"], "running")

    def test_unlimited_free_tier_mode(self):
        """free_tier_containers: 0 = billing off — every home provisions instantly."""
        with tempfile.TemporaryDirectory(prefix="agenc-free-") as d:
            cfg = make_config(d)
            cfg["billing"]["free_tier_containers"] = 0
            srv = build_server(cfg)
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            api = Api(f"http://127.0.0.1:{srv.server_address[1]}")
            try:
                code, res = api.post("/api/agents", {
                    "hostname": "free-forever-1", "email": "f@example.com",
                }, secret=SITE_SECRET)
                self.assertEqual(code, 201)
                self.assertTrue(res["agent"]["free_tier"])
                self.assertIn(res["agent"]["status"], ("provisioning", "running"))
                self.assertIsNone(res["agent"].get("quote"))
                code, health = api.get("/api/health")
                self.assertEqual(code, 200)
                self.assertTrue(health["free_tier"]["unlimited"])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_stack_catalog(self):
        code, res = self.api.get("/api/stacks")
        self.assertEqual(code, 200)
        self.assertTrue(res["ok"])
        self.assertGreater(len(res["selectable"]), 0)
        self.assertGreater(len(res["baked"]), 0)
        self.assertEqual(res["max_groups"], 6)
        selectable = [g["id"] for g in res["selectable"]]
        self.assertIn("databases", selectable)
        self.assertNotIn("base", selectable)  # baked groups are not tickable
        self.assertEqual(res["venv"]["id"], "agent-python")

    def test_stack_validation(self):
        code, res = self.api.post("/api/agents", {
            "hostname": "stackbad-1", "email": "s@e.c", "stack": ["not-a-group"],
        }, secret=SITE_SECRET)
        self.assertEqual(code, 422)
        code, _ = self.api.post("/api/agents", {
            "hostname": "stackbad-2", "email": "s@e.c",
            "stack": ["databases", "net-scan", "containers", "monitor", "media", "x11", "headless"],
        }, secret=SITE_SECRET)
        self.assertEqual(code, 422)

    def test_stack_provisions_and_installs(self):
        """Customer-curated stack persists and the home reaches running (dry-run)."""
        with tempfile.TemporaryDirectory(prefix="agenc-stack-") as d:
            cfg = make_config(d)
            cfg["billing"]["free_tier_containers"] = 0
            srv = build_server(cfg)
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            api = Api(f"http://127.0.0.1:{srv.server_address[1]}")
            try:
                code, res = api.post("/api/agents", {
                    "hostname": "stacky-1", "email": "s@e.c",
                    "stack": ["databases", "net-scan"],
                }, secret=SITE_SECRET)
                self.assertEqual(code, 201)
                agent = res["agent"]
                self.assertEqual(sorted(agent["stack"]), ["databases", "net-scan"])
                self.assertEqual(agent["status"], "provisioning")
                # wait for running (stack install completes instantly in dry-run)
                deadline = time.time() + 20
                last = None
                while time.time() < deadline:
                    _, last = api.get(f"/api/agents/{agent['id']}")
                    if last["agent"]["status"] == "running":
                        break
                    time.sleep(0.3)
                self.assertEqual(last["agent"]["status"], "running")
                self.assertEqual(last["agent"]["stack_status"], "done")
            finally:
                srv.shutdown()
                srv.server_close()

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

    # ------------------------------------------------ agent-native surfaces
    @staticmethod
    def _hdr(hdrs):
        return {k.lower(): v for k, v in hdrs.items()}

    def test_llms_index_and_full(self):
        code, hdrs, body = self.api.raw("GET", "/llms.txt")
        self.assertEqual(code, 200)
        self.assertIn("text/plain", self._hdr(hdrs)["content-type"])
        self.assertIn("# AGENCY", body)
        self.assertIn("/llms-full.txt", body)

        code, hdrs, full = self.api.raw("GET", "/llms-full.txt")
        self.assertEqual(code, 200)
        self.assertIn("text/plain", self._hdr(hdrs)["content-type"])
        self.assertGreater(len(full), len(body))       # index + reference
        self.assertIn("X-Agency-Secret", full)         # reference section
        self.assertIn("request_id", full)              # error-table section

    def test_markdown_twin_pages(self):
        for path in ("/index.md", "/docs/agents.md", "/docs/agents"):
            code, hdrs, body = self.api.raw("GET", path)
            self.assertEqual(code, 200, path)
            self.assertIn("text/markdown", self._hdr(hdrs)["content-type"], path)
            self.assertIn("X-Agency-Secret", body, path)
        # browsers still get the human docs page at /docs
        code, hdrs, body = self.api.raw("GET", "/docs",
                                        headers={"User-Agent": "Mozilla/5.0"})
        self.assertEqual(code, 200)
        self.assertIn("text/html", self._hdr(hdrs)["content-type"])

    def test_content_negotiation(self):
        # explicit markdown preference -> markdown twin of the landing page
        code, hdrs, body = self.api.raw("GET", "/",
                                        headers={"Accept": "text/markdown"})
        self.assertEqual(code, 200)
        self.assertIn("text/markdown", self._hdr(hdrs)["content-type"])
        self.assertIn("X-Agency-Secret", body)
        # browsers keep the HTML page
        code, hdrs, _ = self.api.raw("GET", "/",
                                     headers={"Accept": "text/html"})
        self.assertEqual(code, 200)
        self.assertIn("text/html", self._hdr(hdrs)["content-type"])
        # a bare wildcard + non-browser UA falls back to markdown
        code, hdrs, body = self.api.raw("GET", "/",
                                        headers={"Accept": "*/*",
                                                 "User-Agent": "curl/8.7"})
        self.assertEqual(code, 200)
        self.assertIn("text/markdown", self._hdr(hdrs)["content-type"])
        self.assertIn("X-Agency-Secret", body)
        # the JSON API never negotiates
        code, hdrs, _ = self.api.raw("GET", "/api/health",
                                     headers={"Accept": "text/markdown"})
        self.assertEqual(code, 200)
        self.assertIn("application/json", self._hdr(hdrs)["content-type"])

    def test_error_envelope_and_request_id(self):
        code, res = self.api.post("/api/agents", {
            "hostname": "BAD HOST!", "email": "x@y.com"}, secret=SITE_SECRET)
        self.assertEqual(code, 422)
        err = res["error"]
        self.assertEqual(err["code"], "validation")
        self.assertEqual(err["kind"], "validation")   # legacy alias
        self.assertTrue(err["hint"])
        self.assertRegex(err["request_id"], r"^[0-9a-f]{12}$")
        # success responses carry the request id header too
        code, hdrs, _ = self.api.raw("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertRegex(self._hdr(hdrs).get("x-request-id", ""),
                         r"^[0-9a-f]{12}$")

    def test_head_probe(self):
        """HEAD answers with the GET headers and no body — agent probes."""
        code, hdrs, body = self.api.raw("HEAD", "/index.md")
        self.assertEqual(code, 200)
        self.assertIn("text/markdown", self._hdr(hdrs)["content-type"])
        self.assertEqual(body, "")
        code, hdrs, body = self.api.raw("HEAD", "/api/health")
        self.assertEqual(code, 200)
        self.assertIn("application/json", self._hdr(hdrs)["content-type"])
        self.assertEqual(body, "")
        code, hdrs, _ = self.api.raw("HEAD", "/nope")
        self.assertEqual(code, 404)

    def test_error_page_negotiation(self):
        # JSON-asking client gets the JSON envelope on a bad path
        code, hdrs, body = self.api.raw(
            "GET", "/no/such/page", headers={"Accept": "application/json"})
        self.assertEqual(code, 404)
        self.assertIn("application/json", self._hdr(hdrs)["content-type"])
        data = json.loads(body)
        self.assertEqual(data["error"]["code"], "not_found")
        self.assertIn("request_id", data["error"])
        # markdown-asking client gets a markdown 404 with the request id
        code, hdrs, body = self.api.raw(
            "GET", "/no/such/page", headers={"Accept": "text/markdown"})
        self.assertEqual(code, 404)
        self.assertIn("text/markdown", self._hdr(hdrs)["content-type"])
        self.assertIn("# 404", body)
        self.assertIn("Request id:", body)
        # bare curl (no Accept, no browser UA) gets the markdown 404
        code, _, body = self.api.raw("GET", "/no/such/page")
        self.assertEqual(code, 404)
        self.assertIn("# 404", body)
        # a browser gets an HTML 404
        code, _, body = self.api.raw(
            "GET", "/no/such/page", headers={"User-Agent": "Mozilla/5.0"})
        self.assertEqual(code, 404)
        self.assertIn("<!DOCTYPE html>", body)


    # ------------------------------- phase 2: metered billing + lifecycle
    @staticmethod
    def _spin_server(cfg):
        srv = build_server(cfg)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        return srv, Api(f"http://127.0.0.1:{srv.server_address[1]}")

    @staticmethod
    def _wait_status(api, agent_id, want, timeout=15):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            _, last = api.get(f"/api/agents/{agent_id}")
            if last["agent"]["status"] == want:
                return last["agent"]
            time.sleep(0.3)
        raise AssertionError(f"agent {agent_id} never reached {want!r}; last={last}")

    def test_plans_catalog(self):
        code, res = self.api.get("/api/plans")
        self.assertEqual(code, 200)
        self.assertTrue(res["ok"])
        self.assertEqual(res["default_plan"], "standard")
        self.assertEqual(res["default_period"], "monthly")
        self.assertEqual(res["free_periods"], 1)
        self.assertEqual(list(res["periods"].keys()),
                         ["weekly", "fortnightly", "monthly"])
        self.assertEqual(res["periods"]["monthly"]["days"], 30)
        ids = [p["id"] for p in res["plans"]]
        self.assertEqual(ids, ["micro", "nano", "standard"])  # sorted by $/mo
        micro = next(p for p in res["plans"] if p["id"] == "micro")
        self.assertEqual(micro["memory_mb"], 1024)
        self.assertEqual(micro["disk_gb"], 30)
        self.assertEqual(micro["cores"], 1)
        self.assertEqual(micro["price_usd_per_month"], 8.0)
        self.assertEqual(micro["price_usd_per_hour_equiv"], 0.011)
        self.assertEqual(micro["period_prices"],
                         {"weekly": 2.0, "fortnightly": 4.0, "monthly": 8.0})
        nano = next(p for p in res["plans"] if p["id"] == "nano")
        self.assertEqual((nano["memory_mb"], nano["disk_gb"], nano["cores"]),
                         (4096, 50, 4))
        standard = next(p for p in res["plans"] if p["id"] == "standard")
        self.assertEqual(standard["memory_mb"], 8192)
        self.assertEqual(standard["period_prices"]["monthly"], 32.0)

    def test_plan_and_period_validation(self):
        # shared server's free slot is consumed by now -> quote_required, but
        # the plan/period specs land on the record and drive the quote amount
        code, res = self.api.post("/api/agents", {
            "hostname": "plan-micro-1", "email": "m@e.c",
            "plan": "micro", "period": "weekly",
        }, secret=SITE_SECRET)
        self.assertEqual(code, 201)
        agent = res["agent"]
        self.assertEqual(agent["plan"], "micro")
        self.assertEqual(agent["memory_mb"], 1024)
        self.assertEqual(agent["disk_gb"], 30)
        self.assertEqual(agent["price_usd_per_month"], 8.0)
        self.assertEqual(agent["billing_period"], "weekly")
        self.assertEqual(agent["period_days"], 7)
        self.assertEqual(agent["period_price_usd"], 2.0)
        self.assertEqual(agent["status"], "quote_required")
        self.assertEqual(agent["quote"]["plan"], "micro")
        self.assertEqual(agent["quote"]["period"], "weekly")
        self.assertEqual(agent["quote"]["amount"], "2.00")
        # unknown plan / unknown period are rejected with a hint
        code, res = self.api.post("/api/agents", {
            "hostname": "plan-bad-1", "email": "m@e.c", "plan": "jumbo",
        }, secret=SITE_SECRET)
        self.assertEqual(code, 422)
        self.assertIn("unknown plan", res["error"]["message"])
        code, res = self.api.post("/api/agents", {
            "hostname": "period-bad-1", "email": "m@e.c", "period": "yearly",
        }, secret=SITE_SECRET)
        self.assertEqual(code, 422)
        self.assertIn("unknown billing period", res["error"]["message"])

    def test_period_billing_expiry_halt_renew_statement(self):
        with tempfile.TemporaryDirectory(prefix="agenc-bill-") as d:
            cfg = make_config(d)
            cfg["billing"]["free_tier_containers"] = 0
            srv, api = self._spin_server(cfg)
            try:
                code, res = api.post("/api/agents", {
                    "hostname": "meter-1", "email": "m@e.c",
                }, secret=SITE_SECRET)
                self.assertEqual(code, 201)
                agent = res["agent"]
                self.assertTrue(agent["free_tier"])
                self.assertEqual(agent["plan"], "standard")
                self.assertEqual(agent["billing_period"], "monthly")
                # the first period is free: a ~30-day prepaid window
                a = self._wait_status(api, agent["id"], "running")
                self.assertGreater(a["remaining_s"], 28 * 86400)
                self.assertLessEqual(a["remaining_s"], 30 * 86400)
                self.assertEqual(a["periods_bought"], 1)
                self.assertEqual(a["billed_total_usd"], 0.0)
                self.assertFalse(a["expiring"])
                # push paid_until into the warn window -> expiring flag
                srv.state["store"].update(agent["id"],
                                           paid_until=time.time() + 100)
                deadline = time.time() + 10
                while time.time() < deadline:
                    _, res = api.get(f"/api/agents/{agent['id']}")
                    if res["agent"].get("expiring"):
                        break
                    time.sleep(0.3)
                self.assertTrue(res["agent"]["expiring"])
                # push it into the past -> the watchdog halts the home
                srv.state["store"].update(agent["id"],
                                           paid_until=time.time() - 10)
                halted = self._wait_status(api, agent["id"], "halted")
                self.assertLess(halted["remaining_s"], 0)
                self.assertFalse(halted["expiring"])
                # statement shows the prepaid window + purchase ledger
                code, _, body = api.raw(
                    "GET", f"/api/agents/{agent['id']}/statement",
                    headers={"X-Admin-Key": ADMIN_KEY})
                self.assertEqual(code, 200)
                st = json.loads(body)["statement"]
                self.assertEqual(st["plan"], "standard")
                self.assertEqual(st["price_usd_per_month"], 32.0)
                self.assertEqual(st["billing_period"], "monthly")
                self.assertEqual(st["status"], "halted")
                self.assertGreater(len(st["entries"]), 0)
                self.assertEqual(st["entries"][0]["note"], "free_tier")
                self.assertEqual(st["entries"][0]["amount_usd"], 0.0)
                # renew weekly x2 restarts the home: +14 days at $8/week
                code, res = api.post(f"/api/agents/{agent['id']}/renew",
                                     {"period": "weekly", "periods": 2},
                                     admin=ADMIN_KEY)
                self.assertEqual(code, 200)
                a = res["agent"]
                self.assertEqual(a["status"], "running")
                self.assertGreater(a["remaining_s"], 13 * 86400)
                self.assertLessEqual(a["remaining_s"], 14 * 86400)
                self.assertEqual(a["periods_bought"], 3)       # 1 free + 2
                self.assertAlmostEqual(a["billed_total_usd"], 16.0, places=2)
                # renew by calendar days: 10 days, pro-rated at $32/mo.
                # The window extends contiguously: 14 left + 10 = ~24 days.
                code, res = api.post(f"/api/agents/{agent['id']}/renew",
                                     {"days": 10}, admin=ADMIN_KEY)
                self.assertEqual(code, 200)
                a = res["agent"]
                self.assertGreater(a["remaining_s"], 23 * 86400)
                self.assertLessEqual(a["remaining_s"], 24 * 86400)
                self.assertEqual(a["periods_bought"], 4)
                self.assertAlmostEqual(a["billed_total_usd"], 26.67, places=2)
                # a nonsense renewal is rejected
                code, _ = api.post(f"/api/agents/{agent['id']}/renew",
                                   {"days": 0}, admin=ADMIN_KEY)
                self.assertEqual(code, 422)
            finally:
                srv.shutdown()
                srv.server_close()

    def test_disk_preflight_low_space(self):
        with tempfile.TemporaryDirectory(prefix="agenc-disk-") as d:
            cfg = make_config(d)
            cfg["billing"]["free_tier_containers"] = 0
            srv, api = self._spin_server(cfg)
            try:
                # a nearly-full storage (5 GB free < 8.8 GB needed for standard)
                srv.state["pve"].storage_status = lambda storage: {
                    "storage": storage, "enabled": 1, "total": 10 * 1024 ** 3,
                    "used": 5 * 1024 ** 3, "avail": 5 * 1024 ** 3}
                code, res = api.post("/api/agents", {
                    "hostname": "lowdisk-1", "email": "l@e.c",
                }, secret=SITE_SECRET)
                self.assertEqual(code, 502)
                self.assertIn("low on space", res["warning"])
                # the record survives in error state with the real reason
                _, res = api.get("/api/agents")
                failed = [a for a in res["agents"] if a["hostname"] == "lowdisk-1"]
                self.assertEqual(len(failed), 1)
                self.assertEqual(failed[0]["status"], "error")
                self.assertIn("low on space", failed[0]["error"])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_scheduled_destroy_ttl(self):
        with tempfile.TemporaryDirectory(prefix="agenc-ttl-") as d:
            cfg = make_config(d)
            cfg["billing"]["free_tier_containers"] = 0
            srv, api = self._spin_server(cfg)
            try:
                code, res = api.post("/api/agents", {
                    "hostname": "ttl-1", "email": "t@e.c",
                }, secret=SITE_SECRET)
                self.assertEqual(code, 201)
                agent = res["agent"]
                self._wait_status(api, agent["id"], "running")
                code, res = api.post(f"/api/agents/{agent['id']}/schedule-destroy",
                                     {"ttl_seconds": 2}, admin=ADMIN_KEY)
                self.assertEqual(code, 200)
                self.assertIsNotNone(res["agent"]["destroy_scheduled_at"])
                destroyed = self._wait_status(api, agent["id"], "destroyed")
                self.assertIsNone(destroyed.get("destroy_scheduled_at"))
            finally:
                srv.shutdown()
                srv.server_close()

    def test_scheduled_destroy_cancel_and_past(self):
        with tempfile.TemporaryDirectory(prefix="agenc-ttl2-") as d:
            cfg = make_config(d)
            cfg["billing"]["free_tier_containers"] = 0
            srv, api = self._spin_server(cfg)
            try:
                code, res = api.post("/api/agents", {
                    "hostname": "ttl2-1", "email": "t@e.c",
                }, secret=SITE_SECRET)
                agent = self._wait_status(api, res["agent"]["id"], "running")
                code, res = api.post(f"/api/agents/{agent['id']}/schedule-destroy",
                                     {"destroy_at": int(time.time() + 3600)},
                                     admin=ADMIN_KEY)
                self.assertEqual(code, 200)
                self.assertIsNotNone(res["agent"]["destroy_scheduled_at"])
                # cancel: true clears it
                code, res = api.post(f"/api/agents/{agent['id']}/schedule-destroy",
                                     {"cancel": True}, admin=ADMIN_KEY)
                self.assertEqual(code, 200)
                self.assertIsNone(res["agent"]["destroy_scheduled_at"])
                # a past destroy_at is rejected
                code, _ = api.post(f"/api/agents/{agent['id']}/schedule-destroy",
                                   {"destroy_at": int(time.time()) - 10},
                                   admin=ADMIN_KEY)
                self.assertEqual(code, 422)
                # admin-key only
                code, _ = api.post(f"/api/agents/{agent['id']}/schedule-destroy",
                                   {"ttl_seconds": 60}, secret=SITE_SECRET)
                self.assertEqual(code, 403)
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)