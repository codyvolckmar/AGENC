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
    """One poll of the async create task / stack install; advances state."""
    task = agent.get("task")
    vmid = agent.get("vmid")
    if agent.get("stack_status") == "installing":
        _poll_stack(store, cfg, px, agent)
        return
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
        log.info("agent %s (vmid %s) container created — moving to stack phase", agent["id"], vmid)
        _finish_create(store, cfg, px, agent, ip=None)
    else:
        store.update(
            agent["id"],
            status="error",
            error=f"proxmox task failed: {state.get('exitstatus')}",
            task_end=int(time.time()),
        )


def _finish_create(store, cfg: dict, px: Proxmox, agent: dict, ip: str | None) -> None:
    """Container is up. If the customer curated a stack, install it; else finish."""
    stack = agent.get("stack") or []
    if stack:
        store.update(
            agent["id"],
            ip=ip,
            error=None,
            task_end=int(time.time()),
            stack_status="pending",
        )
        _start_stack(store, cfg, px, agent)
    else:
        if ip is None:
            ip = px.container_ip(agent.get("vmid")) if agent.get("vmid") else None
        _finish_home(store, agent, ip)


def _finish_home(store, agent: dict, ip: str | None) -> None:
    vmid = agent.get("vmid")
    if ip is None and vmid:
        log.warning(
            "agent %s (vmid %s) is up but its IP could not be auto-detected "
            "(pct exec may be denied to this API token) — the home is fine; "
            "find its IP in the Proxmox GUI.",
            agent["id"], vmid,
        )
    store.update(
        agent["id"], status="running", ip=ip, error=None, task_end=int(time.time()),
    )
    log.info("agent %s (vmid %s) is home at %s", agent["id"], vmid, ip)


# ------------------------------------------------------------ app stacks
def _start_stack(store, cfg: dict, px: Proxmox, agent: dict) -> None:
    """Launch the curated stack installer in the container, detached."""
    from stacks import apt_packages, venv_packages, venv_requested

    stack = agent.get("stack") or []
    vmid = agent.get("vmid")
    parts = []
    pkgs = apt_packages(stack, include_baked=False)
    if pkgs:
        parts.append(
            "DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a "
            "apt-get update -qq && apt-get install -y -qq --no-install-recommends "
            + " ".join(pkgs)
        )
    if venv_requested(stack):
        parts.append(
            "python3 -m venv $HOME/.agentbox-venv && "
            "$HOME/.agentbox-venv/bin/python -m pip install --upgrade -q "
            + " ".join(venv_packages())
        )
    if not parts or vmid is None:
        return
    inner = (
        " && ".join(parts)
        + " >/var/log/agenc-stack.log 2>&1; "
        + "echo $? > /root/.agenc-stack-done"
    )
    # Fully detach so pct exec returns immediately; the watchdog polls the marker.
    launch = f"setsid /bin/bash -c '{inner}' </dev/null >/dev/null 2>&1 & echo launched"
    result = px.exec(vmid, ["/bin/sh", "-c", launch])
    store.update(
        agent["id"],
        stack_pid=agent.get("id"),
        stack_status="installing" if result is not None else "failed",
        stack_started_at=int(time.time()),
        stack_error=None if result is not None else "could not launch stack installer",
    )
    if result is None:
        log.warning("agent %s: stack installer could not be launched", agent["id"])


def _poll_stack(store, cfg: dict, px: Proxmox, agent: dict) -> None:
    """Poll for the stack-install completion marker, then finalize."""
    vmid = agent.get("vmid")
    timeout = int(cfg.get("container", {}).get("stack_timeout_s", 900))
    if vmid is None:
        store.update(agent["id"], status="error", error="stack poll: no vmid")
        return
    result = px.exec(vmid, ["/bin/sh", "-c", "test -f /root/.agenc-stack-done"])
    if result is None:  # exec denied — assume install will finish on its own
        log.warning("agent %s: cannot poll stack marker (exec denied)", agent["id"])
        return
    if result[0] != 0:  # marker not yet present
        started = int(agent.get("stack_started_at") or 0)
        if started and int(time.time()) - started > timeout:
            store.update(agent["id"], status="error",
                         error=f"stack install timed out after {timeout}s "
                               "(see /var/log/agenc-stack.log in the container)")
        return
    # marker exists: read the installer's exit code
    code_res = px.exec(vmid, ["cat", "/root/.agenc-stack-done"])
    try:
        code = int((code_res[1] if code_res else "1").strip() or 1)
    except (ValueError, TypeError, IndexError):
        code = 1
    if code == 0:
        ip = agent.get("ip") or px.container_ip(vmid)
        _finish_home(store, agent, ip)
        store.update(agent["id"], stack_status="done", stack_finished_at=int(time.time()))
        log.info("agent %s: app stack installed", agent["id"])
    else:
        store.update(
            agent["id"], status="error", stack_status="failed",
            error="stack install failed (see /var/log/agenc-stack.log in the container)",
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