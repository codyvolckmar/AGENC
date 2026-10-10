"""Agent-home lifecycle on Proxmox.

`start_provision` creates an unprivileged LXC container from a base
template, sized per the business rules:
  * 2 GB RAM  (config: container.memory_mb)
  * full shared CPU pool — the container gets *all* host cores with no
    pinning, so every agent can burst on any core; `cpuunits` weights the
    scheduler share and `cpulimit` (optional) caps one agent's usage.
  * <= 8 GB disk  (config: container.disk_gb) on the rootfs storage.

The PVE create call returns a task UPID which the API's watchdog thread
polls until the task completes, then records the container's IP.
"""

from __future__ import annotations

import json
import logging
import time

from proxmox import Proxmox, ProxmoxError

log = logging.getLogger("agenc.provisioning")


def _host_cores(px: Proxmox, cfg: dict) -> int:
    cores = int(cfg.get("container", {}).get("cores") or 0)
    if cores > 0:
        return cores
    try:
        status = px.node_status()
        cpus = int((status.get("cpuinfo") or {}).get("cpus") or 0)
    except ProxmoxError:
        cpus = 0
    if not cpus:
        raise ProxmoxError(
            "Could not determine host CPU count for shared-core allocation. "
            "Set container.cores explicitly in config.json."
        )
    return cpus


def build_params(px: Proxmox, cfg: dict, agent: dict) -> tuple[int, str, int, dict]:
    """Draft the PVE LXC create payload. Returns (vmid, template, cores, params)."""
    pve_cfg = cfg["proxmox"]
    c = cfg["container"]

    vmid = px.free_vmid(c["vmid_start"], c["vmid_end"])
    if vmid is None:
        raise ProxmoxError(
            f"VMID range {c['vmid_start']}-{c['vmid_end']} exhausted — no homes left."
        )
    template = px.pick_template(pve_cfg.get("os_template"))
    cores = _host_cores(px, cfg)

    ip = c.get("ip", "dhcp")
    bridge = c["net_bridge"]
    net0 = f"name=eth0,bridge={bridge},firewall=0,ip={ip}"

    # Preflight: the NIC target must exist (SDN vnet or linux bridge).
    # For Proxmox SDN vnets the `bridge=` value IS the vnet name, so
    # 'sandbox' here attaches every agent NIC to the sandbox vnet.
    # Set net_preflight: false to skip the existence check (e.g. when the
    # bridge is a raw kernel device PVE's API cannot list).
    if c.get("net_preflight", True):
        check = px.net_exists(bridge)
        if check is False:
            raise ProxmoxError(
                f"NIC target '{bridge}' not found on node '{px.node}' — create an SDN "
                f"vnet named '{bridge}' (or a linux bridge in PVE: Node -> Network), "
                "or set container.net_preflight=false in config.json if you manage "
                "that network yourself. Then retry."
            )
        if check is None:
            log.warning(
                "could not verify NIC target '%s' on %s (token may lack SDN.Audit, or "
                "SDN undetermined) — continuing; create will surface any permission error",
                bridge, px.node,
            )
    else:
        log.info("net_preflight disabled — attaching NICs to '%s' without an existence check", bridge)

    params: dict = {
        "vmid": vmid,
        "ostemplate": template,
        "hostname": agent["hostname"],
        "memory": c["memory_mb"],
        "swap": c.get("swap_mb", 512),
        "cores": cores,
        "cpuunits": c.get("cpuunits", 1024),
        "rootfs": f"{pve_cfg['rootfs_storage']}:{c['disk_gb']}",
        "net0": net0,
        "features": c.get("features", "nesting=1,keyctl=1"),
        "unprivileged": 1 if c.get("unprivileged", True) else 0,
        "ostype": c.get("ostype", "ubuntu"),
        "tags": "agenc",
        "onboot": 0,
        "start": 1 if c.get("start", True) else 0,
        "description": (
            f"AGENCY agent home\nagent_id: {agent['id']}\n"
            f"purpose: {agent.get('purpose', '')}\nemail: {agent.get('email', '')}\n"
            f"status_at_create: {agent.get('status')}"
        ),
    }
    if c.get("cpulimit"):
        params["cpulimit"] = c["cpulimit"]

    keys = []
    if agent.get("ssh_key"):
        keys.append(agent["ssh_key"])
    if c.get("agency_ssh_key"):
        keys.append(c["agency_ssh_key"])
    if keys:
        params["ssh-public-keys"] = "\n".join(keys)

    return vmid, template, cores, params


def start_provision(store, cfg: dict, px: Proxmox, agent: dict) -> str | None:
    """Create the LXC and return the task UPID. Retries vmid selection on race.

    Every failure path records the REAL reason on the agent record before
    re-raising, so the UI/API never shows a misleading "task missing".
    """
    last_error: ProxmoxError | None = None
    for _attempt in range(3):
        try:
            vmid, template, cores, params = build_params(px, cfg, agent)
            upid = px.create_lxc(params)
            break
        except ProxmoxError as exc:
            last_error = exc
            log.warning("provision attempt %d failed: %s", _attempt + 1, exc)
            time.sleep(1)
    else:
        err = f"provision failed: {last_error or 'unknown error'}"
        store.update(
            agent["id"], status="error", error=err,
            task=None, vmid=agent.get("vmid"),
        )
        raise last_error  # type: ignore[misc]

    store.update(
        agent["id"],
        vmid=vmid,
        task=upid,
        template=template,
        cores=cores,
        status="provisioning",
        error=None,
        provision_started_at=int(time.time()),
    )
    return upid


def poll_agent(store, cfg: dict, px: Proxmox, agent: dict) -> None:
    """One poll of the async create task; advances the agent state."""
    task = agent.get("task")
    vmid = agent.get("vmid")
    if not task:
        store.update(agent["id"], status="error", error="provision task missing")
        return
    try:
        state = px.task_status(task)
    except ProxmoxError as exc:
        log.warning("task poll failed: %s", exc)
        return  # keep polling
    if state.get("status") == "running":
        return
    if state.get("exitstatus") == "OK" or agent.get("status") != "provisioning":
        ip = px.container_ip(vmid) if vmid else None
        if ip is None and vmid:
            log.warning(
                "agent %s (vmid %s) is up but its IP could not be auto-detected "
                "(pct exec may be denied to this API token) — the home is fine; "
                "find its IP in the Proxmox GUI.",
                agent["id"], vmid,
            )
        store.update(
            agent["id"],
            status="running",
            ip=ip,
            error=None,
            task_end=int(time.time()),
        )
        log.info("agent %s (vmid %s) is home at %s", agent["id"], vmid, ip)
    else:
        store.update(
            agent["id"],
            status="error",
            error=f"proxmox task failed: {state.get('exitstatus')}",
            task_end=int(time.time()),
        )


def destroy_home(store, cfg: dict, px: Proxmox, agent: dict) -> None:
    """Delete the container and mark the record destroyed."""
    vmid = agent.get("vmid")
    if vmid:
        px.destroy_lxc(vmid)
    from quotes import cancel

    quote = agent.get("quote")
    if quote:
        agent["quote"] = cancel(quote)
    store.update(agent["id"], status="destroyed", destroyed_at=int(time.time()))