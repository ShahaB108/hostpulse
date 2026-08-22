#!/usr/bin/env python3
"""
Vhost traffic collector (LiteSpeed Prometheus exporter based).

Replaces log-scanning for per-domain traffic (domain_traffic.py) -- with
~500 domains per server, parsing daily access logs on every run is too
expensive. This collector instead scrapes the already-running LiteSpeed
Prometheus exporter's /metrics endpoint once per run (a few thousand text
lines, not gigabytes of logs) and reads two things:

  - litespeed_total_requests_per_vhost (counter): cumulative request count
    per vhost since LSWS started. We diff this against the previous run's
    value (stored in a small state file) to get requests-per-minute over
    the actual interval between runs, whatever that interval is.
  - litespeed_requests_per_second_per_vhost (gauge): the single most
    recent second's rate, same "point in time, can spike" caveat as the
    ps snapshot in live_stats.py -- kept as a secondary/instant signal,
    not the primary scoring metric.

Confirmed against a real exporter dump from this deployment: LSWS runs
multiple worker cores (.rtreport, .rtreport.4 ... .rtreport.32 in the
sample seen), and every per-vhost metric is reported separately per core
-- the same vhost appears many times with a different `core` label. These
must be SUMMED across cores per vhost, not averaged or taken from one
core, or the numbers will silently undercount by an order of magnitude.

Also confirmed: this exporter build does NOT expose a per-vhost bytes
metric (the README documents litespeed_outgoing_bytes_per_second_per_vhost
but it isn't present in the real /metrics output checked) -- only request
counts are available per vhost here. Traffic volume by bytes stays a gap
until/unless that's confirmed on a newer exporter version.

vhost labels look like "APVH_www.example.com:443" or "APVH_www.example.com"
(no port suffix on the plain HTTP vhost entry) or "" (the catch-all/default
vhost with no site attached, always skipped).
"""

import json
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, Tuple


METRIC_LINE_RE = re.compile(
    r'^(?P<name>litespeed_\w+)\{(?P<labels>[^}]*)\}\s+(?P<value>[-+0-9.eE]+)\s*$'
)
LABEL_RE = re.compile(r'(?P<key>\w+)="(?P<value>[^"]*)"')

TOTAL_REQUESTS_METRIC = "litespeed_total_requests_per_vhost"
CURRENT_RATE_METRIC = "litespeed_requests_per_second_per_vhost"


def _parse_domain_owners(path: Path) -> Dict[str, str]:
    """
    Parse /etc/virtual/domainowners (DirectAdmin's exim domain->user map,
    "domain: user" per line). Returns {} if missing so the collector
    degrades to "no owner mapping" rather than failing the whole run.
    """
    owners: Dict[str, str] = {}

    if not path.exists():
        return owners

    with path.open("r", encoding="utf-8", errors="replace") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or ":" not in line:
                continue

            domain, _, user = line.partition(":")
            domain = domain.strip().lower()
            user = user.strip()

            if domain and user:
                owners[domain] = user

    return owners


def _vhost_to_domain(vhost_label: str) -> str:
    """"APVH_www.example.com:443" -> "www.example.com". "" stays empty."""
    name = vhost_label
    if name.startswith("APVH_"):
        name = name[len("APVH_"):]
    if ":" in name:
        name = name.rsplit(":", 1)[0]
    return name.lower()


def _lookup_owner(domain: str, owners: Dict[str, str]) -> str:
    """
    Look up a vhost's domain in the owners map. DirectAdmin's
    domainowners file lists the bare registered domain (e.g.
    "example.com"), but exporter vhost labels are the actual vhost name,
    which is very often "www.example.com" -- so an exact match fails for
    most www vhosts even though the domain is owned and correctly listed.
    Try the exact name first, then retry with a leading "www." stripped.
    """
    owner = owners.get(domain)
    if owner:
        return owner

    if domain.startswith("www."):
        return owners.get(domain[len("www."):], "")

    return ""


def _fetch_metrics_text(url: str, timeout: int) -> str:
    request = urllib.request.Request(url, headers={"Accept": "text/plain"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _sum_per_vhost(metrics_text: str, metric_name: str) -> Dict[str, float]:
    """
    Sum a single per-vhost metric across every `core` label. Skips the
    empty/catch-all vhost. Non-matching or malformed lines are ignored
    rather than raising -- a malformed line shouldn't sink the whole scrape.
    """
    totals: Dict[str, float] = {}

    for line in metrics_text.splitlines():
        if not line.startswith(metric_name + "{"):
            continue

        match = METRIC_LINE_RE.match(line)
        if not match or match.group("name") != metric_name:
            continue

        labels = dict(LABEL_RE.findall(match.group("labels")))
        vhost_label = labels.get("vhost", "")
        domain = _vhost_to_domain(vhost_label)

        if not domain:
            continue

        try:
            value = float(match.group("value"))
        except ValueError:
            continue

        totals[domain] = totals.get(domain, 0.0) + value

    return totals


def _load_state(path: Path) -> Tuple[Dict[str, float], float]:
    """Returns (previous per-domain totals, previous timestamp). Empty/0 if no state yet (first run)."""
    if not path.exists():
        return {}, 0.0

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("totals", {}), float(data.get("timestamp", 0.0))
    except (ValueError, OSError):
        return {}, 0.0


def _save_state(path: Path, totals: Dict[str, float], timestamp: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps({"timestamp": timestamp, "totals": totals}, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_path.replace(path)


def collect(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Scrape the LiteSpeed exporter, diff cumulative per-vhost requests
    against the previous run to get requests/min, map domains to
    DirectAdmin owners, and aggregate per user.
    """
    exporter_url = config.get("exporter_url", "http://127.0.0.1:9936/metrics")
    owners_file = Path(config.get("domain_owners_file", "/etc/virtual/domainowners"))
    state_file = Path(config.get("vhost_state_file", "/opt/hostpulse/state/vhost_traffic.json"))
    timeout = int(config.get("exporter_timeout", 15))

    try:
        metrics_text = _fetch_metrics_text(exporter_url, timeout)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {
            "status": "error", "users": {}, "users_found": 0,
            "error": "failed to reach exporter at {}: {}".format(exporter_url, exc),
        }

    current_totals = _sum_per_vhost(metrics_text, TOTAL_REQUESTS_METRIC)
    current_rates = _sum_per_vhost(metrics_text, CURRENT_RATE_METRIC)
    now = time.time()

    previous_totals, previous_timestamp = _load_state(state_file)
    elapsed = now - previous_timestamp if previous_timestamp else 0.0

    owners = _parse_domain_owners(owners_file)
    ignored_users = config.get("ignored_users", set())

    per_user: Dict[str, Dict[str, Any]] = {}

    for domain, total_now in current_totals.items():
        previous_value = previous_totals.get(domain, total_now)
        # A drop means LSWS restarted and the counter reset -- treat the
        # current value as the delta rather than going negative.
        delta = total_now - previous_value if total_now >= previous_value else total_now

        requests_per_min = (delta / elapsed * 60.0) if elapsed > 0 else 0.0
        requests_per_sec_now = current_rates.get(domain, 0.0)

        owner = _lookup_owner(domain, owners)
        if not owner or owner in ignored_users:
            continue

        if owner not in per_user:
            per_user[owner] = {
                "requests_per_min": 0.0,
                "requests_per_sec_now": 0.0,
                "top_domain": "",
                "_top_delta": -1.0,
            }

        entry = per_user[owner]
        entry["requests_per_min"] += requests_per_min
        entry["requests_per_sec_now"] += requests_per_sec_now

        if delta > entry["_top_delta"]:
            entry["_top_delta"] = delta
            entry["top_domain"] = domain

    users: Dict[str, Dict[str, Any]] = {}
    for username, entry in per_user.items():
        entry.pop("_top_delta", None)
        users[username] = entry

    _save_state(state_file, current_totals, now)

    return {
        "status": "ok",
        "users": users,
        "users_found": len(users),
        "domains_seen": len(current_totals),
        "first_run": previous_timestamp == 0.0,
        "error": None,
    }


if __name__ == "__main__":
    # Manual test entrypoint. Run this twice a minute or two apart on the
    # real server to see requests_per_min populate (it's 0 on the very
    # first run -- there's nothing to diff against yet).
    import json as _json

    result = collect({
        "exporter_url": "http://127.0.0.1:9936/metrics",
        "domain_owners_file": "/etc/virtual/domainowners",
        "vhost_state_file": "/opt/hostpulse/state/vhost_traffic.json",
        "exporter_timeout": 15,
        "ignored_users": set(),
    })

    print(_json.dumps(result, indent=2, ensure_ascii=False))