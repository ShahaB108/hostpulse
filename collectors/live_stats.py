#!/usr/bin/env python3
"""
Live process/memory snapshot collector.

Replaces the old separate live_procs.py + live_mem.py pair: both ran their
own `ps` command independently, which meant two subprocess calls and an
unused HOSTPULSE_PS_COMMAND config value. This version runs `ps` once and
aggregates CPU%, RSS memory, and process count per user from the same
snapshot, so the numbers are also consistent with each other (same instant
in time) rather than two slightly different snapshots.

This is a point-in-time snapshot, not a rolling average -- a single burst
can trigger a flag here even if the user is normally quiet. Combine with
the lveinfo collector's fault counts (which reflect sustained/repeated
throttling over the period) for a fuller picture rather than trusting a
single ps snapshot alone.
"""

import subprocess
from typing import Dict, Any


def collect(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run the configured ps command and aggregate per-user CPU%, RSS memory
    (MiB), and process count. Expects columns in the order:
    user, pcpu, pmem, rss, pid (see HOSTPULSE_PS_COMMAND in the env file).
    """
    command = config.get("ps_command", "ps -eo user=,pcpu=,pmem=,rss=,pid=")

    try:
        process = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(config.get("command_timeout", 120)),
            check=False,
        )
    except Exception as exc:
        return {"status": "error", "users": {}, "users_found": 0, "error": str(exc)}

    ignored_users = config.get("ignored_users", set())
    users: Dict[str, Dict[str, float]] = {}

    for line in process.stdout.splitlines():
        # Expected: user  pcpu  pmem  rss  pid  (5 whitespace-separated fields)
        parts = line.split()
        if len(parts) < 5:
            continue

        username, cpu_value, mem_value, rss_value, _pid = parts[:5]

        if username in ignored_users:
            continue

        try:
            cpu_percent = float(cpu_value)
            rss_kb = float(rss_value)
        except ValueError:
            continue

        if username not in users:
            users[username] = {"cpu_percent": 0.0, "rss_mb": 0.0, "nproc": 0}

        users[username]["cpu_percent"] += cpu_percent
        # ps reports RSS in KiB; convert to MiB to match configured thresholds.
        users[username]["rss_mb"] += rss_kb / 1024.0
        users[username]["nproc"] += 1

    return {
        "status": "ok" if process.returncode == 0 else "error",
        "users": users,
        "users_found": len(users),
        "returncode": process.returncode,
        "error": process.stderr[-1000:] if process.returncode != 0 else None,
    }


if __name__ == "__main__":
    # Manual test entrypoint: prints the aggregated snapshot as JSON.
    import json

    result = collect({
        "ps_command": "ps -eo user=,pcpu=,pmem=,rss=,pid=",
        "ignored_users": set(),
        "command_timeout": 120,
    })

    print(json.dumps(result, indent=2, ensure_ascii=False))
