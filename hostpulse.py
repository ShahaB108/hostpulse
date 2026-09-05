#!/usr/bin/env python3
"""
HostPulse - detects high-resource-usage users on DirectAdmin + CloudLinux
servers by running a set of collectors (LVE faults, live process/memory
snapshot) and writing a merged JSON report, with optional Prometheus
textfile output for node_exporter.

All configuration is read from an env file (see hostpulse.env.sample).
Real OS environment variables always take priority over the env file, and
values like $HOSTNAME are expanded against the real environment.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Tuple

from collectors import lve_faults
from collectors import live_stats
from collectors import vhost_traffic
from collectors import lve_db_stats
from collectors import email_usage

from collectors.utils import resolve_username

BASE_DIR = Path(__file__).resolve().parent

# Single source of truth for the HostPulse version. It is included in the
# JSON output (see write_json) so ServerHub agents can read the exact
# version straight from output/users.json instead of guessing from file
# mtimes or requiring git metadata. Bump this on every release.
__version__ = "1.3.0"

# The env file location itself can be overridden via a real OS environment
# variable (set before running this script), since the script obviously
# can't read its own path from inside the file it's about to load.
ENV_FILE = Path(os.getenv("HOSTPULSE_ENV_FILE", str(BASE_DIR / "hostpulse.env")))

COLLECTORS = (
    ("lveinfo", lve_faults),
    ("live_stats", live_stats),
    ("vhost_traffic", vhost_traffic),
    ("lve_db_stats", lve_db_stats),
    ("email_usage", email_usage),
)

DEFAULT_IGNORED_USERS = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "gnats",
    "nobody", "systemd-network", "systemd-resolve", "systemd-timesync",
    "messagebus", "sshd", "apache", "nginx", "dovecot", "exim", "postfix",
    "mysql", "mariadb", "named", "bind", "redis", "memcached", "polkitd",
    "dbus", "chrony",
}

# Single source of truth for every threshold metric: which collector it
# comes from, which key holds the raw value in the merged metrics dict,
# the env var names for warning/critical, the default values, and which
# scoring weight category it belongs to. build_config() and evaluate_user()
# both read from this table instead of each hardcoding the metric list
# separately -- that duplication is what caused the env/code name mismatch
# in the previous version. Add a new threshold metric by adding one row
# here; nothing else needs to change.
THRESHOLD_SPEC: List[Tuple[str, str, str, str, float, float, str]] = [
    # (source,      metric,        env_warn_name,               env_crit_name,               default_warn, default_crit, weight_category)
    ("lveinfo",     "pmemf",       "HOSTPULSE_PMEMF_WARNING",   "HOSTPULSE_PMEMF_CRITICAL",   15,   30,   "lve"),
    ("lveinfo",     "nprocf",      "HOSTPULSE_NPROCF_WARNING",  "HOSTPULSE_NPROCF_CRITICAL",  15,   30,   "lve"),
    ("lveinfo",     "cpuf",        "HOSTPULSE_CPUF_WARNING",    "HOSTPULSE_CPUF_CRITICAL",    15,   30,   "lve"),
    ("lveinfo",     "iof",         "HOSTPULSE_IOF_WARNING",     "HOSTPULSE_IOF_CRITICAL",     15,   30,   "lve"),
    ("lveinfo",     "iopsf",       "HOSTPULSE_IOPSF_WARNING",   "HOSTPULSE_IOPSF_CRITICAL",   15,   30,   "lve"),
    # aCPU and cpu_percent are both CPU-usage numbers (100 = 1 core), so
    # they are evaluated as a PERCENTAGE of that user's own LVE CPU limit
    # (the lCPU field collected by lve_faults), not against an absolute
    # number -- two users at the same raw usage can be at very different
    # fractions of their actual limit if they're on different plans. See
    # LCPU_PERCENT_METRICS below for how the evaluation works.
    ("lveinfo",     "acpu",        "HOSTPULSE_ACPU_WARNING",    "HOSTPULSE_ACPU_CRITICAL",    70,   90,   "lve"),
    ("live_stats",  "cpu_percent", "HOSTPULSE_CPU_WARNING",     "HOSTPULSE_CPU_CRITICAL",     70,   90,   "process"),
    ("live_stats",  "nproc",       "HOSTPULSE_NPROC_WARNING",   "HOSTPULSE_NPROC_CRITICAL",   15,   30,   "process"),
    ("live_stats",  "rss_mb",      "HOSTPULSE_RSS_WARNING",     "HOSTPULSE_RSS_CRITICAL",     3072, 4096, "memory"),
    # CAVEAT: requests_per_min is a delta of the exporter's cumulative
    # per-vhost counter between this run and the previous one, normalized
    # to requests/minute -- it's only as smooth as your cron/timer
    # interval, and reads 0 on the very first run (nothing to diff yet).
    # requests_per_sec_now is a single-second gauge snapshot from the
    # exporter, same "can spike" caveat as the ps snapshot in
    # live_stats.py.
    ("vhost_traffic", "requests_per_min",     "HOSTPULSE_VHOST_REQPM_WARNING", "HOSTPULSE_VHOST_REQPM_CRITICAL", 1000, 3000, "traffic"),
    ("vhost_traffic", "requests_per_sec_now", "HOSTPULSE_VHOST_REQPS_WARNING", "HOSTPULSE_VHOST_REQPS_CRITICAL", 50,   120,  "traffic"),
    # DataBase Usage
    ("lve_db_stats", "db_peak_cpu",       "HOSTPULSE_DB_PEAK_CPU_WARNING", "HOSTPULSE_DB_PEAK_CPU_CRITICAL", 150, 300, "database"),
    ("lve_db_stats", "db_avg_cpu",        "HOSTPULSE_DB_AVG_CPU_WARNING",  "HOSTPULSE_DB_AVG_CPU_CRITICAL",  100, 200, "database"),
    ("lve_db_stats", "db_total_write_mb", "HOSTPULSE_DB_WRITE_MB_WARNING", "HOSTPULSE_DB_WRITE_MB_CRITICAL", 10,  20,  "database"),
    # Email usage
    ("email_usage", "email_count", "HOSTPULSE_EMAIL_WARNING", "HOSTPULSE_EMAIL_CRITICAL", 160, 200, "email"),
]

# Metrics whose warning/critical values are percentages of the user's own
# LVE CPU limit (lCPU, where 100 = 1 core) rather than absolute numbers.
# evaluate_user() multiplies lCPU by the threshold percentage; when lCPU
# is unknown (lveinfo failed or the field is missing), the metric is
# treated as "normal" instead of guessing a limit.
LCPU_PERCENT_METRICS = {"acpu", "cpu_percent"}

WEIGHT_ENV_NAMES = {
    "lve": ("HOSTPULSE_LVE_WEIGHT", 5),
    "process": ("HOSTPULSE_PROCESS_WEIGHT", 4),
    "memory": ("HOSTPULSE_MEMORY_WEIGHT", 4),
    "traffic": ("HOSTPULSE_TRAFFIC_WEIGHT", 3),
    "database": ("HOSTPULSE_DATABASE_WEIGHT", 3),
    "email": ("HOSTPULSE_EMAIL_WEIGHT", 2),
}


def load_env_file(path: Path) -> Dict[str, str]:
    """
    Load key-value pairs from a simple KEY=VALUE env file.
    Values go through os.path.expandvars(), so $HOSTNAME or ${HOSTNAME}
    resolve against the real OS environment at load time -- this is NOT a
    full shell, it does not run commands or expand globs, just variable
    substitution.
    """
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
            value = os.path.expandvars(value)

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
    """
    Build the set of usernames excluded from resource monitoring.
    HOSTPULSE_IGNORED_USERS in the env file is ADDED to the built-in
    DEFAULT_IGNORED_USERS list below, not a replacement for it -- only
    list server-specific extras there, not the whole system-account list.
    """
    configured = env_value("HOSTPULSE_IGNORED_USERS", "")

    users = set(DEFAULT_IGNORED_USERS)

    if configured.strip():
        users.update(item.strip() for item in configured.split(",") if item.strip())

    return users


def _resolve_server_name() -> str:
    """
    Resolve HOSTPULSE_SERVER_NAME, falling back to the actual system
    hostname if it's unset, blank, or still contains an unresolved $VAR
    reference. os.path.expandvars() silently leaves $VAR untouched if that
    variable isn't set in the real environment (e.g. cron/systemd jobs
    often don't export HOSTNAME) -- without this check, the server name
    would end up as the literal string "$HOSTNAME" instead of falling back.
    """
    raw = env_value("HOSTPULSE_SERVER_NAME", "").strip()

    if not raw or "$" in raw:
        if raw:
            logging.warning(
                "HOSTPULSE_SERVER_NAME='%s' has an unresolved variable, "
                "falling back to system hostname", raw,
            )
        return os.uname().nodename

    return raw


def build_config() -> Dict[str, Any]:
    """
    Build the full runtime configuration from environment variables.
    Threshold values are read generically from THRESHOLD_SPEC/WEIGHT_ENV_NAMES
    above rather than one env_float() call per metric, so the env var names
    only ever exist in one place in the code.
    """
    server_name = _resolve_server_name()
    is_mailservice = server_name.lower().startswith("mailservice")
    # Limit check: 2000 for mailservice hostnames, 200 for normal hostnames
    default_email_crit = 2000.0 if is_mailservice else 200.0
    default_email_warn = default_email_crit * 0.8  # 80% limit warning threshold (1600 or 160)

    config: Dict[str, Any] = {
        # Paths / general
        "server_name": _resolve_server_name(),
        "log_level": env_value("HOSTPULSE_LOG_LEVEL", "INFO"),
        "log_file": Path(env_value("HOSTPULSE_LOG_FILE", str(BASE_DIR / "logs" / "hostpulse.log"))),
        "json_output": Path(env_value("HOSTPULSE_JSON_OUTPUT", str(BASE_DIR / "output" / "users.json"))),
        "prom_output": env_value("HOSTPULSE_PROM_OUTPUT", ""),
        # Database collector
        "lve_db_path": env_value("HOSTPULSE_LVE_DB_PATH", "/var/lve/lvestats2.db"),
        # Email collector path
        "email_usage_path": env_value("HOSTPULSE_EMAIL_USAGE_PATH", "/etc/virtual/usage"),

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

        # Vhost traffic collector (LiteSpeed Prometheus exporter)
        "exporter_url": env_value("HOSTPULSE_EXPORTER_URL", "http://127.0.0.1:9936/metrics"),
        "exporter_timeout": env_int("HOSTPULSE_EXPORTER_TIMEOUT", 15),
        "domain_owners_file": env_value("HOSTPULSE_DOMAIN_OWNERS_FILE", "/etc/virtual/domainowners"),
        "vhost_state_file": env_value("HOSTPULSE_VHOST_STATE_FILE", str(BASE_DIR / "state" / "vhost_traffic.json")),

        "ignored_users": load_ignored_users(),
        "thresholds": {},
    }

    # Read every threshold from the single spec table above.
    for _source, metric, warn_name, crit_name, default_warn, default_crit, weight_cat in THRESHOLD_SPEC:
        if metric == "email_count":
            default_warn = default_email_warn
            default_crit = default_email_crit

        config["thresholds"][metric] = {
            "warning": env_float(warn_name, default_warn),
            "critical": env_float(crit_name, default_crit),
            "weight_category": weight_cat,
        }

    # Read scoring weights per category.
    config["weights"] = {
        category: env_float(env_name, default)
        for category, (env_name, default) in WEIGHT_ENV_NAMES.items()
    }

    return config


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
    for raw_user, metrics in collector_result.get("users", {}).items():
        username = resolve_username(raw_user)
        if username not in aggregate:
            aggregate[username] = {
                "username": username,
                "metrics": {},
                "causes": [],
                "score": 0,
            }

        aggregate[username]["metrics"].update(metrics)


def evaluate_user(user: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate a user's merged metrics against THRESHOLD_SPEC and assign
    severity, causes, and score. Iterates the same spec table build_config()
    used, so a metric's env names and defaults only ever live in one place.
    """
    metrics = user["metrics"]
    causes = []
    category_statuses: Dict[str, List[str]] = {}

    for source, metric, _warn_name, _crit_name, _dw, _dc, weight_cat in THRESHOLD_SPEC:
        value = float(metrics.get(metric, 0) or 0)
        thresholds = config["thresholds"][metric]

        if metric in LCPU_PERCENT_METRICS:
            # Percentage-of-plan metric: compare usage against the user's
            # own LVE CPU limit (lCPU). Without a known lCPU the check
            # can't be evaluated, so the metric counts as "normal".
            lcpu = float(metrics.get("lcpu", 0) or 0)
            if lcpu > 0:
                warn_val = lcpu * (thresholds["warning"] / 100.0)
                crit_val = lcpu * (thresholds["critical"] / 100.0)
                status = status_for_value(value, warn_val, crit_val)
            else:
                status = "normal"
        else:
            status = status_for_value(value, thresholds["warning"], thresholds["critical"])

        category_statuses.setdefault(weight_cat, []).append(status)

        if status != "normal":
            display_value = round(value, 2) if isinstance(value, float) else value
            causes.append({"source": source, "metric": metric, "value": display_value, "status": status})

    score = 0.0
    all_statuses = []
    for category, statuses in category_statuses.items():
        all_statuses.extend(statuses)
        if "warning" in statuses or "critical" in statuses:
            score += config["weights"].get(category, 0)

    if "critical" in all_statuses:
        final_status = "critical"
    elif "warning" in all_statuses:
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
        "version": __version__,
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

    logging.info("HostPulse collection started (env file: %s, server: %s)", ENV_FILE, config["server_name"])

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

    evaluated_users.sort(key=lambda item: (-item["score"], str(item["username"])))

    write_json(config, evaluated_users, collector_stats)
    write_prometheus(config, evaluated_users)

    logging.info("JSON output written to %s", config["json_output"])
    logging.info("HostPulse collection completed: %d flagged users", len(evaluated_users))

    return 0


if __name__ == "__main__":
    sys.exit(main())
