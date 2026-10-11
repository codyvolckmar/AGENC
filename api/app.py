#!/usr/bin/env python3
"""AGENCY — agent-hosting website + provisioning API.

One stdlib-only process does both jobs:
  * serves the static website from `website_dir` (landing + request form)
  * exposes the provisioning REST API under `/api`

Deploy on the Proxmox node (see deploy/deploy.sh), port-forward your web
ports to it, and the website can spin up agent homes on demand.

Quick start (local demo):
    cp api/config.example.json api/config.json
    python3 api/app.py          # dry_run is auto-enabled until real creds exist
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxmox import Proxmox, ProxmoxError  # noqa: E402
from provisioning import destroy_home, poll_agent, start_provision  # noqa: E402
from quotes import confirm as confirm_quote  # noqa: E402
from quotes import create_quote  # noqa: E402
from stacks import public_catalog, validate_stack  # noqa: E402
from store import Store  # noqa: E402

log = logging.getLogger("agenc.api")

HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?$")
SSH_KEY_PREFIXES = ("ssh-rsa ", "ssh-ed25519 ", "ecdsa-sha2-", "sk-ssh-", "sk-ecdsa-")
PUBLIC_FIELDS = (
    "id", "hostname", "status", "ip", "vmid", "cores", "memory_mb",
    "disk_gb", "created_at", "updated_at", "email", "purpose",
    "free_tier", "quote", "error", "task", "template", "stack",
    "stack_status",
)

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json")


# ----------------------------------------------------------------- config
def load_config(explicit: str | None = None) -> dict:
    path = explicit or os.environ.get("AGENC_CONFIG") or DEFAULT_CONFIG_PATH
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    else:
        with open(EXAMPLE_CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg["dry_run"] = True
        log.warning(
            "No config.json found at %s — running in DRY-RUN mode with the "
            "example config (no containers will actually be created).",
            path,
        )
    cfg.setdefault("dry_run", False)
    cfg.setdefault("site_secret", "")
    cfg.setdefault("admin_key", "")
    return cfg


# ---------------------------------------------------------------- helpers
# Stable machine-readable hints for the error envelope (see error_json).
ERROR_HINTS = {
    "bad_request": "The request body is not valid JSON, or it exceeds the size limit.",
    "validation": "Fix the listed fields and retry.",
    "auth": "Send a valid X-Agency-Secret header (public writes) or X-Admin-Key (admin actions).",
    "forbidden": "Your key is valid but cannot do this — admin actions need X-Admin-Key.",
    "rate_limit": "Slow down and retry later.",
    "conflict": "Resolve the conflict (e.g. pick another hostname) and retry.",
    "not_found": "No such route or resource — check the path or id.",
    "proxmox": "The Proxmox node refused the operation; fix the node-side cause and retry.",
    "internal": "Unexpected server error — include the request_id when reporting it.",
}

# Tie-breaking order for Accept negotiation (HTML wins ties, as browsers do).
_KIND_RANK = {"text/html": 0, "text/markdown": 1, "application/json": 2}


def public_agent(agent: dict) -> dict:
    return {k: agent.get(k) for k in PUBLIC_FIELDS if k in agent}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def new_request_id() -> str:
    """Short collision-resistant id tagging every HTTP response."""
    return uuid.uuid4().hex[:12]


def validate_request(body: dict) -> list[str]:
    errors = []
    hostname = str(body.get("hostname", "")).strip().lower()
    email = str(body.get("email", "")).strip()
    purpose = str(body.get("purpose", "")).strip()
    ssh_key = str(body.get("ssh_key", "")).strip()

    if not hostname or not HOSTNAME_RE.match(hostname):
        errors.append("hostname must be 2-42 chars: lowercase letters, digits, dashes")
    if not email or "@" not in email or len(email) > 254:
        errors.append("a valid email is required (for the quote / delivery note)")
    if purpose and len(purpose) > 500:
        errors.append("purpose must be under 500 characters")
    if ssh_key and not ssh_key.startswith(SSH_KEY_PREFIXES):
        errors.append("ssh_key must be an OpenSSH public key (ssh-ed25519/ssh-rsa/ecdsa…)")
    meta = body.get("meta")
    if meta is not None and not isinstance(meta, dict):
        errors.append("meta must be an object")
    return errors


# ------------------------------------------------------------------ state
def build_state(cfg: dict) -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    data_dir = cfg.get("data_dir") or os.path.join(here, "data")
    website_dir = os.path.realpath(
        cfg.get("website_dir") and os.path.join(here, cfg["website_dir"])
        or os.path.join(os.path.dirname(here), "website")
    )
    store = Store(os.path.join(data_dir, "agents.json"))
    pve = Proxmox(cfg["proxmox"], dry_run=cfg["dry_run"],
                  log=lambda m: log.info("%s", m))
    return {
        "cfg": cfg,
        "store": store,
        "pve": pve,
        "website_dir": website_dir,
        "hits": {},
        "lock": threading.Lock(),
        "started_at": int(time.time()),
        "warned_no_site_secret": False,
    }


# ------------------------------------------------------------- watchdog
def watchdog_loop(state: dict, interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        try:
            store, cfg, pve = state["store"], state["cfg"], state["pve"]
            for agent in store.all():
                if agent.get("status") == "provisioning":
                    try:
                        poll_agent(store, cfg, pve, agent)
                    except ProxmoxError as exc:
                        log.warning("poll failed for %s: %s", agent["id"], exc)
        except Exception:  # never kill the watchdog
            log.exception("watchdog iteration failed")


# ---------------------------------------------------------------- server
class AgencyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class AgencyHandler(BaseHTTPRequestHandler):
    server_version = "AGENCY/0.3"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        # Set before super() — on CPython >= 3.14 socketserver invokes
        # handle() from within BaseRequestHandler.__init__, i.e. while this
        # constructor is still on the stack.
        self.request_id = new_request_id()
        self._head = False
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------ state
    @property
    def state(self) -> dict:
        return self.server.state  # type: ignore[attr-defined]

    # ------------------------------------------------------------ utils
    def _headers(self, no_cache: bool = True) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Agency-Secret, X-Admin-Key")
        self.send_header("X-Request-Id", self.request_id)
        if no_cache:
            self.send_header("Cache-Control", "no-store")

    def _negotiated_kind(self) -> str:
        """Pick the response format this client asked for.

        Explicit Accept q-values among text/html, text/markdown and
        application/json win (top q, ties in that order). With no stated
        preference (no header, or only `*/*`) the User-Agent decides:
        browsers get HTML, agents get markdown. The JSON API itself never
        negotiates.
        """
        scores = {"text/html": 0.0, "text/markdown": 0.0, "application/json": 0.0}
        saw_explicit = False
        for token in (self.headers.get("Accept") or "").split(","):
            parts = [p.strip() for p in token.split(";")]
            name = parts[0].lower()
            q = 1.0
            for attr in parts[1:]:
                if attr.startswith("q="):
                    try:
                        q = float(attr[2:])
                    except ValueError:
                        q = 0.0
            if name in scores and q > 0:
                scores[name] = max(scores[name], q)
                saw_explicit = True
        if saw_explicit:
            return max(scores, key=lambda k: (scores[k], -_KIND_RANK[k]))
        ua = (self.headers.get("User-Agent") or "")
        return "text/html" if "Mozilla" in ua else "text/markdown"

    def send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self._headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    def error_json(self, code: int, message: str, kind: str = "error",
                    hint: str | None = None) -> None:
        self.send_json(code, {"ok": False, "error": {
            "code": kind,
            "kind": kind,  # legacy alias — old clients keep working
            "message": message,
            "hint": hint if hint is not None else ERROR_HINTS.get(kind, ""),
            "request_id": self.request_id,
        }})

    def read_body(self, max_bytes: int = 32 * 1024) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return {}
        if length > max_bytes:
            return None
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    # ---------------------------------------------- security / limiting
    def _site_secret_ok(self) -> bool:
        expected = self.state["cfg"].get("site_secret")
        if not expected:
            state = self.state
            if not state["warned_no_site_secret"]:
                state["warned_no_site_secret"] = True
                log.warning(
                    "site_secret is empty — provisioning API is open. "
                    "Set a secret in config.json before going live."
                )
            return True
        provided = self.headers.get("X-Agency-Secret") or ""
        return hmac.compare_digest(provided, expected)

    def _admin_ok(self) -> bool:
        expected = self.state["cfg"].get("admin_key")
        if not expected:
            log.warning("admin_key is empty — admin endpoints are open. Set it in config.json.")
            return True
        provided = self.headers.get("X-Admin-Key") or ""
        return hmac.compare_digest(provided, expected)

    def _rate_ok(self) -> bool:
        limit = self.state["cfg"].get("rate_limit", {}).get("per_ip_per_hour", 10)
        ip = self.client_address[0]
        now = time.time()
        with self.state["lock"]:
            hits = [t for t in self.state["hits"].get(ip, []) if now - t < 3600]
            if len(hits) >= limit:
                self.state["hits"][ip] = hits
                return False
            hits.append(now)
            self.state["hits"][ip] = hits
        return True

    # ------------------------------------------------------------ routes
    def _route(self, method: str, path: str):
        if path.startswith("/api/"):
            return self._api(method, path)
        return self._static(path)

    def _api(self, method: str, path: str):
        cfg = self.state["cfg"]
        store = self.state["store"]
        pve = self.state["pve"]
        if method == "HEAD":
            method = "GET"  # reads answer HEAD with their headers, no body
        parts = [p for p in path.split("/") if p]  # ['api', 'agents', '<id>', ...]

        try:
            # ---- app-stack catalog (public, feeds the website menu)
            if method == "GET" and parts == ["api", "stacks"]:
                cat = public_catalog()
                cat["max_groups"] = int(cfg.get("request", {}).get("max_stack_groups", 6))
                return self.send_json(200, {"ok": True, **cat})

            # ---- health
            if method == "GET" and parts == ["api", "health"]:
                bridge = cfg["container"].get("net_bridge", "sandbox")
                px_info = {"reachable": False, "auth_ok": None, "error": None}
                cpus = 0
                mem = {}
                try:
                    node = pve.node_status()
                    px_info.update({"reachable": True, "auth_ok": True})
                    cpus = (node.get("cpuinfo") or {}).get("cpus", 0)
                    mem = node.get("memory", {})
                except ProxmoxError as exc:
                    px_info["error"] = str(exc)
                try:
                    templates = len(pve.list_templates())
                except ProxmoxError:
                    templates = None
                return self.send_json(200, {
                    "ok": True,
                    "dry_run": cfg["dry_run"],
                    "node": pve.node,
                    "proxmox": px_info,
                    "templates_available": templates,
                    "node_status": "online" if px_info["reachable"] else "unreachable",
                    "cpus": cpus,
                    "mem_total_gb": round((mem.get("total") or 0) / 1073741824, 1),
                    "mem_used_gb": round((mem.get("used") or 0) / 1073741824, 1),
                    "net_bridge": bridge,
                    "net_bridge_ok": pve.net_exists(bridge),
                    "rootfs_storage": cfg["proxmox"].get("rootfs_storage"),
                    "agents": {
                        "alive": store.alive_count(),
                        "total": len(store.all()),
                    },
                    "free_tier": {
                        "used": store.free_tier_used(),
                        "limit": int(cfg["billing"].get("free_tier_containers", 1)),
                        "unlimited": int(cfg["billing"].get("free_tier_containers", 1)) == 0,
                    },
                    "uptime_s": int(time.time()) - self.state["started_at"],
                })

            # ---- create agent home
            if method == "POST" and parts == ["api", "agents"]:
                if not self._site_secret_ok():
                    return self.error_json(401, "bad or missing X-Agency-Secret", "auth")
                if not self._rate_ok():
                    return self.error_json(429, "too many requests from this IP", "rate_limit")
                body = self.read_body()
                if body is None:
                    return self.error_json(400, "invalid JSON body (must be an object, < 32KB)", "bad_request")
                errors = validate_request(body)
                if errors:
                    return self.error_json(422, "; ".join(errors), "validation")

                hostname = str(body["hostname"]).strip().lower()
                if store.hostname_taken(hostname):
                    return self.error_json(409, f"hostname '{hostname}' is already in use", "conflict")

                billing = cfg["billing"]
                free_limit = int(billing.get("free_tier_containers", 1))
                # 0 = unlimited free homes (billing disabled / dev mode)
                free_tier = free_limit == 0 or store.free_tier_used() < free_limit
                agent_id = new_id("ag")
                # customer-curated app stack (validated against the manifest)
                stack_raw = body.get("stack", [])
                max_groups = int(cfg.get("request", {}).get("max_stack_groups", 6))
                stack, stack_err = validate_stack(stack_raw, max_groups)
                if stack_err:
                    return self.error_json(422, stack_err, "validation")

                agent = {
                    "id": agent_id,
                    "hostname": hostname,
                    "email": str(body.get("email", "")).strip(),
                    "purpose": str(body.get("purpose", "")).strip(),
                    "ssh_key": str(body.get("ssh_key", "")).strip() or None,
                    "meta": body.get("meta"),
                    "stack": stack or None,
                    "stack_status": None,
                    "memory_mb": cfg["container"]["memory_mb"],
                    "disk_gb": cfg["container"]["disk_gb"],
                    "free_tier": free_tier,
                    "created_at": int(time.time()),
                    "request_ip": self.client_address[0],
                }

                if free_tier:
                    agent["status"] = "provisioning"
                else:
                    agent["status"] = "quote_required"
                    agent["quote"] = create_quote(agent_id, billing)

                store.add(agent)

                if free_tier:
                    try:
                        start_provision(store, cfg, pve, agent)
                    except ProxmoxError as exc:
                        return self.send_json(502, {"ok": True, "created": True,
                                                     "agent": public_agent(store.get(agent_id)),
                                                     "warning": f"creation failed on node: {exc}"})
                log.info("agent requested: %s free_tier=%s status=%s stack=%s",
                         hostname, free_tier, agent["status"], agent.get("stack") or [])
                return self.send_json(201, {"ok": True, "created": True,
                                            "agent": public_agent(store.get(agent_id))})

            # ---- list agents
            if method == "GET" and parts == ["api", "agents"]:
                items = [public_agent(a) for a in store.all()]
                return self.send_json(200, {"ok": True, "agents": items})

            # ---- get one agent
            if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "agents"]:
                agent = store.get(parts[2])
                if not agent:
                    return self.error_json(404, "no such agent", "not_found")
                return self.send_json(200, {"ok": True, "agent": public_agent(agent)})

            # ---- confirm stablecoin payment (admin/webhook)
            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "agents"] \
                    and parts[3] == "confirm-payment":
                if not self._admin_ok():
                    return self.error_json(403, "bad or missing X-Admin-Key", "forbidden")
                agent = store.get(parts[2])
                if not agent:
                    return self.error_json(404, "no such agent", "not_found")
                if agent.get("status") != "quote_required":
                    return self.error_json(409, "agent is not awaiting payment", "conflict")
                agent["quote"] = confirm_quote(agent["quote"], agent["id"])
                store.update(agent["id"], status="provisioning")
                start_provision(store, cfg, pve, agent)
                log.info("payment confirmed for %s — provisioning", agent["id"])
                return self.send_json(200, {"ok": True, "agent": public_agent(store.get(agent["id"]))})

            # ---- retry a failed provisioning (admin)
            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "agents"] \
                    and parts[3] == "retry":
                if not self._admin_ok():
                    return self.error_json(403, "bad or missing X-Admin-Key", "forbidden")
                agent = store.get(parts[2])
                if not agent:
                    return self.error_json(404, "no such agent", "not_found")
                if agent.get("status") != "error":
                    return self.error_json(409, "agent is not in error state", "conflict")
                store.update(agent["id"], status="provisioning", error=None)
                try:
                    start_provision(store, cfg, pve, agent)
                except ProxmoxError as exc:
                    return self.send_json(502, {"ok": False, "agent": public_agent(store.get(agent["id"])),
                                                 "error": {"kind": "proxmox", "message": str(exc)}})
                return self.send_json(200, {"ok": True, "agent": public_agent(store.get(agent["id"]))})

            # ---- destroy agent home (admin)
            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "agents"] \
                    and parts[3] == "destroy":
                if not self._admin_ok():
                    return self.error_json(403, "bad or missing X-Admin-Key", "forbidden")
                agent = store.get(parts[2])
                if not agent:
                    return self.error_json(404, "no such agent", "not_found")
                try:
                    destroy_home(store, cfg, pve, agent)
                except ProxmoxError as exc:
                    return self.error_json(502, str(exc), "proxmox")
                log.info("agent %s destroyed", agent["id"])
                return self.send_json(200, {"ok": True, "agent": public_agent(store.get(agent["id"]))})

        except ProxmoxError as exc:
            return self.error_json(502, str(exc), "proxmox")
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error on %s %s", method, path)
            return self.error_json(500, f"internal error: {exc}", "internal")

        return self.error_json(404, "unknown api route", "not_found")

    # ----------------------------------------------------------- static
    # Canonical agent reference — the markdown twin of the landing page
    # and the body of `/llms-full.txt`. Only one copy of the docs exists.
    MARKDOWN_TWIN = "docs/agents.md"

    def _static(self, path: str):
        rel = unquote(urlparse(path).path.lstrip("/") or "index.html")
        kind = self._negotiated_kind()

        # The whole agent reference in one fetch: index + this page.
        if rel == "llms-full.txt":
            return self._send_llms_full()

        # The docs page: raw markdown, or a markdown answer for agents.
        if rel == "docs/agents" or (rel == "docs" and kind == "text/markdown"):
            return self._send_file(self.MARKDOWN_TWIN, "text/markdown; charset=utf-8")

        # The landing page answers in markdown when asked (or as /index.md).
        if rel == "index.md" or (rel == "index.html" and kind == "text/markdown"):
            return self._send_file(self.MARKDOWN_TWIN, "text/markdown; charset=utf-8")

        # Everything else: real files from the website directory.
        root = self.state["website_dir"]
        candidate = os.path.realpath(os.path.join(root, rel))
        if candidate != root and not candidate.startswith(root + os.sep):
            return self._send_error_page(403, "forbidden", "forbidden")
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "index.html")
        if not os.path.isfile(candidate):
            return self._send_error_page(
                404, f"no page at `/{rel}`", "not_found")
        ctype = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        if candidate.endswith(".md"):
            ctype = "text/markdown; charset=utf-8"
        with open(candidate, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self._headers(no_cache=False)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    def _send_file(self, rel: str, ctype: str) -> None:
        """Serve one file from the website directory, traversal-guarded."""
        root = os.path.realpath(self.state["website_dir"])
        candidate = os.path.realpath(os.path.join(root, rel))
        if candidate != root and not candidate.startswith(root + os.sep):
            return self._send_error_page(403, "forbidden", "forbidden")
        if not os.path.isfile(candidate):
            return self._send_error_page(404, f"no page at `/{rel}`", "not_found")
        with open(candidate, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self._headers(no_cache=False)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    def _send_llms_full(self) -> None:
        """Assemble /llms-full.txt from the index + the agent reference.

        Assembled at request time so the single-fetch contract never drifts
        from the pages it documents.
        """
        root = self.state["website_dir"]
        parts = []
        for rel in ("llms.txt", self.MARKDOWN_TWIN):
            with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
                parts.append(fh.read().rstrip())
        body = ("\n\n---\n\n".join(parts) + "\n").encode("utf-8")
        self.send_response(200)
        self._headers(no_cache=False)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    def _send_error_page(self, code: int, message: str, kind: str = "not_found") -> None:
        """404/403 for non-API paths, answered in the format the client wants."""
        best = self._negotiated_kind()
        if best == "application/json":
            return self.error_json(code, message, kind)
        ctype = {
            "text/markdown": "text/markdown; charset=utf-8",
            "text/html": "text/html; charset=utf-8",
        }[best]
        if best == "text/markdown":
            body = (
                f"# {code}: {message}\n\n"
                f"{self.headers.get('Host', 'this node')} has no page here. "
                "Where to look instead:\n\n"
                "- Home (markdown for agents): /index.md\n"
                "- Agent reference: /docs/agents.md\n"
                "- Agent index: /llms.txt\n"
                "- Everything in one fetch: /llms-full.txt\n"
                "- API (JSON): /api/health\n\n"
                f"Request id: {self.request_id}\n"
            ).encode("utf-8")
        else:
            body = (
                "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
                f"<title>{code}</title></head><body>"
                f"<h1>{code}</h1><p>{message}</p>"
                "<p>Try <a href=\"/\">the site</a>, "
                "<a href=\"/docs/agents.md\">the agent reference</a>, "
                "or <a href=\"/llms.txt\">/llms.txt</a>.</p>"
                "</body></html>"
            ).encode("utf-8")
        self.send_response(code)
        self._headers()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    # ------------------------------------------------------------ verbs
    def do_HEAD(self):
        """HEAD answers with GET's headers and no body — an agent probe."""
        self._head = True
        self._route("HEAD", urlparse(self.path).path)
    def do_GET(self):
        self._route("GET", urlparse(self.path).path)

    def do_POST(self):
        self._route("POST", urlparse(self.path).path)

    def do_DELETE(self):
        self._route("DELETE", urlparse(self.path).path)

    def do_OPTIONS(self):
        self.send_response(204)
        self._headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args):  # quieter access log
        log.info("%s - %s", self.client_address[0], fmt % args)


# ---------------------------------------------------------------- entry
def build_server(cfg: dict) -> AgencyServer:
    state = build_state(cfg)
    server = AgencyServer((cfg.get("listen_host", "0.0.0.0"), int(cfg.get("listen_port", 8080))),
                          AgencyHandler)
    server.state = state  # type: ignore[attr-defined]
    stop = threading.Event()
    server.agenc_stop = stop  # type: ignore[attr-defined]
    interval = max(0.2, float(cfg.get("container", {}).get("poll_interval_s", 5)))
    thread = threading.Thread(target=watchdog_loop, args=(state, interval, stop), daemon=True)
    thread.name = "agenc-watchdog"
    thread.start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AGENCY agent-hosting API")
    parser.add_argument("config", nargs="?", default=None, help="path to config.json (default: api/config.json)")
    parser.add_argument("--port", type=int, default=None, help="override listen port")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )
    cfg = load_config(args.config)
    if args.port:
        cfg["listen_port"] = args.port

    server = build_server(cfg)
    host, port = server.server_address
    log.info(
        "AGENCY online — website+API on http://%s:%d  (dry_run=%s node=%s)",
        host, port, cfg["dry_run"], cfg["proxmox"]["node"],
    )
    if cfg["dry_run"]:
        log.warning("DRY-RUN MODE: no real containers will be created.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())