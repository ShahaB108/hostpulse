#!/usr/bin/env python3
"""
HostPulse web service -- serves the JSON report written by hostpulse.py
over plain HTTP with HTTP Basic Auth backed by an htpasswd file.

Endpoints:
  GET /health     -- no auth; liveness probe for monitoring/load balancers
  GET /           -- the JSON report (same payload as /users.json)
  GET /users.json -- the JSON report produced by hostpulse.py
  GET /metrics    -- Prometheus textfile output, only if HOSTPULSE_PROM_OUTPUT
                     is set (same file node_exporter reads)

Everything except /health requires Basic Auth against the htpasswd file.
The file is reloaded automatically whenever its mtime changes, so users
can be added/removed without restarting this service.

Configuration is read from the same hostpulse.env file hostpulse.py uses
(real OS environment always takes priority over the env file), plus these
web-only variables:

  HOSTPULSE_WEB_HOST       bind address             (default 0.0.0.0)
  HOSTPULSE_WEB_PORT       bind port                (default 35707)
  HOSTPULSE_HTPASSWD_FILE  htpasswd file location   (default config/htpasswd
                           next to this file, i.e. /opt/hostpulse/config/htpasswd)

Run:            python3 -m uvicorn hostpulse_web:app --host 0.0.0.0 --port 35707
Or simply:      python3 hostpulse_web.py
Manage users:   python3 hostpulse_web.py --add-user admin
                          --htpasswd /opt/hostpulse/config/htpasswd
"""

import argparse
import getpass
import os
import re
import sys
import threading
from pathlib import Path
from typing import Dict, Optional

try:
    import uvicorn
    from fastapi import Depends, FastAPI, HTTPException, Response, status
    from fastapi.security import HTTPBasic, HTTPBasicCredentials
    from passlib.apache import HtpasswdFile
except ImportError as exc:
    sys.exit(
        "Missing dependency: %s\n"
        "The web service needs a few packages the collector doesn't:\n"
        "  python3 -m pip install -r requirements.txt\n" % exc
    )

BASE_DIR = Path(__file__).resolve().parent

# Same env file contract as hostpulse.py: the file path itself can be
# overridden via a real OS environment variable.
ENV_FILE = Path(os.getenv("HOSTPULSE_ENV_FILE", str(BASE_DIR / "hostpulse.env")))


def load_env_file(path: Path) -> Dict[str, str]:
    """
    Load key-value pairs from a simple KEY=VALUE env file.
    Copied from hostpulse.py (importing hostpulse.py directly would drag in
    the collectors, which use Linux-only modules such as pwd). Values go
    through os.path.expandvars() -- variable substitution only, no shell.
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


def _read_version() -> str:
    """Read HostPulse's version from hostpulse.py -- one source of truth."""
    try:
        text = (BASE_DIR / "hostpulse.py").read_text(encoding="utf-8")
        match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "unknown"


# Everything HostPulse needs lives under its install directory
# (/opt/hostpulse by default) -- including the htpasswd file.
WEB_HOST = env_value("HOSTPULSE_WEB_HOST", "0.0.0.0")
try:
    WEB_PORT = int(env_value("HOSTPULSE_WEB_PORT", "35707"))
except ValueError:
    WEB_PORT = 35707

HTPASSWD_PATH = Path(env_value("HOSTPULSE_HTPASSWD_FILE", str(BASE_DIR / "config" / "htpasswd")))

# Where the collector's JSON report lives -- MUST match what hostpulse.py
# writes (same HOSTPULSE_JSON_OUTPUT variable, same default). Relative
# paths are resolved against this file's directory so the endpoint works
# regardless of the process working directory.
_json_output_raw = env_value("HOSTPULSE_JSON_OUTPUT", str(BASE_DIR / "output" / "users.json"))
JSON_OUTPUT_PATH = Path(_json_output_raw)
if not JSON_OUTPUT_PATH.is_absolute():
    JSON_OUTPUT_PATH = BASE_DIR / JSON_OUTPUT_PATH

# Optional Prometheus textfile output -- the very same variable the
# collector uses, so /metrics automatically matches what it writes.
_prom_output_raw = env_value("HOSTPULSE_PROM_OUTPUT", "").strip()
PROM_OUTPUT_PATH = None
if _prom_output_raw:
    _prom_path = Path(_prom_output_raw)
    if not _prom_path.is_absolute():
        _prom_path = BASE_DIR / _prom_path
    PROM_OUTPUT_PATH = _prom_path


# ---------------------------------------------------------------------------
# HTTP Basic Auth against the htpasswd file
# ---------------------------------------------------------------------------

_htpasswd_lock = threading.Lock()
_htpasswd_cache: Dict[str, object] = {"path": None, "mtime": None, "file": None}


def _get_htpasswd() -> HtpasswdFile:
    """
    Return an HtpasswdFile for the configured path, reloading it whenever
    the file's mtime changes so user management never needs a restart.
    """
    if not HTPASSWD_PATH.is_file():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="htpasswd file not found at %s" % HTPASSWD_PATH,
        )

    mtime = HTPASSWD_PATH.stat().st_mtime

    with _htpasswd_lock:
        cached = _htpasswd_cache["file"]
        if (
            cached is None
            or _htpasswd_cache["path"] != str(HTPASSWD_PATH)
            or _htpasswd_cache["mtime"] != mtime
        ):
            cached = HtpasswdFile(str(HTPASSWD_PATH))
            _htpasswd_cache.update(path=str(HTPASSWD_PATH), mtime=mtime, file=cached)
        return cached


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Basic authentication required",
        headers={"WWW-Authenticate": 'Basic realm="hostpulse"'},
    )


security = HTTPBasic(auto_error=False)


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> str:
    """FastAPI dependency: verify the request's Basic Auth credentials."""
    if credentials is None:
        raise _unauthorized()

    try:
        valid = _get_htpasswd().check_password(credentials.username, credentials.password)
    except Exception as exc:
        # Fail closed, but explain why (e.g. bcrypt hash without bcrypt
        # installed). Never leak whether the username exists.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="htpasswd verification failed: %s" % exc,
        )

    if not valid:
        raise _unauthorized()

    return credentials.username


# ---------------------------------------------------------------------------
# FastAPI application and endpoints
# ---------------------------------------------------------------------------

app = FastAPI(
    title="HostPulse",
    version=_read_version(),
    docs_url=None,
    redoc_url=None,
    openapi_url=None,  # don't expose the schema publicly
)


@app.get("/health", include_in_schema=False)
def health() -> dict:
    """Liveness probe -- intentionally unauthenticated."""
    return {"status": "ok", "version": _read_version()}


@app.get("/", dependencies=[Depends(require_auth)], include_in_schema=False)
@app.get("/users.json", dependencies=[Depends(require_auth)])
def report() -> Response:
    """Serve the JSON report produced by hostpulse.py, byte for byte."""
    if not JSON_OUTPUT_PATH.is_file():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JSON report not generated yet -- run hostpulse.py first",
        )

    return Response(
        content=JSON_OUTPUT_PATH.read_bytes(),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/metrics", dependencies=[Depends(require_auth)])
def metrics() -> Response:
    """Serve the Prometheus textfile output, if the collector writes one."""
    if PROM_OUTPUT_PATH is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prometheus output is disabled (HOSTPULSE_PROM_OUTPUT is empty)",
        )

    if not PROM_OUTPUT_PATH.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prometheus output not generated yet",
        )

    return Response(
        content=PROM_OUTPUT_PATH.read_bytes(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# htpasswd user management (so httpd-tools' htpasswd binary isn't required)
# ---------------------------------------------------------------------------

def add_user(username: str, path: Path, scheme: str = "apr1") -> None:
    """Interactively add/update one user in the htpasswd file."""
    # passlib's internal scheme name for `htpasswd -m` is apr_md5_crypt.
    scheme_names = {"apr1": "apr_md5_crypt", "bcrypt": "bcrypt"}
    passlib_scheme = scheme_names.get(scheme, scheme)

    password = getpass.getpass("Password for %s: " % username)
    confirm = getpass.getpass("Confirm password: ")

    if not password:
        sys.exit("Empty passwords are not allowed")
    if password != confirm:
        sys.exit("Passwords do not match")

    if path.exists():
        htpasswd = HtpasswdFile(str(path))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        htpasswd = HtpasswdFile(str(path), new=True, default_scheme=passlib_scheme)

    htpasswd.set_password(username, password)
    htpasswd.save()

    if os.name == "posix":
        path.chmod(0o640)

    print("User %r written to %s (scheme: %s)" % (username, path, scheme))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="HostPulse web service (JSON report over HTTP with htpasswd auth)",
    )
    parser.add_argument(
        "--add-user",
        metavar="USERNAME",
        help="add/update USERNAME in the htpasswd file and exit (no server start)",
    )
    parser.add_argument(
        "--htpasswd",
        metavar="PATH",
        default=str(HTPASSWD_PATH),
        help="htpasswd file path (default: %(default)s)",
    )
    parser.add_argument("--host", default=WEB_HOST, help="bind address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=WEB_PORT, help="bind port (default: %(default)s)")
    parser.add_argument(
        "--scheme",
        choices=("apr1", "bcrypt"),
        default="apr1",
        help="hash scheme for --add-user; bcrypt needs the bcrypt package (default: %(default)s)",
    )
    args = parser.parse_args()

    if args.add_user:
        add_user(args.add_user, Path(args.htpasswd), args.scheme)
        return 0

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
