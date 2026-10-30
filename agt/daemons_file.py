"""Single source of truth for the ``~/.agt/daemons`` file format.

Both processes parse this file: the Server treats it as the reverse-dial
target list, the daemon uses it to discover its own identity. They used
to carry independent line parsers, which is how "the same URL string in
both files" became a silent correctness requirement. One parser, one
rule.

Line format::

    # comment
    <name> <url> [id=<uuid>] [cap1,cap2,...]

``id=`` is optional. Without it the caller derives the id from the URL
(uuid5, see :mod:`agt.ids`) exactly as before — old files keep working
and old deployments keep their identity. When present it wins, which
decouples a daemon's identity from the exact spelling of its URL.

The value must be a valid UUID. A token that merely looks like
``id=`` but is not a UUID is read back as a capability instead, so a
typo can never silently redefine an identity.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_URL_SCHEMES = ("ws://", "wss://")


class DaemonsFileError(ValueError):
    """A write was refused. ``code`` is a stable machine-readable tag."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DaemonEntry:
    """One parsed line of ``~/.agt/daemons``."""

    name: str
    url: str
    daemon_id: str | None = None
    capabilities: tuple[str, ...] = field(default_factory=tuple)


def daemons_file_path() -> Path:
    """Canonical path, honouring ``AGT_DAEMONS_FILE`` when set."""
    override = os.environ.get("AGT_DAEMONS_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agt" / "daemons"


def _as_uuid_or_none(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def parse_daemons_line(raw: str) -> DaemonEntry | None:
    """Parse one line. Returns ``None`` for blanks, comments and junk."""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(None, 1)
    if len(parts) != 2:
        return None
    name, rest = parts[0].strip(), parts[1].strip()
    tokens = rest.split()
    if not name or not tokens:
        return None
    url, tail = tokens[0], tokens[1:]

    daemon_id: str | None = None
    caps_parts: list[str] = []
    for token in tail:
        if token.startswith("id=") and daemon_id is None:
            parsed = _as_uuid_or_none(token[3:])
            if parsed is not None:
                daemon_id = parsed
                continue
        caps_parts.append(token)

    caps_raw = " ".join(caps_parts)
    capabilities = tuple(
        c.strip() for c in caps_raw.split(",") if c.strip()
    )
    return DaemonEntry(
        name=name,
        url=url,
        daemon_id=daemon_id,
        capabilities=capabilities,
    )


def read_daemons_entries(path: str | Path | None = None) -> list[DaemonEntry]:
    """Read every well-formed entry. A missing file yields ``[]``."""
    target = Path(path).expanduser() if path is not None else daemons_file_path()
    if not target.exists():
        return []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[DaemonEntry] = []
    for raw in text.splitlines():
        entry = parse_daemons_line(raw)
        if entry is not None:
            out.append(entry)
    return out


def validate_url(url: str) -> None:
    """Raise :class:`DaemonsFileError` when a URL is not a WS endpoint."""
    if not url.startswith(_URL_SCHEMES):
        raise DaemonsFileError(
            "bad-url", "URL must start with ws:// or wss://"
        )


def validate_name(name: str) -> None:
    """Raise :class:`DaemonsFileError` when a name would break the format."""
    if not name:
        raise DaemonsFileError("name-required", "name is required")
    if not _NAME_RE.match(name):
        raise DaemonsFileError(
            "bad-name",
            "name may only contain letters, digits, dots, underscores "
            "and hyphens",
        )


def validate_daemon_id(value: str) -> str:
    """Return the canonical UUID form, or raise ``bad-id``.

    The reader only accepts UUIDs as ``id=`` (see the module docstring),
    so writing anything else would silently demote the id to a
    capability and leave the two sides computing different identities.
    """
    parsed = _as_uuid_or_none(value)
    if parsed is None:
        raise DaemonsFileError(
            "bad-id", f"daemon id must be a UUID, got {value!r}"
        )
    return parsed


def format_daemons_entry(entry: DaemonEntry) -> str:
    """Render an entry back to its single-line form."""
    validate_name(entry.name)
    validate_url(entry.url)
    parts = [entry.name, entry.url]
    if entry.daemon_id:
        parts.append(f"id={validate_daemon_id(entry.daemon_id)}")
    if entry.capabilities:
        parts.append(",".join(entry.capabilities))
    return " ".join(parts)


def _atomic_write(target: Path, lines: list[str]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines)
    if body:
        body += "\n"
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(target)


def upsert_daemons_entry(
    entry: DaemonEntry,
    path: str | Path | None = None,
    *,
    replace_name: bool = False,
) -> str:
    """Insert or update one entry, preserving every other line verbatim.

    Returns ``"added"``, ``"updated"`` or ``"unchanged"``.

    Raises :class:`DaemonsFileError` with ``code`` ``duplicate-url`` when
    the URL is already present under a different name, and
    ``name-conflict`` when the name already maps to a different URL and
    ``replace_name`` is not set.
    """
    target = Path(path).expanduser() if path is not None else daemons_file_path()
    new_line = format_daemons_entry(entry)

    lines: list[str] = []
    if target.exists():
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise DaemonsFileError(
                "unreadable", f"cannot read {target}: {exc}"
            ) from exc

    out: list[str] = []
    changed = False
    matched_url = False
    for raw in lines:
        parsed = parse_daemons_line(raw)
        if parsed is None:
            out.append(raw)
            continue
        if parsed.url == entry.url:
            matched_url = True
            if (
                parsed.name == entry.name
                and parsed.daemon_id == entry.daemon_id
                and parsed.capabilities == entry.capabilities
            ):
                return "unchanged"
            if parsed.name != entry.name and not replace_name:
                raise DaemonsFileError(
                    "duplicate-url",
                    f"{entry.url} is already registered as {parsed.name}",
                )
            out.append(new_line)
            changed = True
            continue
        if parsed.name == entry.name:
            if not replace_name:
                raise DaemonsFileError(
                    "name-conflict",
                    f"{entry.name} is already registered with {parsed.url}",
                )
            out.append(new_line)
            changed = True
            continue
        out.append(raw)

    if matched_url:
        # The URL line was rewritten in place above.
        pass
    elif changed:
        pass
    else:
        out.append(new_line)

    _atomic_write(target, out)
    return "updated" if changed else "added"


def remove_daemons_entry(
    *,
    name: str | None = None,
    url: str | None = None,
    daemon_id: str | None = None,
    path: str | Path | None = None,
) -> DaemonEntry:
    """Delete the first entry matching any selector, keeping the rest.

    Selectors are matched independently — the first line matching any of
    them is removed. Pass exactly one in practice; ``name`` and ``url``
    are unique per file anyway.

    Raises :class:`DaemonsFileError` with ``code`` ``bad-selector`` when no
    selector is given and ``not-found`` when nothing matches.
    """
    if not (name or url or daemon_id):
        raise DaemonsFileError(
            "bad-selector", "one of name, url or daemon_id is required"
        )
    target = Path(path).expanduser() if path is not None else daemons_file_path()
    if not target.exists():
        raise DaemonsFileError("not-found", f"{target} does not exist")

    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DaemonsFileError("unreadable", f"cannot read {target}: {exc}") from exc

    out: list[str] = []
    removed: DaemonEntry | None = None
    for raw in lines:
        entry = parse_daemons_line(raw)
        if entry is None:
            out.append(raw)
            continue
        if removed is None and (
            (url is not None and entry.url == url)
            or (name is not None and entry.name == name)
            or (daemon_id is not None and entry.daemon_id == daemon_id)
        ):
            removed = entry
            continue
        out.append(raw)

    if removed is None:
        selector = url or name or daemon_id
        raise DaemonsFileError("not-found", f"no entry matches {selector!r} in {target}")

    _atomic_write(target, out)
    return removed


__all__ = [
    "DaemonEntry",
    "DaemonsFileError",
    "daemons_file_path",
    "format_daemons_entry",
    "parse_daemons_line",
    "read_daemons_entries",
    "remove_daemons_entry",
    "upsert_daemons_entry",
    "validate_daemon_id",
    "validate_name",
    "validate_url",
]
