#!/usr/bin/env python3
"""
HostPulse - detects high-resource-usage users on DirectAdmin + CloudLinux
servers by running a set of collectors (LVE faults, live process/memory
snapshot) and writing a merged JSON report, with optional Prometheus
textfile output for node_exporter.

All configuration is read from an env file (see hostpulse.env.sample).
Real OS environment variables always take priority over the env file.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

from collectors import lve_faults
from collectors import live_stats


BASE_DIR = Path(__file__).resolve().parent

# The env file location itself can be overridden via a real OS environment
# variable (set before running this script), since the script obviously
# can't read its own path from inside the file it's about to load.
ENV_FILE = Path(os.getenv("HOSTPULSE_ENV_FILE", str(BASE_DIR / "hostpulse.env")))

COLLECTORS = (
    ("lveinfo", lve_faults),
    ("live_stats", live_stats),
)

DEFAULT_IGNORED_USERS = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "gnats",
    "nobody", "systemd-network", "systemd-resolve", "systemd-timesync",
    "messagebus", "sshd", "apache", "nginx", "dovecot", "exim", "postfix",
    "mysql", "mariadb", "named", "bind", "redis", "memcached", "polkitd",
    "dbus", "chrony",
}


def load_env_file(path: Path) -> Dict[str, str]:
    """Load key-value pairs from a simple KEY=VALUE env file."""
    values = {}

    if not path.exists():
        return values

    with path.open("r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")

            values[key] = value

    return values


ENV = load_env_file(ENV_FILE)


def env_value(name: str, default: str = "") -> str:
    """Return a config value: real OS env takes priority over the env file."""
    return os.getenv(name, ENV.get(name, default))


def env_int(name: str, default: int) -> int:
    """Return a config value converted to an integer, falling back on parse errors."""
    try:
        return int(env_value(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    """Return a config value converted to a float, falling back on parse errors."""
    try:
        return float(env_value(name, str(default)))
    except ValueError:
        return default


def load_ignored_users() -> set:
    """Build the set of usernames excluded from resource monitoring."""
    configured = env_value("HOSTPULSE_IGNORED_USERS", "")

    users = set(DEFAULT_IGNORED_USERS)

    if configured.strip():
        users.update(item.strip() for item in configured.split(",") if item.strip())

    return users


def build_config() -> Dict[str, Any]:
    """
    Build the full runtime configuration from environment variables.

    NOTE: every key read here must have a matching entry in
    hostpulse.env.sample with the exact same name, or a threshold silently
    falls back to the hardcoded default below instead of the value the
    user thinks they set. Keep these two files in sync.
    """
    return {
        # Paths / general
        "server_name": env_value("HOSTPULSE_SERVER_NAME", os.uname().nodename),
        "log_level": env_value("HOSTPULSE_LOG_LEVEL", "INFO"),
        "log_file": Path(env_value("HOSTPULSE_LOG_FILE", str(BASE_DIR / "logs" / "hostpulse.log"))),
        "json_output": Path(env_value("HOSTPULSE_JSON_OUTPUT", str(BASE_DIR / "output" / "users.json"))),
        "prom_output": env_value("HOSTPULSE_PROM_OUTPUT", ""),

        # Commands
        "command_timeout": env_int("HOSTPULSE_COMMAND_TIMEOUT", 120),
        "lveinfo_command": env_value(
            "HOSTPULSE_LVEINFO_COMMAND",
            "lveinfo --period=1d --by-fault any -d --show-all",
        ),
        "ps_command": env_value(
            "HOSTPULSE_PS_COMMAND",
            "ps -eo user=,pcpu=,pmem=,rss=,pid=",
        ),

        "ignored_users": load_ignored_users(),

        # LVE fault thresholds (24h window, from lveinfo)
        "pmemf_warning": env_float("HOSTPULSE_PMEMF_WARNING", 50),
        "pmemf_critical": env_float("HOSTPULSE_PMEMF_CRITICAL", 100),
        "nprocf_warning": env_float("HOSTPULSE_NPROCF_WARNING", 10),
        "nprocf_critical": env_float("HOSTPULSE_NPROCF_CRITICAL", 20),
        "cpuf_warning": env_float("HOSTPULSE_CPUF_WARNING", 10),
        "cpuf_critical": env_float("HOSTPULSE_CPUF_CRITICAL", 20),

        # Live process snapshot thresholds
        "cpu_warning": env_float("HOSTPULSE_CPU_WARNING", 300),
        "cpu_critical": env_float("HOSTPULSE_CPU_CRITICAL", 400),
        "nproc_warning": env_float("HOSTPULSE_NPROC_WARNING", 15),
        "nproc_critical": env_float("HOSTPULSE_NPROC_CRITICAL", 30),
        "rss_warning": env_float("HOSTPULSE_RSS_WARNING", 3072),
        "rss_critical": env_float("HOSTPULSE_RSS_CRITICAL", 4096),

        # Scoring weights (used to rank flagged users against each other)
        "lve_weight": env_float("HOSTPULSE_LVE_WEIGHT", 5),
        "process_weight": env_float("HOSTPULSE_PROCESS_WEIGHT", 3),
        "memory_weight": env_float("HOSTPULSE_MEMORY_WEIGHT", 3),
    }


def setup_logging(config: Dict[str, Any]) -> None:
    """Configure file and console logging using paths/level from config."""
    log_file = config["log_file"]
    log_file.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, str(config["log_level"]).upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def status_for_value(value: float, warning: float, critical: float) -> str:
    """Determine the severity of a metric based on configured thresholds."""
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "normal"


def merge_collector_data(
    aggregate: Dict[str, Dict[str, Any]],
    collector_result: Dict[str, Any],
) -> None:
    """Merge metrics collected from a single collector into the aggregate."""
    for username, metrics in collector_result.get("users", {}).items():
        if username not in aggregate:
            aggregate[username] = {
                "username": username,
                "metrics": {},
                "causes": [],
                "score": 0,
            }

        aggregate[username]["metrics"].update(metrics)


def evaluate_user(user: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate a user's merged metrics and assign severity, causes, and score."""
    metrics = user["metrics"]
    causes = []
    score = 0.0

    lve_statuses = []
    for metric_name, warning_key, critical_key in (
        ("pmemf", "pmemf_warning", "pmemf_critical"),
        ("nprocf", "nprocf_warning", "nprocf_critical"),
        ("cpuf", "cpuf_warning", "cpuf_critical"),
    ):
        value = float(metrics.get(metric_name, 0) or 0)
        status = status_for_value(value, config[warning_key], config[critical_key])
        lve_statuses.append(status)

        if status != "normal":
            causes.append({"source": "lveinfo", "metric": metric_name, "value": value, "status": status})

    if "warning" in lve_statuses or "critical" in lve_statuses:
        score += config["lve_weight"]

    process_statuses = []
    for metric_name, warning_key, critical_key in (
        ("cpu_percent", "cpu_warning", "cpu_critical"),
        ("nproc", "nproc_warning", "nproc_critical"),
    ):
        value = float(metrics.get(metric_name, 0) or 0)
        status = status_for_value(value, config[warning_key], config[critical_key])
        process_statuses.append(status)

        if status != "normal":
            causes.append({"source": "live_stats", "metric": metric_name, "value": round(value, 2), "status": status})

    if "warning" in process_statuses or "critical" in process_statuses:
        score += config["process_weight"]

    memory_value = float(metrics.get("rss_mb", 0) or 0)
    memory_status = status_for_value(memory_value, config["rss_warning"], config["rss_critical"])

    if memory_status != "normal":
        score += config["memory_weight"]
        causes.append({"source": "live_stats", "metric": "rss_mb", "value": round(memory_value, 2), "status": memory_status})

    statuses = lve_statuses + process_statuses + [memory_status]

    if "critical" in statuses:
        final_status = "critical"
    elif "warning" in statuses:
        final_status = "warning"
    else:
        final_status = "normal"

    user["score"] = score
    user["status"] = final_status
    user["causes"] = causes
    user["metrics"] = {
        key: (round(value, 2) if isinstance(value, float) else value)
        for key, value in metrics.items()
    }

    return user


def write_json(config: Dict[str, Any], users: list, collector_stats: dict) -> None:
    """Write the evaluated users and collector statistics to JSON atomically."""
    output_path = config["json_output"]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": config["server_name"],
        "users": users,
        "collector_stats": collector_stats,
    }

    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.write("\n")

    # Atomic replace so nothing tailing this file ever sees a partial write.
    temp_path.replace(output_path)


def prometheus_escape(value: str) -> str:
    """Escape characters that are special inside a Prometheus label value."""
    return str(value).replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


def write_prometheus(config: Dict[str, Any], users: list) -> None:
    """Write user metrics in Prometheus text exposition format, if enabled."""
    prom_output = str(config.get("prom_output", "")).strip()

    if not prom_output:
        logging.info("Prometheus output is disabled (HOSTPULSE_PROM_OUTPUT is empty)")
        return

    status_to_int = {"normal": 0, "warning": 1, "critical": 2}
    lines = []

    for user in users:
        username = prometheus_escape(user["username"])
        labels = 'username="{}"'.format(username)

        lines.append("hostpulse_user_score{{{}}} {}".format(labels, user["score"]))
        lines.append("hostpulse_user_status{{{}}} {}".format(labels, status_to_int.get(user["status"], 0)))

        for metric_name, metric_value in user["metrics"].items():
            if isinstance(metric_value, (int, float)):
                lines.append("hostpulse_user_{}{{{}}} {}".format(metric_name, labels, metric_value))

    target = Path(prom_output)
    if not target.is_absolute():
        target = BASE_DIR / target

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    logging.info("Prometheus output written to %s", target)


def main() -> int:
    """Run all collectors, evaluate users, and write the JSON/Prometheus output."""
    config = build_config()
    setup_logging(config)

    logging.info("HostPulse collection started (env file: %s)", ENV_FILE)

    aggregate: Dict[str, Dict[str, Any]] = {}
    collector_stats: Dict[str, Any] = {}

    for collector_name, collector_module in COLLECTORS:
        try:
            result = collector_module.collect(config)
        except Exception as exc:
            # A single collector failing should never take down the whole run.
            logging.exception("Collector %s raised an unhandled exception", collector_name)
            result = {"status": "error", "users": {}, "users_found": 0, "error": str(exc)}

        collector_stats[collector_name] = {
            "status": result.get("status"),
            "users_found": result.get("users_found", 0),
            "error": result.get("error"),
        }

        merge_collector_data(aggregate, result)

        logging.info(
            "Collector %s completed: status=%s users_found=%s",
            collector_name, result.get("status"), result.get("users_found", 0),
        )

    evaluated_users = []
    for user in aggregate.values():
        evaluated = evaluate_user(user, config)
        # Only report users that actually crossed a warning/critical threshold.
        if evaluated["status"] != "normal":
            evaluated_users.append(evaluated)

    evaluated_users.sort(key=lambda item: (-item["score"], item["username"]))

    write_json(config, evaluated_users, collector_stats)
    write_prometheus(config, evaluated_users)

    logging.info("JSON output written to %s", config["json_output"])
    logging.info("HostPulse collection completed: %d flagged users", len(evaluated_users))

    return 0


if __name__ == "__main__":
    sys.exit(main())
