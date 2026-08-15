#!/usr/bin/env python3

import re
import subprocess
from typing import Dict, Any


def _split_table_line(line: str):
    """
    Supports different delimiters used in lveinfo table output:
    ┆
    │
    |
    """

    line = line.strip()

    if "┆" in line:
        return [item.strip() for item in line.split("┆")]

    if "│" in line:
        return [item.strip() for item in line.split("│")]

    if "|" in line:
        return [item.strip() for item in line.split("|")]

    return []


def _normalize_column(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_]+", "", value)
    return value


def _to_number(value: str):
    if value is None:
        return 0

    value = str(value).strip()
    value = value.replace(",", "")

    match = re.search(r"-?\d+(?:\.\d+)?", value)

    if not match:
        return 0

    number = float(match.group(0))

    if number.is_integer():
        return int(number)

    return number


def _find_header(lines):
    """
    Finds the table header based on known column names.
    """

    known_columns = {
        "id",
        "user",
        "username",
        "login",
        "pmemf",
        "nprocf",
        "cpuf",
    }

    for index, line in enumerate(lines):
        columns = _split_table_line(line)

        if not columns:
            continue

        normalized = [_normalize_column(item) for item in columns]
        matches = set(normalized).intersection(known_columns)

        if len(matches) >= 2:
            return index, normalized

    return None, []


def _is_separator_line(line: str) -> bool:
    stripped = line.strip()

    if not stripped:
        return True

    separator_chars = set("─━═-+┄┈│┆|:")

    return all(char in separator_chars or char.isspace() for char in stripped)


def _extract_username(row: Dict[str, str]) -> str:
    username_keys = (
        "id",
        "username",
        "user",
        "login",
        "name",
        "account",
        "owner",
    )

    for key in username_keys:
        candidate = row.get(key)

        if not candidate:
            continue

        candidate = candidate.strip()

        if not candidate:
            continue

        # The ID column may contain either an LVE ID or the username itself.
        if key == "id":
            if not candidate.isdigit():
                return candidate

        elif not candidate.isdigit():
            return candidate

    return ""


def parse_lveinfo_output(output: str) -> Dict[str, Dict[str, Any]]:
    """
    Parses tabular lveinfo output into the following structure:

    {
        "username": {
            "pmemf": 174,
            "nprocf": 0,
            "cpuf": 0
        }
    }
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

        if not columns:
            continue

        # Ignore rows that do not match the expected table structure.
        if len(columns) < 2:
            continue

        if len(columns) < len(headers):
            columns += [""] * (len(headers) - len(columns))

        if len(columns) > len(headers):
            columns = columns[:len(headers)]

        row = dict(zip(headers, columns))
        username = _extract_username(row)

        if not username:
            continue

        users[username] = {
            "pmemf": _to_number(
                row.get("pmemf")
                or row.get("pmem")
                or row.get("pmemfault")
            ),
            "nprocf": _to_number(
                row.get("nprocf")
                or row.get("nproc")
                or row.get("nprocfault")
            ),
            "cpuf": _to_number(
                row.get("cpuf")
                or row.get("cpu")
                or row.get("cpufault")
            ),
        }

    return users


def collect(config: Dict[str, Any]) -> Dict[str, Any]:
    command = config.get(
        "lveinfo_command",
        "lveinfo --period=1d --by-fault any -d --show-all",
    )

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
        return {
            "status": "error",
            "users": {},
            "error": str(exc),
        }

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
        "error": (
            process.stdout[-1000:]
            if process.returncode != 0
            else None
        ),
    }


if __name__ == "__main__":
    import json
    import os

    command = os.getenv(
        "HOSTPULSE_LVEINFO_COMMAND",
        "lveinfo --period=1d --by-fault any -d --show-all",
    )

    result = subprocess.run(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    parsed = parse_lveinfo_output(result.stdout)
    print(json.dumps(parsed, indent=2, ensure_ascii=False))
