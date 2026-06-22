"""Per-unit backlight schedules, loaded from a ConfigMap-mounted directory.

Each file in the schedules dir is one entry: filename = unit id (matching the
client's ``UNIT_ID``) or ``default``, content = a schedule spec string (see
``client.schedule``). The server serves the raw spec for the requesting unit,
falling back to ``default``; the client parses/validates it (and falls back
further on its own). Loaded once at startup, so edits take effect on a rollout
restart — which is the documented update path.

The server intentionally does not parse the spec (that lives in the client
module): it serves the raw string and lets the client validate, so a bad entry
degrades to the client's fallback rather than failing the server.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DIR = "/etc/usage-dashboard/schedules"
DEFAULT_KEY = "default"


class ScheduleConfig:
    def __init__(self, entries: dict[str, str]) -> None:
        self._entries = entries

    def for_unit(self, unit_id: str | None) -> str | None:
        """The spec for *unit_id*, else the ``default`` entry, else None."""
        if unit_id and unit_id in self._entries:
            return self._entries[unit_id]
        return self._entries.get(DEFAULT_KEY)

    @classmethod
    def load(cls, directory: str | Path | None = None) -> "ScheduleConfig":
        path = Path(directory) if directory is not None else Path(_DEFAULT_DIR)
        entries: dict[str, str] = {}
        try:
            if path.is_dir():
                for entry in path.iterdir():
                    # ConfigMap volumes carry internal ..data/..timestamp links;
                    # the real keys are the non-dot entries (symlinks to files).
                    if entry.name.startswith("."):
                        continue
                    if entry.is_file():
                        spec = entry.read_text().strip()
                        if spec:
                            entries[entry.name] = spec
        except OSError as exc:
            logger.warning("failed reading schedules from %s: %s", path, exc)
        logger.info("loaded %d schedule entries from %s", len(entries), path)
        return cls(entries)
