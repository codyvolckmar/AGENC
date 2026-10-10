#!/usr/bin/env python3
"""Refresh `proxmox.api_token_secret` in a deployed config.json from the
saved PVE token file (/root/.agenc-token by default).

Why: the first deploy may run before the token exists, leaving a
placeholder secret in /opt/agenc/api/config.json. Re-running deploy must
heal it without clobbering the operator's other settings.

Usage:  refresh_token.py <config.json> <token-file>
Exit 0 (idempotent); prints what it did.
"""

from __future__ import annotations

import json
import os
import sys


def main(cfg_path: str, token_file: str) -> int:
    if not os.path.exists(cfg_path):
        print(f"    !! {cfg_path} missing — nothing to refresh")
        return 1
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    current = cfg.get("proxmox", {}).get("api_token_secret", "")

    if os.path.exists(token_file):
        token = open(token_file, "r", encoding="utf-8").read().strip()
        secret = token.split("=", 1)[1].strip() if "=" in token else token
        if current == secret:
            print("    token secret matches — kept")
        else:
            cfg.setdefault("proxmox", {})["api_token_secret"] = secret
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            print("    token secret refreshed from " + token_file)
    else:
        print(f"    !! {token_file} missing — leaving existing secret untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]) if len(sys.argv) >= 3 else 1)