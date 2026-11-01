"""DaemonConfig — runtime configuration loaded from environment variables
and an optional persistent JSON file.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..daemons_file import (
    DaemonEntry,
    read_daemons_entries,
    validate_url,
)
from ..ids import AGT_DAEMON_NAMESPACE, daemon_id_from_url
from ..net import host_of_url as _host_of_url, is_local_host as _is_local_host
from ..server_secret import load_server_secret

# Re-exported for backward compatibility (CLI and tests import it from
# here); the canonical definition lives in ``agt.ids``.
_ = (AGT_DAEMON_NAMESPACE, daemon_id_from_url)


def _read_daemons_file() -> list[DaemonEntry]:
    """Return every well-formed entry of ``~/.agt/daemons``.

    The line format itself lives in :mod:`agt.daemons_file` so the daemon
    and the Server can never drift apart on it.
    """
    return read_daemons_entries()


def _port_of_url(url: str) -> str | None:
    """Extract the port from a ``ws://host:port`` / ``http://host:port`` URL."""
    try:
        after_scheme = url.split("://", 1)[1]
        host_part = after_scheme.split("/", 1)[0]
        if ":" in host_part:
            return host_part.rsplit(":", 1)[1]
    except (IndexError, ValueError):
        pass
    return None


def _self_daemon_entry(ws_listen_port: int) -> DaemonEntry | None:
    """Return this machine's entry from ``~/.agt/daemons``, if any.

    Rules (always require BOTH a port match and a local host):
    - any entry whose URL port equals ``ws_listen_port`` AND whose host
      is this machine → that entry
    - otherwise → None

    A single-entry file is NOT assumed to be this machine — that's the
    foot-gun that made a host with only a cloud registration (in
    ``~/.agt/daemons``) appear to be the cloud machine itself, with
    uuid5(NS, cloud_url) as its own daemon_id. The dual check also
    disambiguates a cloud entry and the auto-registered local entry
    when both use port 8422: ``agt daemon start`` appends
    ``local ws://0.0.0.0:<port>`` at startup, and only that entry has
    a local host.

    Since daemon identity can now be persisted explicitly (see
    :func:`_load_daemon_id`), this heuristic is only a fallback for
    installs that have not been through ``agt daemon init``.
    """
    for entry in _read_daemons_file():
        if _port_of_url(entry.url) == str(ws_listen_port) and _is_local_host(
            _host_of_url(entry.url)
        ):
            return entry
    return None


def _find_self_daemon(ws_listen_port: int) -> tuple[str, str] | None:
    """Backward-compatible ``(name, url)`` view of :func:`_self_daemon_entry`."""
    entry = _self_daemon_entry(ws_listen_port)
    return (entry.name, entry.url) if entry is not None else None


def _entry_for_url(url: str | None) -> DaemonEntry | None:
    """The file entry that names ``url`` exactly, if any.

    Unlike :func:`_self_daemon_entry` this makes no assumption about the
    host being local: a paired daemon advertises its public address.
    """
    if not url:
        return None
    for entry in _read_daemons_file():
        if entry.url == url:
            return entry
    return None


def _parse_allowlist(raw: str | None) -> list[Path]:
    """Parse ``AGT_AGENT_CWD_ALLOWLIST`` into a list of absolute Path roots.

    Format: colon-separated absolute paths. Empty / unset → ``[Path.home()]``
    (the safe default). A single ``/`` is allowed and means "anywhere" —
    same semantics as giving the daemon full filesystem access; only set
    this when you trust every submitter.
    """
    if not raw or not raw.strip():
        return [Path.home()]
    out: list[Path] = []
    for part in raw.split(":"):
        part = part.strip()
        if not part:
            continue
        out.append(Path(part))
    return out or [Path.home()]


# ── persistent managed configuration file ────────────────────────────


class DaemonConfigError(ValueError):
    """Invalid daemon configuration file or managed value."""


_CURRENT_FILE_SCHEMA_VERSION = 1

_MANAGED_KEYS: dict[str, str | None] = {
    "max_concurrent": "AGT_MAX_CONCURRENT",
    "agent_timeout": "AGT_AGENT_TIMEOUT",
    "work_dir": "AGT_WORK_DIR",
    "agent_cwd_allowlist": "AGT_AGENT_CWD_ALLOWLIST",
    # Reverse-dial pairing settings. Persisting these is what lets
    # ``agt daemon init`` leave a machine configured across restarts
    # instead of relying on exports in the operator's shell.
    "listen_mode": "AGT_LISTEN_MODE",
    "ws_port": "AGT_DAEMON_WS_PORT",
    "daemon_id": "AGT_DAEMON_ID",
    "daemon_url": None,  # no env equivalent; file-only
    "server_url": "AGT_SERVER_URL",
}


@dataclass
class ManagedDaemonConfig:
    """The subset of daemon settings that can be persisted in
    ``~/.agt/daemon.json``. ``None`` means "not set in the file".
    """

    max_concurrent: int | None = None
    agent_timeout: int | None = None
    work_dir: Path | None = None
    agent_cwd_allowlist: list[Path] | None = None
    listen_mode: bool | None = None
    ws_port: int | None = None
    daemon_id: str | None = None
    daemon_url: str | None = None
    server_url: str | None = None


@dataclass
class ResolvedSetting:
    """One managed setting after precedence resolution."""

    value: Any
    source: str  # "env" | "file" | "default"
    file_value: Any | None = None
    default_value: Any | None = None
    env_name: str | None = None


def daemon_config_file_path() -> Path:
    """Canonical location of the user-level daemon configuration file."""
    return Path.home() / ".agt" / "daemon.json"


def _normalize_config_path(raw: str) -> Path:
    """Expand ``~`` and validate that the result is an absolute path.

    Symlinks are intentionally preserved; the daemon resolves paths at use
    time. Trailing slashes are stripped for stable persistence, except for
    the filesystem root ``/`` which remains valid.
    """
    expanded = os.path.expanduser(raw)
    if expanded != "/":
        expanded = expanded.rstrip("/")
    if not expanded:
        raise DaemonConfigError(f"path is empty: {raw!r}")
    p = Path(expanded)
    if not p.is_absolute():
        raise DaemonConfigError(f"path must be absolute: {raw!r}")
    return p


def _validate_managed_config(data: dict[str, Any]) -> ManagedDaemonConfig:
    """Parse and validate the contents of ``daemon.json``."""
    if not isinstance(data, dict):
        raise DaemonConfigError("configuration file must contain a JSON object")

    version = data.get("version")
    if version != _CURRENT_FILE_SCHEMA_VERSION:
        raise DaemonConfigError(
            f"unsupported configuration version {version!r}; "
            f"expected {_CURRENT_FILE_SCHEMA_VERSION}"
        )

    unknown = set(data.keys()) - set(_MANAGED_KEYS) - {"version"}
    if unknown:
        raise DaemonConfigError(
            f"unknown configuration keys: {sorted(unknown)}"
        )

    cfg = ManagedDaemonConfig()

    if "max_concurrent" in data:
        v = data["max_concurrent"]
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise DaemonConfigError(
                "max_concurrent must be a positive integer"
            )
        cfg.max_concurrent = v

    if "agent_timeout" in data:
        v = data["agent_timeout"]
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise DaemonConfigError(
                "agent_timeout must be a positive integer (seconds)"
            )
        cfg.agent_timeout = v

    if "work_dir" in data:
        if not isinstance(data["work_dir"], str):
            raise DaemonConfigError("work_dir must be a string")
        cfg.work_dir = _normalize_config_path(data["work_dir"])

    if "agent_cwd_allowlist" in data:
        raw = data["agent_cwd_allowlist"]
        if not isinstance(raw, list) or not raw:
            raise DaemonConfigError(
                "agent_cwd_allowlist must be a non-empty array"
            )
        seen: set[str] = set()
        paths: list[Path] = []
        for entry in raw:
            if not isinstance(entry, str):
                raise DaemonConfigError(
                    "agent_cwd_allowlist entries must be strings"
                )
            p = _normalize_config_path(entry)
            key = str(p)
            if key not in seen:
                seen.add(key)
                paths.append(p)
        cfg.agent_cwd_allowlist = paths

    if "listen_mode" in data:
        v = data["listen_mode"]
        if not isinstance(v, bool):
            raise DaemonConfigError("listen_mode must be a boolean")
        cfg.listen_mode = v

    if "ws_port" in data:
        v = data["ws_port"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 65535):
            raise DaemonConfigError("ws_port must be an integer in 1..65535")
        cfg.ws_port = v

    if "daemon_id" in data:
        v = data["daemon_id"]
        if not isinstance(v, str) or not v.strip():
            raise DaemonConfigError("daemon_id must be a non-empty string")
        if any(ch.isspace() for ch in v):
            raise DaemonConfigError("daemon_id must not contain whitespace")
        cfg.daemon_id = v.strip()

    if "daemon_url" in data:
        v = data["daemon_url"]
        if not isinstance(v, str):
            raise DaemonConfigError("daemon_url must be a string")
        try:
            validate_url(v)
        except ValueError as exc:
            raise DaemonConfigError(str(exc)) from exc
        cfg.daemon_url = v

    if "server_url" in data:
        v = data["server_url"]
        if not isinstance(v, str) or not v.startswith(("http://", "https://")):
            raise DaemonConfigError(
                "server_url must be an http:// or https:// URL"
            )
        cfg.server_url = v

    return cfg


def load_daemon_config_file(
    path: Path | None = None,
) -> ManagedDaemonConfig | None:
    """Load and validate the managed configuration file.

    Returns ``None`` when the file is absent. Raises
    :class:`DaemonConfigError` for malformed JSON, unsupported schema
    versions, unknown keys, or invalid values.
    """
    target = path if path is not None else daemon_config_file_path()
    if not target.exists():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise DaemonConfigError(f"cannot read {target}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DaemonConfigError(
            f"invalid JSON in {target}: {exc}"
        ) from exc
    return _validate_managed_config(data)


def _coerce_int_env(name: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise DaemonConfigError(
            f"environment variable {name} must be an integer, got {raw!r}"
        ) from exc


def _coerce_bool_env(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise DaemonConfigError(
        f"environment variable {name} must be a boolean, got {raw!r}"
    )


def resolve_daemon_settings(
    file_cfg: ManagedDaemonConfig | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, ResolvedSetting]:
    """Resolve the four managed settings with env > file > default precedence.

    Returns a mapping from JSON key to a :class:`ResolvedSetting` containing
    the effective value, its source, and the file/default/env layers for
    inspection commands.
    """
    if file_cfg is None:
        file_cfg = load_daemon_config_file()
    env = environ if environ is not None else os.environ
    defaults = DaemonConfig()

    settings: dict[str, ResolvedSetting] = {}

    # max_concurrent
    env_name = _MANAGED_KEYS["max_concurrent"]
    env_raw = env.get(env_name)
    file_val = file_cfg.max_concurrent if file_cfg else None
    default_val = defaults.max_concurrent_tasks
    if env_raw is not None:
        value = _coerce_int_env(env_name, env_raw)
        source = "env"
    elif file_val is not None:
        value = file_val
        source = "file"
    else:
        value = default_val
        source = "default"
    settings["max_concurrent"] = ResolvedSetting(
        value=value,
        source=source,
        file_value=file_val,
        default_value=default_val,
        env_name=env_name,
    )

    # agent_timeout
    env_name = _MANAGED_KEYS["agent_timeout"]
    env_raw = env.get(env_name)
    file_val = file_cfg.agent_timeout if file_cfg else None
    default_val = defaults.agent_timeout
    if env_raw is not None:
        value = _coerce_int_env(env_name, env_raw)
        source = "env"
    elif file_val is not None:
        value = file_val
        source = "file"
    else:
        value = default_val
        source = "default"
    settings["agent_timeout"] = ResolvedSetting(
        value=value,
        source=source,
        file_value=file_val,
        default_value=default_val,
        env_name=env_name,
    )

    # work_dir
    env_name = _MANAGED_KEYS["work_dir"]
    env_raw = env.get(env_name)
    file_val = file_cfg.work_dir if file_cfg else None
    default_val = defaults.work_dir_root
    if env_raw is not None:
        value = Path(env_raw)
        source = "env"
    elif file_val is not None:
        value = file_val
        source = "file"
    else:
        value = default_val
        source = "default"
    settings["work_dir"] = ResolvedSetting(
        value=value,
        source=source,
        file_value=file_val,
        default_value=default_val,
        env_name=env_name,
    )

    # agent_cwd_allowlist
    env_name = _MANAGED_KEYS["agent_cwd_allowlist"]
    env_raw = env.get(env_name)
    file_val = file_cfg.agent_cwd_allowlist if file_cfg else None
    default_val = defaults.agent_cwd_allowlist or [Path.home()]
    if env_raw is not None:
        value = _parse_allowlist(env_raw)
        source = "env"
    elif file_val is not None:
        value = file_val
        source = "file"
    else:
        value = default_val
        source = "default"
    settings["agent_cwd_allowlist"] = ResolvedSetting(
        value=value,
        source=source,
        file_value=file_val,
        default_value=default_val,
        env_name=env_name,
    )

    # listen_mode
    env_name = _MANAGED_KEYS["listen_mode"]
    env_raw = env.get(env_name) if env_name else None
    file_val = file_cfg.listen_mode if file_cfg else None
    default_val = defaults.listen_mode
    if env_raw is not None and env_raw != "":
        value = _coerce_bool_env(env_name, env_raw)
        source = "env"
    elif file_val is not None:
        value = file_val
        source = "file"
    else:
        value = default_val
        source = "default"
    settings["listen_mode"] = ResolvedSetting(
        value=value,
        source=source,
        file_value=file_val,
        default_value=default_val,
        env_name=env_name,
    )

    # ws_port
    env_name = _MANAGED_KEYS["ws_port"]
    env_raw = env.get(env_name) if env_name else None
    file_val = file_cfg.ws_port if file_cfg else None
    default_val = defaults.ws_listen_port
    if env_raw is not None and env_raw != "":
        value = _coerce_int_env(env_name, env_raw)
        source = "env"
    elif file_val is not None:
        value = file_val
        source = "file"
    else:
        value = default_val
        source = "default"
    settings["ws_port"] = ResolvedSetting(
        value=value,
        source=source,
        file_value=file_val,
        default_value=default_val,
        env_name=env_name,
    )

    # daemon_id (explicit identity override; None means "derive")
    env_name = _MANAGED_KEYS["daemon_id"]
    env_raw = env.get(env_name) if env_name else None
    file_val = file_cfg.daemon_id if file_cfg else None
    default_val = None
    if env_raw is not None and env_raw.strip():
        value = env_raw.strip()
        source = "env"
    elif file_val is not None:
        value = file_val
        source = "file"
    else:
        value = default_val
        source = "default"
    settings["daemon_id"] = ResolvedSetting(
        value=value,
        source=source,
        file_value=file_val,
        default_value=default_val,
        env_name=env_name,
    )

    # daemon_url (file-only; the URL this daemon advertises)
    env_name = _MANAGED_KEYS["daemon_url"]
    file_val = file_cfg.daemon_url if file_cfg else None
    default_val = ""
    if file_val is not None:
        value = file_val
        source = "file"
    else:
        value = default_val
        source = "default"
    settings["daemon_url"] = ResolvedSetting(
        value=value,
        source=source,
        file_value=file_val,
        default_value=default_val,
        env_name=env_name,
    )

    # server_url (Server base URL; env wins, file is the persisted form)
    env_name = _MANAGED_KEYS["server_url"]
    env_raw = env.get(env_name) if env_name else None
    file_val = file_cfg.server_url if file_cfg else None
    default_val = ""
    if env_raw is not None and env_raw.strip():
        value = env_raw.strip()
        source = "env"
    elif file_val is not None:
        value = file_val
        source = "file"
    else:
        value = default_val
        source = "default"
    settings["server_url"] = ResolvedSetting(
        value=value,
        source=source,
        file_value=file_val,
        default_value=default_val,
        env_name=env_name,
    )

    return settings


def _managed_config_to_dict(cfg: ManagedDaemonConfig) -> dict[str, Any]:
    """Serialize a managed config to the JSON-on-disk shape."""
    data: dict[str, Any] = {"version": _CURRENT_FILE_SCHEMA_VERSION}
    if cfg.max_concurrent is not None:
        data["max_concurrent"] = cfg.max_concurrent
    if cfg.agent_timeout is not None:
        data["agent_timeout"] = cfg.agent_timeout
    if cfg.work_dir is not None:
        data["work_dir"] = str(cfg.work_dir)
    if cfg.agent_cwd_allowlist is not None:
        data["agent_cwd_allowlist"] = [str(p) for p in cfg.agent_cwd_allowlist]
    if cfg.listen_mode is not None:
        data["listen_mode"] = cfg.listen_mode
    if cfg.ws_port is not None:
        data["ws_port"] = cfg.ws_port
    if cfg.daemon_id is not None:
        data["daemon_id"] = cfg.daemon_id
    if cfg.daemon_url is not None:
        data["daemon_url"] = cfg.daemon_url
    if cfg.server_url is not None:
        data["server_url"] = cfg.server_url
    return data


def _load_existing_or_empty(
    path: Path,
) -> tuple[dict[str, Any], bool]:
    """Return ``(data, existed)`` for the configuration file.

    Raises :class:`DaemonConfigError` when the existing file is malformed so
    that callers can refuse to overwrite user-edited broken state.
    """
    if not path.exists():
        return ({"version": _CURRENT_FILE_SCHEMA_VERSION}, False)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DaemonConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DaemonConfigError(
            f"existing configuration file is malformed ({exc}); "
            "fix or remove it before saving"
        ) from exc
    # Validate early so malformed files are never silently overwritten.
    _validate_managed_config(data)
    return (data, True)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(serialized, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def update_daemon_config_file(
    updates: dict[str, Any],
    path: Path | None = None,
) -> None:
    """Atomically update managed keys in the daemon configuration file.

    ``updates`` maps JSON key to raw user input. ``None`` removes a key.
    Paths are normalized; integers are validated. The file is written with
    ``0o600`` permissions only after the full resulting configuration passes
    validation.
    """
    if not updates:
        return
    target = path if path is not None else daemon_config_file_path()
    data, _existed = _load_existing_or_empty(target)

    for key, value in updates.items():
        if key == "version":
            continue
        if key not in _MANAGED_KEYS:
            raise DaemonConfigError(f"unknown setting: {key}")
        if value is None:
            data.pop(key, None)
            continue

        if key in ("max_concurrent", "agent_timeout"):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise DaemonConfigError(
                    f"{key} must be a positive integer"
                )
            data[key] = value
        elif key == "work_dir":
            data[key] = str(_normalize_config_path(str(value)))
        elif key == "agent_cwd_allowlist":
            raw_list = list(value)
            if not raw_list:
                raise DaemonConfigError(
                    "agent_cwd_allowlist must be a non-empty array"
                )
            seen: set[str] = set()
            out: list[str] = []
            for entry in raw_list:
                p = _normalize_config_path(str(entry))
                key_path = str(p)
                if key_path not in seen:
                    seen.add(key_path)
                    out.append(key_path)
            data[key] = out
        elif key == "listen_mode":
            if not isinstance(value, bool):
                raise DaemonConfigError("listen_mode must be a boolean")
            data[key] = value
        elif key == "ws_port":
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not (1 <= value <= 65535)
            ):
                raise DaemonConfigError("ws_port must be an integer in 1..65535")
            data[key] = value
        elif key == "daemon_id":
            text = str(value).strip()
            if not text or any(ch.isspace() for ch in text):
                raise DaemonConfigError(
                    "daemon_id must be a non-empty string without whitespace"
                )
            data[key] = text
        elif key == "daemon_url":
            text = str(value).strip()
            try:
                validate_url(text)
            except ValueError as exc:
                raise DaemonConfigError(str(exc)) from exc
            data[key] = text
        elif key == "server_url":
            text = str(value).strip()
            if not text.startswith(("http://", "https://")):
                raise DaemonConfigError(
                    "server_url must be an http:// or https:// URL"
                )
            data[key] = text

    # Validate the merged result before writing.
    _validate_managed_config(data)

    # If no managed keys remain, delete the file rather than leaving a
    # near-empty JSON object behind.
    managed_present = any(k in data for k in _MANAGED_KEYS)
    if managed_present:
        _atomic_write_json(target, data)
    elif target.exists():
        target.unlink()


def remove_daemon_config_keys(
    keys: list[str],
    path: Path | None = None,
) -> None:
    """Remove managed keys from the daemon configuration file.

    Unknown keys are ignored. After removal, if no managed keys remain the
    file is deleted.
    """
    if not keys:
        return
    target = path if path is not None else daemon_config_file_path()
    data, existed = _load_existing_or_empty(target)
    if not existed:
        return
    changed = False
    for key in keys:
        if key in data:
            del data[key]
            changed = True
    if not changed:
        return
    managed_present = any(k in data for k in _MANAGED_KEYS)
    if managed_present:
        _validate_managed_config(data)
        _atomic_write_json(target, data)
    elif target.exists():
        target.unlink()


# ── end persistent managed configuration file ────────────────────────


def _is_path_under(child: Path, parent: Path) -> bool:
    """Return True if ``child`` is the same as or inside ``parent``.

    Both are resolved (``Path.resolve``) first to defeat symlink tricks
    and relative paths. Used by the agent_cwd allowlist check.
    """
    try:
        child_r = child.resolve()
        parent_r = parent.resolve()
    except (OSError, RuntimeError):
        return False
    if child_r == parent_r:
        return True
    try:
        child_r.relative_to(parent_r)
        return True
    except ValueError:
        return False


def _load_daemon_id(url: str | None, persisted_id: str | None = None) -> str:
    """Stable daemon identity.

    Priority:
    1. ``AGT_DAEMON_ID`` env (explicit override, highest).
    2. ``persisted_id`` — ``daemon_id`` from ``~/.agt/daemon.json`` or the
       ``id=`` field of this machine's ``~/.agt/daemons`` entry. Set by
       ``agt daemon init``, it decouples the identity from the exact
       spelling of the URL.
    3. ``uuid5(AGT_DAEMON_NAMESPACE, url)`` — deterministic, stable across
       restarts; a changed URL (address moved) legitimately changes the id.
    4. Fresh uuid4 — transient, NOT persisted. Only hit when the daemon
       starts with no env override, no persisted id and no self entry in
       ``~/.agt/daemons`` (i.e. started outside ``agt daemon start`` on a
       bare machine); the id will not survive a restart in that case.

    The old ``~/.agt/daemon_id`` persistent-file fallback was removed:
    it was almost never written (the env and url branches return before
    reaching it) while the CLI's local-daemon detection depended on it —
    which therefore never worked. Local detection now goes through
    ``~/.agt/daemons`` (see ``_ensure_local_daemons_entry``).
    """
    env = os.environ.get("AGT_DAEMON_ID")
    if env:
        return env
    if persisted_id:
        return persisted_id
    if url:
        return daemon_id_from_url(url)
    return str(uuid.uuid4())[:8]


@dataclass
class DaemonConfig:
    """All knobs needed to run the daemon. Loaded from env in ``from_env()``."""

    # Server integration (Phase 2).
    server_base_url: str | None = None
    # Shared HMAC secret with the Server; required to sign task tokens injected
    # into spawned agent subprocesses (Phase 3). Empty disables token injection.
    server_secret: str = ""

    # Local identity.
    # ``daemon_id`` is the stable machine/identity key (uuid5 of the daemon's
    # URL when available); ``daemon_name`` is the human-readable label (from
    # ``~/.agt/daemons`` name, or the device name). name may change without
    # affecting id; changing the URL changes the id (address moved).
    # ``daemon_url`` is the URL this daemon advertises for itself, taken from
    # the matching entry in ``~/.agt/daemons`` (used in the daemon_token's
    # ``url`` claim for auditability).
    daemon_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    daemon_name: str = field(default_factory=socket.gethostname)
    daemon_url: str = ""
    device_name: str = field(default_factory=socket.gethostname)
    runtime_name: str = ""

    # Execution.
    max_concurrent_tasks: int = 1
    agent_timeout: int = 600  # seconds
    work_dir_root: Path = field(
        default_factory=lambda: Path.home() / ".agt" / "workspaces"
    )
    # cwd injection (Phase 4 workdir supplement): list of root directories
    # that ``agent_cwd`` from a claim payload is allowed to land under.
    # Default: ``[Path.home()]``. Override via ``AGT_AGENT_CWD_ALLOWLIST``
    # (colon-separated absolute paths; ``/`` = allow anywhere). May be
    # ``None`` for older ``DaemonConfig(...)`` callers — the daemon falls
    # back to ``[Path.home()]`` at use time (see ``Daemon.run``).
    agent_cwd_allowlist: list[Path] | None = None

    # Local resource server (Phase 2). Port 0 = disabled.
    daemon_port: int = 8421
    # Shared secret with spawned agent subprocesses for X-Daemon-Local-Token.
    # Random per-launch by default; can be pinned via AGT_DAEMON_LOCAL_TOKEN.
    local_token: str = ""

    # Reverse-dial mode (Step 3.5). When True, the daemon listens on
    # ``ws_listen_port`` for the Server to dial in, instead of dialing
    # the Server itself. When False (default), the daemon claims via
    # plain HTTP — the local-daemon path.
    listen_mode: bool = False
    ws_listen_port: int = 8422  # AGT_DAEMON_WS_PORT

    # Phase B: reverse-dial proxy MCP server port (127.0.0.1 only). The
    # remote daemon serves the catalog proxy here; local agents instead
    # reach the Server's ``/mcp`` directly, so this is only bound in
    # ``listen_mode``.
    mcp_port: int = 8428  # AGT_DAEMON_MCP_PORT

    @property
    def server_url(self) -> str:
        """Convenience accessor for the Server base URL.

        Returns ``AGT_SERVER_URL`` if set, otherwise a localhost default
        pointing at the well-known Server port.
        """
        if self.server_base_url:
            return self.server_base_url
        return os.environ.get("AGT_SERVER_URL", "http://127.0.0.1:8420")

    @classmethod
    def from_env(cls) -> "DaemonConfig":
        """Build a DaemonConfig from AGT_* environment variables and the
        optional persistent ``~/.agt/daemon.json`` file.

        Resolution order for managed execution settings is:
        environment variable > configuration file > built-in default. A
        malformed configuration file is fatal so the user notices the error
        at startup rather than silently falling back to defaults.
        """
        default = cls()
        resolved = resolve_daemon_settings()
        listen_mode = bool(resolved["listen_mode"].value)
        ws_listen_port = int(resolved["ws_port"].value)
        # ``_self_daemon_entry`` is only a fallback now: ``agt daemon init``
        # persists daemon_id/daemon_url explicitly, and an explicit URL on a
        # public IP would not satisfy the "local host" heuristic anyway.
        self_entry = _self_daemon_entry(ws_listen_port)
        daemon_url = (
            resolved["daemon_url"].value
            or (self_entry.url if self_entry else None)
        )
        # Prefer the entry that actually names this daemon's advertised URL:
        # after ``agt daemon init`` the file holds both a stale local entry
        # (``local ws://0.0.0.0:8422``) and the real one, and only the
        # latter carries the pairing name and id.
        url_entry = _entry_for_url(daemon_url) or self_entry
        # Human-readable label: explicit env, daemons-file name, else "local"
        # (local daemons not registered in ~/.agt/daemons default to "local").
        daemon_name = (
            os.environ.get("AGT_DAEMON_NAME")
            or (url_entry.name if url_entry else None)
            or "local"
        )
        persisted_id = resolved["daemon_id"].value
        if not persisted_id and url_entry is not None:
            persisted_id = url_entry.daemon_id
        daemon_id = _load_daemon_id(daemon_url, persisted_id)
        return cls(
            server_base_url=resolved["server_url"].value or None,
            server_secret=load_server_secret(),
            daemon_id=daemon_id,
            daemon_name=daemon_name,
            daemon_url=daemon_url or "",
            device_name=os.environ.get("AGT_DEVICE_NAME") or default.device_name,
            runtime_name=os.environ.get("AGT_RUNTIME_NAME", ""),
            max_concurrent_tasks=resolved["max_concurrent"].value,
            agent_timeout=resolved["agent_timeout"].value,
            work_dir_root=resolved["work_dir"].value,
            agent_cwd_allowlist=resolved["agent_cwd_allowlist"].value,
            daemon_port=int(os.environ.get("AGT_DAEMON_PORT", str(default.daemon_port))),
            local_token=os.environ.get("AGT_DAEMON_LOCAL_TOKEN") or secrets.token_urlsafe(32),
            listen_mode=listen_mode,
            ws_listen_port=ws_listen_port,
            mcp_port=int(os.environ.get("AGT_DAEMON_MCP_PORT", str(default.mcp_port))),
        )


__all__ = [
    "DaemonConfig",
    "DaemonConfigError",
    "ManagedDaemonConfig",
    "ResolvedSetting",
    "daemon_config_file_path",
    "load_daemon_config_file",
    "resolve_daemon_settings",
    "update_daemon_config_file",
    "remove_daemon_config_keys",
    "AGT_DAEMON_NAMESPACE",
    "_load_daemon_id",
    "_find_self_daemon",
    "_self_daemon_entry",
    "_entry_for_url",
    "_host_of_url",
    "_is_local_host",
    "_parse_allowlist",
    "_is_path_under",
]
