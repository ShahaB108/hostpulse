#!/usr/bin/env python3
"""
LVE Database Stats Collector.
Reads historical resource usage directly from CloudLinux /var/lve/lvestats2.db.
"""

import sqlite3
import time
from pathlib import Path
from typing import Dict, Any
from collectors.utils import resolve_username


def collect(config: Dict[str, Any]) -> Dict[str, Any]:
    db_path = Path(config.get("lve_db_path", "/var/lve/lvestats2.db"))

    if not db_path.exists():
        return {
            "status": "error",
            "users": {},
            "users_found": 0,
            "error": f"Database file not found: {db_path}",
        }

    since_ts = int(time.time()) - 86400
    ignored_users = config.get("ignored_users", set())
    users: Dict[str, Dict[str, float]] = {}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        cursor = conn.cursor()

        query = """
            SELECT uid,
                   MAX(sum_cpu) as max_cpu,
                   ROUND(AVG(sum_cpu), 4) as avg_cpu,
                   ROUND(SUM(sum_read), 4) as total_read,
                   ROUND(SUM(sum_write), 4) as total_write
            FROM lve_stats2_history_gov
            WHERE ts >= ?
            GROUP BY uid
        """
        rows = cursor.execute(query, (since_ts,)).fetchall()
        conn.close()

        for row in rows:
            uid, max_cpu, avg_cpu, total_read, total_write = row
            username = resolve_username(uid)

            if not username or username in ignored_users:
                continue

            users[username] = {
                "db_peak_cpu": round(float(max_cpu or 0), 4),
                "db_avg_cpu": round(float(avg_cpu or 0), 4),
                "db_total_read_mb": round(float(total_read or 0), 4),
                "db_total_write_mb": round(float(total_write or 0), 4),
            }

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

    result = collect({"lve_db_path": "/var/lve/lvestats2.db", "ignored_users": set()})
    print(json.dumps(result, indent=2, ensure_ascii=False))
