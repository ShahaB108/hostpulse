#!/usr/bin/env python3
"""
Email Usage Collector.
Reads sent email counts per user from /etc/virtual/usage/$USERNAME.
Only processes files that correspond to actual system users.
"""

import pwd
from pathlib import Path
from typing import Dict, Any


def collect(config: Dict[str, Any]) -> Dict[str, Any]:
    usage_dir = Path(config.get("email_usage_path", "/etc/virtual/usage"))
    ignored_users = config.get("ignored_users", set())

    if not usage_dir.exists() or not usage_dir.is_dir():
        return {
            "status": "error",
            "users": {},
            "users_found": 0,
            "error": f"Usage directory not found: {usage_dir}",
        }

    users: Dict[str, Dict[str, int]] = {}

    try:
        for entry in usage_dir.iterdir():
            if not entry.is_file():
                continue

            username = entry.name

            # check if its acctually a username
            # skip *.byte files
            try:
                pwd.getpwnam(username)
            except KeyError:
                continue

            if username in ignored_users:
                continue

            try:
                content = entry.read_text(encoding="utf-8").strip()
                # count "1"
                count = content.count('1')
            except Exception:
                count = 0

            users[username] = {"email_count": count}

        return {
            "status": "ok",
            "users": users,
            "users_found": len(users),
            "error": None,
        }

    except Exception as exc:
        return {
            "status": "error",
            "users": {},
            "users_found": 0,
            "error": str(exc),
        }


if __name__ == "__main__":
    import json

    result = collect({"email_usage_path": "/etc/virtual/usage", "ignored_users": set()})
    print(json.dumps(result, indent=2, ensure_ascii=False))
