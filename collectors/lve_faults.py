#!/usr/bin/env python3
"""
LVE fault collector.

Runs `lveinfo` for a given period and parses per-user fault counts.

Two output modes are supported:
  1. JSON (`lveinfo ... --json`) -- preferred when available. Verified
     against real output on a CloudLinux server, structure is:
       {"data": [{"ID": "user", "PMemF": 0, "NprocF": 0, "IOf": 0,
                  "IOPSf": 0, "VMemF": 0, ...usage/limit fields...}, ...]}
     Note: this build of lveinfo does NOT report a CPU fault field at all
     (no "CPUf" key in real output) -- CPU throttling shows up only as
     usage-vs-limit (aCPU/lCPU), not as a fault counter. The "cpuf" metric
     below stays at 0 on servers where this field doesn't exist; it's kept
     for forward/backward compatibility with lveinfo builds that DO report
     it under a differently-cased key, rather than assuming it's always
     present.
  2. Plain text table -- fallback for lveinfo builds without --json
     support. Different CloudLinux versions use different delimiters
     (box-drawing characters or pipes) and column layouts, so this parser
     is delimiter-agnostic and locates the header row by matching known
     column names rather than assuming a fixed line number or position.

collect() tries to json.loads() the command's output first; if that
fails (not valid JSON, or missing the expected "data" list), it falls
back to the text-table parser automatically. No separate config flag is
needed -- just add --json to HOSTPULSE_LVEINFO_COMMAND for the more
reliable path; leaving it off (or running on a server without --json
support) falls back safely to text parsing.
"""

import json
import re
import subprocess
from typing import Dict, Any, Optional


# Maps our internal metric name -> the JSON keys lveinfo might use for it,
# checked in order. Add alternate casings here if a different lveinfo
# build uses a different key.
JSON_FIELD_CANDIDATES = {
    "pmemf": ("PMemF", "pmemf"),
    "nprocf": ("NprocF", "nprocf"),
    "cpuf": ("CPUf", "CPUF", "cpuf"),
    "iof": ("IOf", "iof"),
    "iopsf": ("IOPSf", "iopsf"),
    "vmemf": ("VMemF", "vmemf"),
    # aCPU is NOT a fault counter like the others -- it's the user's actual
    # CPU usage number from the lveinfo snapshot, measured against their
    # per-user limit (lCPU), which varies by hosting plan. A raw threshold
    # on aCPU alone doesn't account for that -- see THRESHOLD_SPEC comment
    # in hostpulse.py for the caveat on this metric specifically.
    "acpu": ("aCPU", "acpu"),
}


def parse_lveinfo_json(output: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """
    Parse `lveinfo --json` output. Returns None (not a dict) if the output
    isn't valid JSON or doesn't have the expected shape, so the caller can
    fall back to text-table parsing instead of treating "couldn't parse"
    as "zero faults everywhere".
    """
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None

    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return None

    users = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        username = entry.get("ID") or entry.get("id")
        if not username:
            continue

        metrics = {}
        for our_key, json_keys in JSON_FIELD_CANDIDATES.items():
            value = 0
            for json_key in json_keys:
                if json_key in entry:
                    value = entry[json_key]
                    break
            metrics[our_key] = value if isinstance(value, (int, float)) else 0

        users[username] = metrics

    return users


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


def parse_lveinfo_table(output: str) -> Dict[str, Dict[str, Any]]:
    """
    Parse plain-text tabular lveinfo output into:
        {"username": {"pmemf": 174, "nprocf": 0, "cpuf": 0}, ...}
    Returns an empty dict if no recognizable header row is found (e.g.
    lveinfo produced an error message instead of a table).

    Note: unlike the JSON parser, this only extracts pmemf/nprocf/cpuf --
    the text table format doesn't reliably expose io/iops/vmem fault
    columns or the aCPU usage figure across lveinfo versions, so those
    stay absent (default 0) when falling back to text parsing.
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
    hostpulse.py's aggregation logic. Tries JSON parsing first, falls back
    to the text-table parser if the output isn't valid/expected JSON.
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

    users = parse_lveinfo_json(process.stdout)
    parse_mode = "json"

    if users is None:
        users = parse_lveinfo_table(process.stdout)
        parse_mode = "text"

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
        "parse_mode": parse_mode,
        "error": process.stdout[-1000:] if process.returncode != 0 else None,
    }


if __name__ == "__main__":
    # Manual test entrypoint: prints parsed lveinfo output as JSON, along
    # with which parser (json/text) ended up being used.
    import os

    test_command = os.getenv(
        "HOSTPULSE_LVEINFO_COMMAND",
        "lveinfo --period=1d --by-fault any -d --show-all --json",
    )

    test_process = subprocess.run(
        test_command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    test_users = parse_lveinfo_json(test_process.stdout)
    test_mode = "json"

    if test_users is None:
        test_users = parse_lveinfo_table(test_process.stdout)
        test_mode = "text"

    print(json.dumps({"parse_mode": test_mode, "users": test_users}, indent=2, ensure_ascii=False))
