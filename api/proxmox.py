"""Minimal Proxmox VE JSON API client (Python stdlib only).

Authenticates with an API token (`Authorization: PVEAPIToken=<user!id>=<secret>`).
Self-signed certs are accepted by default (normal for a PVE box); set
`verify_tls: true` when a proper CA is installed.

Dry-run mode (`dry_run: true`) simulates a full create -> task -> running
lifecycle locally so the entire stack can be demoed and tested without a
real node. Every method prints what it *would* POST/DELETE.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


class ProxmoxError(Exception):
    """Raised when PVE returns an error or is unreachable."""


def _ssl_context(verify: bool):
    return None if verify else ssl._create_unverified_context()


class Proxmox:
    def __init__(self, pve_cfg: dict, dry_run: bool = False, log=print):
        self.host = str(pve_cfg.get("host", "https://127.0.0.1:8006")).rstrip("/")
        self.base = self.host + "/api2/json"
        self.node = pve_cfg.get("node") or "pve"
        self.template_storage = pve_cfg.get("template_storage") or "local"
        token_id = pve_cfg.get("api_token_id") or ""
        secret = pve_cfg.get("api_token_secret") or ""
        self.auth = f"PVEAPIToken={token_id}={secret}"
        self.verify = bool(pve_cfg.get("verify_tls", False))
        self.dry_run = dry_run
        self.log = log
        self._ctx = _ssl_context(self.verify)
        self._sim_seq: dict[str, int] = {}
        self._sim_counter = [0]

    # ------------------------------------------------------------- http
    def _request(self, method: str, path: str, data: dict | None = None):
        url = self.base + path
        body = None
        if data:
            body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self.auth)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            hint = ""
            if exc.code == 401:
                hint = (" (unauthorized — check proxmox.api_token_id / api_token_secret "
                        "in config.json, or re-run proxmox/setup.sh + deploy to refresh)")
            raise ProxmoxError(f"PVE {method} {path} -> HTTP {exc.code}: {detail}{hint}") from exc
        except urllib.error.URLError as exc:
            raise ProxmoxError(f"PVE unreachable at {self.base}: {exc.reason}") from exc
        return payload.get("data") if isinstance(payload, dict) else payload

    def _sim(self, method: str, path: str, data: dict | None = None):
        self.log(f"[dry-run] {method} {path} data={json.dumps(data or {}, default=str)[:400]}")
        return None  # per-method simulators return the real value

    # ------------------------------------------------------------ nodes
    def node_status(self) -> dict:
        """GET /nodes/<node>/status — cpu count, memory, uptime, status."""
        if self.dry_run:
            self._sim("GET", f"/nodes/{self.node}/status")
            return {
                "status": "online",
                "uptime": 6000,
                "cpu": 0.04,
                "cpuinfo": {"cpus": 8, "model": "AMD Dry-run(tm) 8-Core"},
                "memory": {"total": 64 * 1024 * 1024 * 1024,
                           "used": 12 * 1024 * 1024 * 1024,
                           "free": 52 * 1024 * 1024 * 1024},
            }
        return self._request("GET", f"/nodes/{self.node}/status")

    # --------------------------------------------------------- resources
    def cluster_resources(self, rtype: str = "vm") -> list[dict]:
        if self.dry_run:
            self._sim("GET", f"/cluster/resources?type={rtype}")
            return []
        return self._request("GET", f"/cluster/resources?type={rtype}")

    def free_vmid(self, low: int, high: int) -> int | None:
        """Lowest unused vmid in [low, high], or None if the range is full."""
        used = {
            int(r["vmid"])
            for r in self.cluster_resources("vm")
            if r.get("vmid") and low <= int(r["vmid"]) <= high
        }
        for vmid in range(low, high + 1):
            if vmid not in used:
                return vmid
        return None

    # --------------------------------------------------------- templates
    def list_templates(self) -> list[dict]:
        if self.dry_run:
            self._sim("GET", f"/nodes/{self.node}/storage/{self.template_storage}/content?content=vztmpl")
            return [
                {"volid": f"{self.template_storage}:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst",
                 "content": "vztmpl", "ctime": 1710000000, "size": 300 * 1024 * 1024},
                {"volid": f"{self.template_storage}:vztmpl/alpine-3.20-default_20240926_amd64.tar.zst",
                 "content": "vztmpl", "ctime": 1720000000, "size": 3 * 1024 * 1024},
            ]
        return self._request(
            "GET",
            f"/nodes/{self.node}/storage/{self.template_storage}/content?content=vztmpl",
        )

    def pick_template(self, prefix: str | None = None) -> str:
        """Newest vztmpl matching an optional filename prefix -> volid."""
        items = self.list_templates()
        if not items:
            raise ProxmoxError(
                f"No container templates found on storage '{self.template_storage}'. "
                "Run proxmox/setup.sh (pveam download) or upload one."
            )
        if prefix:
            name = lambda i: i.get("volid", "").split("/")[-1]  # noqa: E731
            items = [i for i in items if name(i).startswith(prefix)]
            if not items:
                raise ProxmoxError(
                    f"No template matching prefix '{prefix}' on {self.template_storage}. "
                    "Templates: " + ", ".join(i.get("volid", "?") for i in self.list_templates())
                )
        items = sorted(items, key=lambda i: int(i.get("ctime") or 0), reverse=True)
        return items[0]["volid"]

    # ------------------------------------------------------------- lxc
    def create_lxc(self, params: dict) -> str:
        """POST /nodes/<node>/lxc — returns the task UPID."""
        if self.dry_run:
            self._sim("POST", f"/nodes/{self.node}/lxc", params)
            vmid = int(params.get("vmid", 1000))
            return f"UPID:{self.node}:{vmid:08X}:00000001:agenc:create:{self._next_seq()}"
        data = self._request("POST", f"/nodes/{self.node}/lxc", params)
        if isinstance(data, dict):  # PVE may wrap the upid
            return data.get("upid", "")
        return str(data)

    def destroy_lxc(self, vmid: int) -> None:
        if self.dry_run:
            self._sim("DELETE", f"/nodes/{self.node}/lxc/{vmid}")
            return
        self._request("DELETE", f"/nodes/{self.node}/lxc/{vmid}")

    def task_status(self, upid: str) -> dict:
        """Task poll state: running until done; then exitstatus OK/ERR."""
        if self.dry_run:
            self._sim("GET", f"/nodes/{self.node}/tasks/{upid}/status")
            seq = self._sim_seq.get(upid, 0) + 1
            self._sim_seq[upid] = seq
            if seq < 3:  # a few rounds of "running", then success
                return {"status": "running", "upid": upid}
            return {"status": "stopped", "upid": upid, "exitstatus": "OK",
                    "starttime": int(time.time()) - 6, "endtime": int(time.time())}
        return self._request("GET", f"/nodes/{self.node}/tasks/{urllib.parse.quote(upid)}/status")

    def container_current(self, vmid: int) -> dict:
        """LXC status: running/stopped, net-in/out, uptime."""
        if self.dry_run:
            self._sim("GET", f"/nodes/{self.node}/lxc/{vmid}/status/current")
            return {"status": "running", "vmid": vmid, "uptime": 60}
        return self._request("GET", f"/nodes/{self.node}/lxc/{vmid}/status/current")

    def exec(self, vmid: int, command: list[str]) -> tuple[int, str] | None:
        """Run a command in the container. Returns (exitstatus, stdout) or None."""
        if self.dry_run:
            self._sim("POST", f"/nodes/{self.node}/lxc/{vmid}/exec", {"command": command})
            joined = " ".join(command)
            if "test -f" in joined or (command and command[0] == "test"):
                return 0, ""
            if command and command[0] == "cat":
                return 0, "0\n"  # stack-done marker says success
            return 0, f"10.10.{(vmid >> 8) % 128}.{vmid % 256}\n"
        try:
            out = self._request(
                "POST",
                f"/nodes/{self.node}/lxc/{vmid}/exec",
                {"command": json.dumps(command)},
            )
        except ProxmoxError:
            return None
        if isinstance(out, dict):
            return int(out.get("exitstatus") or 0), str(out.get("out-data") or "")
        return 0, ""

    def container_ip(self, vmid: int) -> str | None:
        """Best-effort IPv4 of the container (needs working /exec)."""
        result = self.exec(vmid, ["hostname", "-I"])
        if not result or result[0] != 0:
            return None
        for token in result[1].split():
            if token.count(".") == 3 and not token.startswith(("0.", "169.254")):
                return token
        return None

    # ------------------------------------------------------------ network
    def net_exists(self, name: str) -> bool | None:
        """Is `name` a usable NIC target on the node?

        Returns tri-state:
          True  — verified present (SDN vnet or linux bridge)
          False — verified absent on both
          None  — cannot determine (e.g. the token lacks permission to list
                  SDN vnets, or SDN is not enabled). None != missing: the
                  caller should proceed and let creation surface the truth.
        """
        if self.dry_run:
            self._sim("GET", "/cluster/sdn/vnets")
            self._sim("GET", f"/nodes/{self.node}/network")
            return name == "sandbox"

        vnet_known: bool | None = None
        bridge_known: bool | None = None
        try:
            vnets = self._request("GET", "/cluster/sdn/vnets") or []
            vnet_known = any(str(v.get("vnet")) == name for v in vnets)
        except ProxmoxError:
            vnet_known = None  # permission / SDN not configured — unknown
        try:
            ifaces = self._request("GET", f"/nodes/{self.node}/network") or []
            bridge_known = any(
                i.get("name") == name and i.get("type") == "bridge" for i in ifaces
            )
        except ProxmoxError:
            bridge_known = None

        if vnet_known is True or bridge_known is True:
            return True
        if vnet_known is False and bridge_known is False:
            return False
        return None

    # ------------------------------------------------------------ storage
    def storage_status(self, storage: str) -> dict:
        """Free/total bytes on a storage pool (preflight disk headroom)."""
        if self.dry_run:
            self._sim("GET", f"/nodes/{self.node}/storage/{storage}/status")
            return {"storage": storage, "enabled": 1,
                    "total": 8 * 1024 ** 4, "used": 2 * 1024 ** 4,
                    "avail": 6 * 1024 ** 4}
        return self._request(
            "GET",
            f"/nodes/{self.node}/storage/{urllib.parse.quote(storage)}/status",
        )

    # ----------------------------------------------------------- lifecycle
    def stop_lxc(self, vmid: int) -> None:
        """Stop a container (billing halt). Async on PVE; fire-and-forget."""
        if self.dry_run:
            self._sim("POST", f"/nodes/{self.node}/lxc/{vmid}/status/stop")
            return
        self._request("POST", f"/nodes/{self.node}/lxc/{vmid}/status/stop")

    def start_lxc(self, vmid: int) -> None:
        """Start a container (renew after a halt). Async on PVE."""
        if self.dry_run:
            self._sim("POST", f"/nodes/{self.node}/lxc/{vmid}/status/start")
            return
        self._request("POST", f"/nodes/{self.node}/lxc/{vmid}/status/start")

    # ------------------------------------------------------------ misc
    def _next_seq(self) -> int:
        self._sim_counter[0] += 1
        return self._sim_counter[0]