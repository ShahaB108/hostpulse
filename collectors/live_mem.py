#!/usr/bin/env python3

import subprocess
from typing import Dict, Any


def collect(config: Dict[str, Any]) -> Dict[str, Any]:
    command = "ps -eo user=,rss=,pid="

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
        return {
            "status": "error",
            "users": {},
            "error": str(exc),
        }

    users = {}

    for line in process.stdout.splitlines():
        # Split the output into username, RSS memory usage, and PID.
        parts = line.split(None, 2)

        if len(parts) < 3:
            continue

        username, rss_value, pid = parts

        # Skip users explicitly excluded from resource monitoring.
        if username in config.get("ignored_users", set()):
            continue

        try:
            rss_kb = int(float(rss_value))
        except ValueError:
            continue

        # ps reports RSS in KiB, so convert it to MiB for the result.
        rss_mb = rss_kb / 1024.0

        if username not in users:
            users[username] = {
                "rss_mb": 0.0,
            }

        # Aggregate RSS memory usage across all processes owned by the user.
        users[username]["rss_mb"] += rss_mb

    return {
        "status": "ok" if process.returncode == 0 else "error",
        "users": users,
        "users_found": len(users),
        "returncode": process.returncode,
        "error": process.stderr[-1000:] if process.returncode != 0 else None,
    }


if __name__ == "__main__":
    import json

    result = collect({
        "ignored_users": set(),
        "command_timeout": 120,
    })

    print(json.dumps(result, indent=2, ensure_ascii=False))
