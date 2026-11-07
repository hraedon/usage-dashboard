"""The plan ledger (plans/00-INDEX.md) agrees with the plan files on disk.

Two branches claimed plan 007 independently and the collision cost a conflict
resolution at merge time; the parked capacity-watch branch's 008 collided the
same way after the renumber. The ledger exists so the next number is visible
before it is claimed; this test pins the file/row agreement so the ledger
cannot rot back into being decoration.
"""

from __future__ import annotations

import re
from pathlib import Path

PLANS_DIR = Path(__file__).resolve().parent.parent / "plans"
INDEX = PLANS_DIR / "00-INDEX.md"

_NUMBERED_FILE = re.compile(r"^(\d{3})-[a-z0-9-]+\.md$")
_ROW = re.compile(r"(?m)^\|\s*(\d+)\s*\|\s*\[.*?\]\((\d{3}-[a-z0-9-]+\.md)\)\s*\|")


def _numbered_files() -> dict[int, str]:
    files: dict[int, str] = {}
    for path in PLANS_DIR.glob("*.md"):
        match = _NUMBERED_FILE.match(path.name)
        if match:
            number = int(match.group(1))
            assert number not in files, f"duplicate plan number {number:03d} on disk"
            files[number] = path.name
    return files


def test_plan_numbers_are_unique_on_disk() -> None:
    # _numbered_files asserts inline; make the guarantee explicit here too.
    files = _numbered_files()
    assert len(files) == len(set(files)), "duplicate plan numbers on disk"


def test_every_plan_file_has_a_ledger_row() -> None:
    rows = {int(n): fname for n, fname in _ROW.findall(INDEX.read_text(encoding="utf-8"))}
    for number, fname in _numbered_files().items():
        assert rows.get(number) == fname, (
            f"plan {fname} is not the ledger row for {number:03d} "
            f"(row says {rows.get(number)!r}); claim numbers by adding the "
            "file AND the row in the same PR"
        )


def test_ledger_rows_point_at_existing_files() -> None:
    for number, fname in _ROW.findall(INDEX.read_text(encoding="utf-8")):
        assert (PLANS_DIR / fname).exists(), f"ledger row {number} -> missing {fname}"
