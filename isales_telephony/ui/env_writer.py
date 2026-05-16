"""Read/write ``%APPDATA%\\isales\\env\\telephony.env``.

Spec: deployment-topology § Scenario "Windows 安装路径约定" — env file
lives under ``%APPDATA%\\isales\\env\\telephony.env``.

Format: shell-style ``KEY=VALUE``, one per line, ``#`` comments. We
intentionally do NOT depend on python-dotenv — the format is trivial
and the deploy/cloud/env/ scripts elsewhere in the repo handle the same
format with plain ``source``. Quoted values are stripped; backslash
escapes are NOT processed (Windows path style ``C:\\path`` is treated
verbatim, matching how systemd EnvironmentFile parses).

The keys we care about are ``ISALES_EDGE_DEVICE_TOKEN`` (the activation
code) and ``ISALES_CLOUD_GRPC_ENDPOINT``. Anything else is preserved on
rewrite (we update in place rather than rewrite the whole file) so
operators can hand-edit other knobs.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# Spec literal: tokens are random opaque strings; we don't enforce a
# format, only basic sanity. Cloud-side ``isales-api`` mints them via
# the bearer-token helper, so the only thing the client can do is
# reject obvious typos (whitespace in the middle, control characters).
_TOKEN_MIN_LEN = 16
_TOKEN_MAX_LEN = 512
_TOKEN_ALLOWED = re.compile(r"^[A-Za-z0-9._\-+=/:]+$")

# gRPC endpoint = ``host:port`` with optional ``grpcs://`` scheme. We
# accept either, the gRPC channel constructor handles both.
_ENDPOINT_RE = re.compile(
    r"^(?:grpcs?://)?[A-Za-z0-9.\-]+(?::\d{1,5})?$"
)


class EnvFileError(ValueError):
    """Raised when the env file or one of the inputs is malformed."""


def default_env_path() -> Path:
    """Resolve ``%APPDATA%\\isales\\env\\telephony.env``.

    Falls back to ``~/.config/isales/telephony.env`` on non-Windows
    hosts so that the same writer is usable from cross-platform unit
    tests + the macOS dev path. Production Windows always hits the
    APPDATA branch.
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "isales" / "env" / "telephony.env"
    return Path.home() / ".config" / "isales" / "telephony.env"


def validate_token(token: str) -> None:
    """Raise :class:`EnvFileError` if the token looks obviously malformed.

    Cloud server is still the source of truth — this only catches
    paste accidents (newlines, surrounding spaces, control chars).
    """
    token = token.strip()
    if not token:
        raise EnvFileError("token is empty")
    if len(token) < _TOKEN_MIN_LEN:
        raise EnvFileError(
            f"token too short (min {_TOKEN_MIN_LEN} chars, got {len(token)})"
        )
    if len(token) > _TOKEN_MAX_LEN:
        raise EnvFileError(f"token too long (max {_TOKEN_MAX_LEN} chars)")
    if not _TOKEN_ALLOWED.match(token):
        raise EnvFileError(
            "token contains disallowed characters; allowed: A-Z a-z 0-9 . _ - + = / :"
        )


def validate_endpoint(endpoint: str) -> None:
    endpoint = endpoint.strip()
    if not endpoint:
        raise EnvFileError("endpoint is empty")
    if not _ENDPOINT_RE.match(endpoint):
        raise EnvFileError(
            f"endpoint {endpoint!r} doesn't look like host[:port] or grpcs://host[:port]"
        )


def read_env(path: Path | None = None) -> dict[str, str]:
    """Parse the env file into a dict. Missing file → empty dict."""
    path = path or default_env_path()
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            logger.warning("env_writer: skipping malformed line %r", raw)
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key.strip()] = value
    return out


def write_token_and_endpoint(
    *,
    token: str,
    endpoint: str,
    path: Path | None = None,
) -> Path:
    """Validate inputs + atomically update token/endpoint in the env file.

    Preserves other keys already in the file. Returns the resolved
    path. Permissions are left at OS default (Windows ACL inherits
    the user-owned APPDATA tree — spec § Scenario "激活码注册流程"
    item 2).
    """
    validate_token(token)
    validate_endpoint(endpoint)

    path = path or default_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    current = read_env(path)
    current["ISALES_EDGE_DEVICE_TOKEN"] = token.strip()
    current["ISALES_CLOUD_GRPC_ENDPOINT"] = endpoint.strip()

    body_lines = [f"{k}={v}" for k, v in current.items()]
    body = "\n".join(body_lines) + "\n"

    # Atomic write — write to .tmp then os.replace(). Important on
    # Windows where a half-flushed file could be read by a concurrent
    # gRPC restart and we'd connect with a corrupt token.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    logger.info("env_writer: wrote token + endpoint to %s", path)
    return path


__all__ = [
    "EnvFileError",
    "default_env_path",
    "read_env",
    "validate_endpoint",
    "validate_token",
    "write_token_and_endpoint",
]
