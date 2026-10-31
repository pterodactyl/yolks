#!/usr/bin/env python3
"""Report Windrose-generated values without mutating Pterodactyl variables.

Pterodactyl injects startup variables into the container but does not provide a
native reverse-sync from a generated configuration file back into the Panel
database. This helper watches ServerDescription.json, prints the effective
values, and writes a small text file to the persistent server volume.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def load_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def get_values(config: dict[str, Any]) -> dict[str, str]:
    persistent = config.get("ServerDescription_Persistent", {})
    if not isinstance(persistent, dict):
        persistent = {}
    return {
        "INVITE_CODE": str(persistent.get("InviteCode", "")).strip(),
        "WORLD_ISLAND_ID": str(persistent.get("WorldIslandId", "")).strip(),
        "PERSISTENT_SERVER_ID": str(persistent.get("PersistentServerId", "")).strip(),
        "DEPLOYMENT_ID": str(config.get("DeploymentId", "")).strip(),
        "WINDROSE_UPSTREAM_DIGEST": os.getenv("WINDROSE_UPSTREAM_DIGEST", "unknown").strip(),
    }


def atomic_write(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Effective Windrose values parsed from R5/ServerDescription.json",
        "# Pterodactyl startup variables are not modified by this file.",
    ]
    for key, value in values.items():
        lines.append(f"{key}={shlex.quote(value)}")
    content = "\n".join(lines) + "\n"

    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def report(values: dict[str, str], output_path: Path) -> None:
    invite_source = "startup override" if os.getenv("INVITE_CODE", "").strip() else "generated/preserved config"
    world_source = "startup override" if os.getenv("WORLD_ISLAND_ID", "").strip() else "generated/preserved config"

    print("Windrose effective server values:", flush=True)
    print(f"  Invite Code: {values['INVITE_CODE'] or '<not available>'} ({invite_source})", flush=True)
    print(f"  World Island ID: {values['WORLD_ISLAND_ID'] or '<not available>'} ({world_source})", flush=True)
    print(f"  Persistent Server ID: {values['PERSISTENT_SERVER_ID'] or '<not available>'}", flush=True)
    print(f"  Values file: {output_path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="wait for Windrose to generate missing values")
    parser.add_argument("--timeout", type=int, default=int(os.getenv("WINDROSE_REPORT_TIMEOUT", "600")))
    args = parser.parse_args()

    server_root = Path(os.getenv("WINDROSE_SERVER_ROOT", "/home/container"))
    config_path = Path(
        os.getenv("WINDROSE_CONFIG_PATH", str(server_root / "R5/ServerDescription.json"))
    )
    output_path = Path(
        os.getenv("WINDROSE_VALUES_PATH", str(server_root / "R5/GeneratedServerValues.txt"))
    )

    deadline = time.monotonic() + max(args.timeout, 0)
    values: dict[str, str] = {}
    while True:
        values = get_values(load_config(config_path))
        ready = bool(values["INVITE_CODE"] and values["WORLD_ISLAND_ID"])
        if ready or not args.watch or time.monotonic() >= deadline:
            break
        time.sleep(2)

    try:
        atomic_write(output_path, values)
    except OSError as exc:
        print(f"Windrose values report could not be written: {exc}", file=sys.stderr, flush=True)
        return 1

    report(values, output_path)
    if args.watch and not (values["INVITE_CODE"] and values["WORLD_ISLAND_ID"]):
        print(
            "Windrose did not populate both generated values before the report timeout.",
            file=sys.stderr,
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
