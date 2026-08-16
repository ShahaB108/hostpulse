# HostPulse

Detects high-resource-usage users on DirectAdmin + CloudLinux servers by
running a set of collectors (LVE fault counts, live CPU/memory/process
snapshot) and writing a merged JSON report, with optional Prometheus
textfile output for node_exporter.

Python stdlib only. No pip dependencies. Intended to run as root via cron
or a systemd timer.

## Install

```bash
sudo mkdir -p /opt/hostpulse
sudo cp -r hostpulse/* /opt/hostpulse/
cd /opt/hostpulse

# The env file name is hostpulse.env, not .env
sudo cp hostpulse.env.sample hostpulse.env
sudo nano hostpulse.env
```

Before running on a real server, check/edit in `hostpulse.env`:

- `HOSTPULSE_LVEINFO_COMMAND` — confirm `lveinfo` syntax matches this
  server's CloudLinux version (`lveinfo --help`)
- `HOSTPULSE_PS_COMMAND` — if you change the column order here, you must
  also update the positional parsing in `collectors/live_stats.py`
- All `HOSTPULSE_*_WARNING` / `HOSTPULSE_*_CRITICAL` thresholds — the
  defaults are starting guesses, not values tuned to your servers
- `HOSTPULSE_IGNORED_USERS` — add any server-specific service accounts on
  top of the built-in default list

## Run manually (test before relying on cron/timer)

```bash
cd /opt/hostpulse
python3 hostpulse.py
cat output/users.json
```

Run each collector standalone to sanity-check its output against real
commands on this server before trusting the merged report:

```bash
python3 -m collectors.lve_faults
python3 -m collectors.live_stats
```

## Run automatically

Cron example (every 15 minutes):

```
*/15 * * * * root /usr/bin/python3 /opt/hostpulse/hostpulse.py
```

Or a systemd service + timer pointed at the same command works the same
way — a oneshot service triggered by a timer, no special setup needed
beyond `WorkingDirectory=/opt/hostpulse`.

## Output

Written to `HOSTPULSE_JSON_OUTPUT` (default `output/users.json`):

```json
{
  "generated_at": "2026-08-16T10:00:00+00:00",
  "server": "lh675.irandns.com",
  "users": [
    {
      "username": "someuser",
      "metrics": {"pmemf": 62, "nprocf": 0, "cpuf": 0, "cpu_percent": 12.3, "rss_mb": 340.1, "nproc": 4},
      "causes": [
        {"source": "lveinfo", "metric": "pmemf", "value": 62, "status": "warning"}
      ],
      "score": 5,
      "status": "warning"
    }
  ],
  "collector_stats": {
    "lveinfo": {"status": "ok", "users_found": 3, "error": null},
    "live_stats": {"status": "ok", "users_found": 40, "error": null}
  }
}
```

Only users that crossed at least one warning/critical threshold are
included — this is a flagged-users report, not a full inventory.

## Feeding this into Grafana

Set `HOSTPULSE_PROM_OUTPUT` to a path inside node_exporter's textfile
collector directory (e.g.
`/var/lib/node_exporter/textfile_collector/hostpulse.prom`) to get
`hostpulse_user_score`, `hostpulse_user_status`, and per-metric gauges
directly in Prometheus/Grafana, alongside the JSON report for anything
that needs the full detail (causes, per-collector status).

## Design notes (why it's structured this way)

- **Threshold names live in exactly one place**: `THRESHOLD_SPEC` in
  `hostpulse.py`. Both `build_config()` and `evaluate_user()` iterate the
  same table instead of each hardcoding the metric/env-var list separately
  — this is what caused the env/code name mismatch in an earlier version,
  so adding a new metric now means adding one row to the table, not
  editing three places that have to stay in sync by hand.
- **`HOSTPULSE_IGNORED_USERS` in the env file is additive**, not a
  replacement for the built-in `DEFAULT_IGNORED_USERS` list in
  `hostpulse.py`. Only list server-specific extra accounts there.
- **`$HOSTNAME` and other `$VAR` references in `hostpulse.env` are
  expanded** against the real OS environment (not a shell — no command
  execution). If the variable isn't actually set in the environment this
  script runs under (cron/systemd jobs often don't export `HOSTNAME`),
  HostPulse detects the unresolved `$VAR` and falls back to
  `os.uname().nodename` with a logged warning, rather than silently using
  the literal string `"$HOSTNAME"` as the server name.

## Known things to verify before trusting this in production

- `collectors/lve_faults.py`: the table parser handles several delimiter
  styles and finds the header by column name rather than position, but
  hasn't been validated against every CloudLinux version's actual output
  — run it standalone (`python3 -m collectors.lve_faults`) on each server
  type before relying on it.
- `collectors/live_stats.py`: parses `ps` output positionally
  (`user, pcpu, pmem, rss, pid`) — if you change `HOSTPULSE_PS_COMMAND`'s
  column order, update the parsing to match.
- Thresholds and scoring weights in `hostpulse.env.sample` are starting
  points, not numbers derived from real server data — tune them after
  watching real output for a few days.
