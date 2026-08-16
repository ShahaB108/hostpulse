#!/usr/bin/env python3
"""
LVE fault collector.

Runs `lveinfo` for a given period and parses its tabular output into
per-user PMemF / NProcF / CPUf fault counts. lveinfo's plain-text table
format has changed across CloudLinux versions (different delimiters,
column names, spacing), so the parser below is delimiter-agnostic and
locates the header row by matching known column names rather than
assuming a fixed line number or column position.
"""

import re
import subprocess
from typing import Dict, Any


def _split_table_line(line: str):
    """
    Split a table row on whichever delimiter lveinfo used for this build.
    Different CloudLinux/lveinfo versions use box-drawing characters or
    plain pipes; try each in order of how commonly they appear.
    """
    line = line.strip()

    if "\u2506" in line:  # ┆
        return [item.strip() for item in line.split("\u2506")]

    if "\u2502" in line:  # │
        return [item.strip() for item in line.split("\u2502")]

    if "|" in line:
        return [item.strip() for item in line.split("|")]

    return []


def _normalize_column(value: str) -> str:
    """Lowercase a header cell and strip anything that isn't alphanumeric/underscore."""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_]+", "", value)
    return value


def _to_number(value):
    """Extract the first numeric value from a cell, defaulting to 0."""
    if value is None:
        return 0

    value = str(value).strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", value)

    if not match:
        return 0

    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def _find_header(lines):
    """
    Locate the header row by looking for a line whose normalized cells
    contain at least two recognized column names. This avoids depending
    on a fixed header line number, which varies across lveinfo output.
    """
    known_columns = {"id", "user", "username", "login", "pmemf", "nprocf", "cpuf"}

    for index, line in enumerate(lines):
        columns = _split_table_line(line)
        if not columns:
            continue

        normalized = [_normalize_column(item) for item in columns]
        if len(set(normalized).intersection(known_columns)) >= 2:
            return index, normalized

    return None, []


def _is_separator_line(line: str) -> bool:
    """True for blank lines or ASCII/box-drawing table divider lines."""
    stripped = line.strip()
    if not stripped:
        return True

    separator_chars = set("\u2500\u2501\u2550-+\u2504\u2508\u2502\u2506|:")
    return all(char in separator_chars or char.isspace() for char in stripped)


def _extract_username(row: Dict[str, str]) -> str:
    """
    Pull the username out of a parsed row. The 'id' column can hold either
    a numeric LVE ID or the username itself depending on lveinfo version,
    so numeric-only values in that column are skipped in favor of other
    candidate columns.
    """
    username_keys = ("id", "username", "user", "login", "name", "account", "owner")

    for key in username_keys:
        candidate = row.get(key)
        if not candidate:
            continue

        candidate = candidate.strip()
        if not candidate:
            continue

        if not candidate.isdigit():
            return candidate

    return ""


def parse_lveinfo_output(output: str) -> Dict[str, Dict[str, Any]]:
    """
    Parse tabular lveinfo output into:
        {"username": {"pmemf": 174, "nprocf": 0, "cpuf": 0}, ...}
    Returns an empty dict if no recognizable header row is found (e.g.
    lveinfo produced an error message instead of a table).
    """
    lines = output.splitlines()
    header_index, headers = _find_header(lines)

    if header_index is None:
        return {}

    users = {}

    for line in lines[header_index + 1:]:
        if _is_separator_line(line):
            continue

        columns = _split_table_line(line)
        if not columns or len(columns) < 2:
            continue

        # Pad or trim so zip() below lines up cleanly with the header.
        if len(columns) < len(headers):
            columns += [""] * (len(headers) - len(columns))
        elif len(columns) > len(headers):
            columns = columns[:len(headers)]

        row = dict(zip(headers, columns))
        username = _extract_username(row)
        if not username:
            continue

        users[username] = {
            "pmemf": _to_number(row.get("pmemf") or row.get("pmem") or row.get("pmemfault")),
            "nprocf": _to_number(row.get("nprocf") or row.get("nproc") or row.get("nprocfault")),
            "cpuf": _to_number(row.get("cpuf") or row.get("cpu") or row.get("cpufault")),
        }

    return users


def collect(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run the configured lveinfo command and return a collector result dict
    in the standard {status, users, users_found, error} shape expected by
    hostpulse.py's aggregation logic.
    """
    command = config.get("lveinfo_command", "lveinfo --period=1d --by-fault any -d --show-all")

    try:
        process = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=int(config.get("command_timeout", 120)),
            check=False,
        )
    except Exception as exc:
        return {"status": "error", "users": {}, "users_found": 0, "error": str(exc)}

    users = parse_lveinfo_output(process.stdout)
    ignored_users = config.get("ignored_users", set())

    filtered_users = {
        username: metrics
        for username, metrics in users.items()
        if username not in ignored_users
    }

    return {
        "status": "ok" if process.returncode == 0 else "error",
        "users": filtered_users,
        "users_found": len(filtered_users),
        "returncode": process.returncode,
        "error": process.stdout[-1000:] if process.returncode != 0 else None,
    }


if __name__ == "__main__":
    # Manual test entrypoint: prints parsed lveinfo output as JSON.
    import json
    import os

    test_command = os.getenv(
        "HOSTPULSE_LVEINFO_COMMAND",
        "lveinfo --period=1d --by-fault any -d --show-all",
    )

    test_process = subprocess.run(
        test_command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    print(json.dumps(parse_lveinfo_output(test_process.stdout), indent=2, ensure_ascii=False))
