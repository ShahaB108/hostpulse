#!/usr/bin/env python3

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

from collectors import lve_faults
from collectors import live_procs
from collectors import live_mem


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "hostpulse.env"
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"

JSON_OUTPUT = OUTPUT_DIR / "users.json"
PROM_OUTPUT = OUTPUT_DIR / "users.prom"
LOG_FILE = LOG_DIR / "hostpulse.log"


COLLECTORS = (
    ("lveinfo", lve_faults),
    ("processes", live_procs),
    ("memory", live_mem),
)


DEFAULT_IGNORED_USERS = {
    "root",
    "daemon",
    "bin",
    "sys",
    "sync",
    "games",
    "man",
    "lp",
    "mail",
    "news",
    "uucp",
    "proxy",
    "www-data",
    "backup",
    "list",
    "irc",
    "gnats",
    "nobody",
    "systemd-network",
    "systemd-resolve",
    "systemd-timesync",
    "messagebus",
    "sshd",
    "apache",
    "nginx",
    "dovecot",
    "exim",
    "postfix",
    "mysql",
    "mariadb",
    "named",
    "bind",
    "redis",
    "memcached",
    "polkitd",
    "dbus",
}


def load_env_file(path: Path) -> Dict[str, str]:
    """Load key-value pairs from a simple environment file."""
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
    """Return an environment variable, falling back to the env file."""
    return os.getenv(name, ENV.get(name, default))


def env_int(name: str, default: int) -> int:
    """Return an environment value converted to an integer."""
    try:
        return int(env_value(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    """Return an environment value converted to a float."""
    try:
        return float(env_value(name, str(default)))
    except ValueError:
        return default


def load_ignored_users():
    """Build the set of users excluded from resource monitoring."""
    configured = env_value("HOSTPULSE_IGNORED_USERS", "")

    users = set(DEFAULT_IGNORED_USERS)

    if configured.strip():
        users.update(
            item.strip()
            for item in configured.split(",")
            if item.strip()
        )

    return users


def build_config() -> Dict[str, Any]:
    """Build the runtime configuration from environment variables."""
    return {
        "command_timeout": env_int("HOSTPULSE_COMMAND_TIMEOUT", 120),
        "lveinfo_command": env_value(
            "HOSTPULSE_LVEINFO_COMMAND",
            "lveinfo --period=1d --by-fault any -d --show-all",
        ),
        "ignored_users": load_ignored_users(),

        "pmemf_warning": env_float("HOSTPULSE_PMEMF_WARNING", 50),
        "pmemf_critical": env_float("HOSTPULSE_PMEMF_CRITICAL", 100),

        "nprocf_warning": env_float("HOSTPULSE_NPROCF_WARNING", 10),
        "nprocf_critical": env_float("HOSTPULSE_NPROCF_CRITICAL", 20),

        "cpuf_warning": env_float("HOSTPULSE_CPUF_WARNING", 10),
        "cpuf_critical": env_float("HOSTPULSE_CPUF_CRITICAL", 20),

        "cpu_warning": env_float("HOSTPULSE_CPU_WARNING", 300),
        "cpu_critical": env_float("HOSTPULSE_CPU_CRITICAL", 400),

        "rss_warning": env_float("HOSTPULSE_RSS_WARNING", 3072),
        "rss_critical": env_float("HOSTPULSE_RSS_CRITICAL", 4096),

        "nproc_warning": env_float("HOSTPULSE_NPROC_WARNING", 15),
        "nproc_critical": env_float("HOSTPULSE_NPROC_CRITICAL", 30),

        "lve_weight": env_float("HOSTPULSE_LVE_WEIGHT", 5),
        "process_weight": env_float("HOSTPULSE_PROCESS_WEIGHT", 3),
        "memory_weight": env_float("HOSTPULSE_MEMORY_WEIGHT", 3),
    }


def setup_logging():
    """Configure file and console logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def status_for_value(value, warning, critical) -> str:
    """Determine the severity of a metric based on configured thresholds."""
    if value >= critical:
        return "critical"

    if value >= warning:
        return "warning"

    return "normal"


def merge_collector_data(
    aggregate: Dict[str, Dict[str, Any]],
    collector_name: str,
    collector_result: Dict[str, Any],
):
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


def evaluate_user(user: Dict[str, Any], config: Dict[str, Any]):
    """Evaluate user metrics and assign severity, causes, and score."""
    metrics = user["metrics"]
    causes = []
    score = 0

    lve_statuses = []

    for metric_name, warning_key, critical_key in (
        ("pmemf", "pmemf_warning", "pmemf_critical"),
        ("nprocf", "nprocf_warning", "nprocf_critical"),
        ("cpuf", "cpuf_warning", "cpuf_critical"),
    ):
        value = float(metrics.get(metric_name, 0) or 0)

        status = status_for_value(
            value,
            config[warning_key],
            config[critical_key],
        )

        lve_statuses.append(status)

        if status != "normal":
            causes.append({
                "source": "lveinfo",
                "metric": metric_name,
                "value": value,
                "status": status,
            })

    if "warning" in lve_statuses or "critical" in lve_statuses:
        score += config["lve_weight"]

    process_statuses = []

    for metric_name, warning_key, critical_key in (
        ("cpu_percent", "cpu_warning", "cpu_critical"),
        ("nproc", "nproc_warning", "nproc_critical"),
    ):
        value = float(metrics.get(metric_name, 0) or 0)

        status = status_for_value(
            value,
            config[warning_key],
            config[critical_key],
        )

        process_statuses.append(status)

        if status != "normal":
            causes.append({
                "source": "processes",
                "metric": metric_name,
                "value": round(value, 2),
                "status": status,
            })

    if "warning" in process_statuses or "critical" in process_statuses:
        score += config["process_weight"]

    memory_value = float(metrics.get("rss_mb", 0) or 0)

    memory_status = status_for_value(
        memory_value,
        config["rss_warning"],
        config["rss_critical"],
    )

    if memory_status != "normal":
        score += config["memory_weight"]

        causes.append({
            "source": "memory",
            "metric": "rss_mb",
            "value": round(memory_value, 2),
            "status": memory_status,
        })

    statuses = (
        lve_statuses
        + process_statuses
        + [memory_status]
    )

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
        key: round(value, 2) if isinstance(value, float) else value
        for key, value in metrics.items()
    }

    return user


def write_json(users, collector_stats):
    """Write the evaluated users and collector statistics to JSON."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hostname": os.uname().nodename,
        "users": users,
        "collector_stats": collector_stats,
    }

    temporary_file = JSON_OUTPUT.with_suffix(".json.tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            indent=2,
            ensure_ascii=False,
        )
        file.write("\n")

    # Replace the previous output atomically to avoid partial JSON files.
    temporary_file.replace(JSON_OUTPUT)


def prometheus_escape(value: str) -> str:
    """Escape characters that are special in Prometheus label values."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")
    )


def write_prometheus(users):
    """Write user metrics in Prometheus exposition format."""
    prom_output = env_value("HOSTPULSE_PROM_OUTPUT", "").strip()

    if not prom_output:
        logging.info("Prometheus output is disabled")
        return

    lines = []

    for user in users:
        username = prometheus_escape(user["username"])
        labels = 'username="{}"'.format(username)

        lines.append(
            "hostpulse_user_score{{{}}} {}".format(
                labels,
                user["score"],
            )
        )

        status_value = {
            "normal": 0,
            "warning": 1,
            "critical": 2,
        }.get(user["status"], 0)

        lines.append(
            "hostpulse_user_status{{{}}} {}".format(
                labels,
                status_value,
            )
        )

        for metric_name, metric_value in user["metrics"].items():
            if isinstance(metric_value, (int, float)):
                lines.append(
                    "hostpulse_user_{}{{{}}} {}".format(
                        metric_name,
                        labels,
                        metric_value,
                    )
                )

    target = Path(prom_output)

    if not target.is_absolute():
        target = BASE_DIR / target

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    logging.info("Prometheus output written to %s", target)


def main():
    """Run all collectors, evaluate users, and write the results."""
    setup_logging()

    logging.info("HostPulse collection started")

    config = build_config()
    aggregate = {}
    collector_stats = {}

    for collector_name, collector_module in COLLECTORS:
        try:
            result = collector_module.collect(config)
        except Exception as exc:
            logging.exception(
                "Collector %s failed",
                collector_name,
            )

            result = {
                "status": "error",
                "users": {},
                "users_found": 0,
                "error": str(exc),
            }

        collector_stats[collector_name] = {
            "status": result.get("status"),
            "users_found": result.get("users_found", 0),
            "error": result.get("error"),
        }

        merge_collector_data(
            aggregate,
            collector_name,
            result,
        )

        logging.info(
            "Collector %s completed: %s",
            collector_name,
            {
                "status": result.get("status"),
                "users_found": result.get("users_found", 0),
            },
        )

    evaluated_users = []

    for user in aggregate.values():
        evaluated = evaluate_user(user, config)

        # Only include users with warning or critical status in the final output.
        if evaluated["status"] != "normal":
            evaluated_users.append(evaluated)

    evaluated_users.sort(
        key=lambda item: (
            -item["score"],
            item["username"],
        )
    )

    write_json(
        evaluated_users,
        collector_stats,
    )

    write_prometheus(evaluated_users)

    logging.info(
        "JSON output written to %s",
        JSON_OUTPUT,
    )

    logging.info(
        "HostPulse collection completed: %d abnormal users",
        len(evaluated_users),
    )


if __name__ == "__main__":
    main()
