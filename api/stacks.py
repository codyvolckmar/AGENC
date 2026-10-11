"""App-stack manifest — the single source of truth for what an agent home
can contain.

Mirrors https://github.com/codyvolckmar/agent-box (agent-box.sh's package
groups). Used by:
  * the website menu   (GET /api/stacks → option cards customers tick)
  * provisioning       (installing the selected groups per home)
  * the template builder (baking the 'baked' groups into the image)
"""

from __future__ import annotations

import json
import os
import subprocess

_MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stacks.json")
_MANIFEST: dict | None = None


def manifest() -> dict:
    global _MANIFEST
    if _MANIFEST is None:
        with open(_MANIFEST_PATH, "r", encoding="utf-8") as fh:
            _MANIFEST = json.load(fh)
    return _MANIFEST


def groups_by_id() -> dict[str, dict]:
    return {g["id"]: g for g in manifest()["groups"]}


def selectable_ids() -> list[str]:
    """Groups the customer may tick at request time (everything not baked)."""
    baked = set(manifest().get("baked", []))
    return [g["id"] for g in manifest()["groups"] if g["id"] not in baked]


def baked_ids() -> list[str]:
    return manifest().get("baked", [])


def public_catalog() -> dict:
    """Shape for GET /api/stacks (no package lists needed on the client)."""
    baked = set(baked_ids())
    return {
        "baked": [{"id": g["id"], "label": g["label"], "desc": g["desc"]}
                  for g in manifest()["groups"] if g["id"] in baked],
        "selectable": [{"id": g["id"], "label": g["label"], "desc": g["desc"]}
                       for g in manifest()["groups"] if g["id"] not in baked],
        "venv": {"id": manifest()["venv"]["id"], "label": manifest()["venv"]["label"],
                 "desc": manifest()["venv"]["desc"]},
    }


def apt_available(name: str) -> bool | None:
    """Can apt install `name` on this box? True/False, or None if apt-cache unusable.

    Mirrors agent-box.sh's apt_resolve candidate probe. Rust-crate tools
    (ripgrep-all, procs, starship, …) are simply not in Ubuntu's repos and
    must be skipped, not fatal.
    """
    try:
        out = subprocess.run(
            ["apt-cache", "policy", name], capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.startswith("Candidate:"):
            cand = line.split(":", 1)[1].strip()
            return bool(cand) and cand != "(none)"
    return None


def filter_apt(pkgs: list[str]) -> tuple[list[str], list[str]]:
    """(installable, unavailable) split; unknown probed status keeps the pkg."""
    kept: list[str] = []
    dropped: list[str] = []
    for name in pkgs:
        if apt_available(name) is False:
            dropped.append(name)
        else:
            kept.append(name)
    return kept, dropped


def validate_stack(ids: list, max_groups: int) -> tuple[list[str], str | None]:
    """Normalize a stack selection. Returns (clean_ids, error_or_None)."""
    if not isinstance(ids, list):
        return [], "stack must be a list of group ids"
    selectable = set(selectable_ids())
    venv_id = manifest()["venv"]["id"]
    valid = selectable | {venv_id}
    seen: list[str] = []
    for gid in ids:
        if not isinstance(gid, str):
            return [], "stack ids must be strings"
        if gid in seen:
            continue
        if gid not in valid:
            return [], f"unknown stack group '{gid}'"
        seen.append(gid)
    if len(seen) > max_groups:
        return [], f"too many stack groups (max {max_groups})"
    return seen, None


def apt_packages(ids: list, include_baked: bool = False) -> list[str]:
    """Flatten the apt package list for the given group ids."""
    by_id = groups_by_id()
    baked = set(baked_ids())
    pkgs: list[str] = []
    for gid in ids:
        group = by_id.get(gid)
        if not group or not include_baked and gid in baked:
            continue
        for p in group.get("packages", []):
            if p not in pkgs:
                pkgs.append(p)
    return pkgs


def venv_requested(ids: list) -> bool:
    return manifest()["venv"]["id"] in ids


def venv_packages() -> list[str]:
    return list(manifest()["venv"].get("packages", []))