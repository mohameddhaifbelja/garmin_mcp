"""Append-only JSONL audit log for Garmin write tools (DESIGN.md §12).

Every Garmin mutating tool calls :func:`record` with the tool name, the args
it received, and the result it produced. Each call appends one JSON line to
``settings.audit_log_path`` (default ``~/.fitness-mcp/logs/writes.jsonl``).
This is the only undo trail since the server is stateless.

Design choices
--------------
* ``src.config`` is imported lazily inside :func:`record` so the module does
  not require the four mandatory env vars at import time. Tests can pass an
  explicit ``path`` and never touch :mod:`src.config`.
* String values longer than 100 chars (anywhere in ``args`` / ``result``,
  including nested dicts and lists) are truncated to ``value[:100] + "..."``
  per the project-wide logging convention in ``CLAUDE.md``.
* ``json.dumps`` runs with ``default=str`` so an unexpected non-serialisable
  value (e.g. a ``Path``) degrades to its string form rather than crashing
  the appender — losing one log line on a write would lose the only undo
  trail.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

_TRUNCATE_AT = 100
_TRUNCATE_SUFFIX = "..."

_T = TypeVar("_T")


def _truncate(value: _T) -> _T | str | dict[str, object] | list[object]:
    """Recursively shrink long strings inside ``value``.

    Strings over :data:`_TRUNCATE_AT` chars are replaced with the first 100
    chars plus ``...``. Dicts and lists are walked element-by-element; other
    types pass through unchanged.
    """
    if isinstance(value, str):
        if len(value) > _TRUNCATE_AT:
            return value[:_TRUNCATE_AT] + _TRUNCATE_SUFFIX
        return value
    if isinstance(value, dict):
        return {key: _truncate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate(item) for item in value]
    return value


def _utc_now_iso_z() -> str:
    """Return ``datetime.now(UTC)`` as iso8601 with a ``Z`` suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def record(
    tool: str,
    args: dict[str, object],
    result: dict[str, object],
    path: Path | None = None,
) -> None:
    """Append one JSON line describing a Garmin write to the audit log.

    Parameters
    ----------
    tool:
        The MCP tool name that performed the write (e.g. ``"create_and_schedule"``).
    args:
        The arguments the tool received. Long string values are truncated.
    result:
        The result the tool returned. Long string values are truncated.
    path:
        Optional override for the log file path. Defaults to
        ``settings.audit_log_path`` resolved lazily so that test suites do not
        need to populate the required env vars consumed by :mod:`src.config`.

    Side effects
    ------------
    Creates the parent directory on first call. After each successful append
    the parent directory is chmod'd to ``0o600`` per DESIGN.md §12 / the
    ticket's AC. The directory is temporarily relaxed to ``0o700`` for the
    duration of the write so the appender (running as the owner) can re-enter
    it on subsequent calls.
    """
    if path is None:
        from src.config import settings

        path = settings.audit_log_path

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Owner needs the execute bit to enter the directory and write the file;
    # we lock it down to 0o600 once the write completes (see below).
    parent.chmod(0o700)

    payload = {
        "ts": _utc_now_iso_z(),
        "tool": tool,
        "args": _truncate(args),
        "result": _truncate(result),
    }

    line = json.dumps(payload, default=str, ensure_ascii=False)
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    finally:
        parent.chmod(0o600)
