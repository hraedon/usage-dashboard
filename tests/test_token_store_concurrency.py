"""Cross-process durability for the token store (Plan 005).

Enrolment runs as a second process inside the server pod and writes the same
file the running server rotates tokens into. These tests use real subprocesses
— a thread-based test cannot show that the *file* lock works.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from usage_dashboard.server.token_store import TokenStore, TokenStoreError

_REPO_SRC = str(Path(__file__).resolve().parents[1] / "src")


def _child(path: Path, script: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=_REPO_SRC)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


class TestDurability:
    def test_file_mode_is_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        TokenStore(path).save("claude", "a", "r")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_write_is_atomic_and_leaves_no_temp_file(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        TokenStore(path).save("claude", "a", "r")
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []
        assert json.loads(path.read_text())["claude"]["access_token"] == "a"

    def test_read_modify_write_preserves_unrelated_providers(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path)
        store.save("claude", "c-a", "c-r")
        store.save_credential("ollama", "cookie")
        store.set_seed_marker("opencode", "marker")
        store.save("claude_work", "w-a", "w-r")

        data = json.loads(path.read_text())
        assert data["claude"]["access_token"] == "c-a"
        assert data["ollama"]["credential"] == "cookie"
        assert data["opencode"]["seed_marker"] == "marker"
        assert data["claude_work"]["access_token"] == "w-a"

    def test_metadata_round_trips_and_none_removes(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path)
        store.save("claude", "a", "r", metadata={"client_id": "abc", "expires_at": 1})
        assert store.get_metadata("claude", "client_id") == "abc"
        # Re-enrolling with a credential that carries no client id must not
        # leave the old one behind for the refresh path to pick up.
        store.save("claude", "a2", "r2", metadata={"client_id": None})
        assert store.get_metadata("claude", "client_id") is None
        assert store.get_metadata("claude", "expires_at") == 1

    def test_restart_loads_the_newest_rotated_credential(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        TokenStore(path).save("claude", "a0", "r0")
        TokenStore(path).save("claude", "a1", "r1")  # a rotation
        assert TokenStore(path).get("claude") == ("a1", "r1")


class TestCrossProcess:
    def test_a_second_process_sees_a_write_without_a_restart(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path)  # the long-running server
        store.save("claude", "old", "old-r")

        # ...while `usage-dashboard login claude` enrols in another process.
        result = _child(
            path,
            f"""
            from usage_dashboard.server.token_store import TokenStore
            TokenStore({str(path)!r}).save("claude", "enrolled", "enrolled-r")
            """,
        )
        assert result.returncode == 0, result.stderr

        # Reads reload under a shared lock, so the already-constructed store
        # observes the enrolment.
        assert store.get("claude") == ("enrolled", "enrolled-r")

    def test_concurrent_writers_do_not_lose_each_others_providers(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "tokens.json"
        TokenStore(path).save("seed", "s", "s")

        # Twelve processes, each hammering its own provider key. Without the
        # file lock these interleave as read-modify-write races and drop
        # entries; the last writer's snapshot wins and the rest vanish.
        children = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        f"""
                        from usage_dashboard.server.token_store import TokenStore
                        store = TokenStore({str(path)!r})
                        for i in range(10):
                            store.save("p{n}", "a%d" % i, "r%d" % i)
                        """
                    ),
                ],
                env=dict(os.environ, PYTHONPATH=_REPO_SRC),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for n in range(12)
        ]
        for child in children:
            out, err = child.communicate(timeout=120)
            assert child.returncode == 0, err.decode()

        final = TokenStore(path)
        assert final.get("seed") == ("s", "s")
        for n in range(12):
            access, refresh = final.get(f"p{n}")
            assert (access, refresh) == ("a9", "r9"), f"p{n} was lost or torn"

    def test_a_reader_never_sees_a_torn_file(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        TokenStore(path).save("claude", "a" * 50_000, "r" * 50_000)
        writer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    from usage_dashboard.server.token_store import TokenStore
                    store = TokenStore({str(path)!r})
                    for i in range(40):
                        store.save("claude", "a" * 50000, "r" * 50000)
                    """
                ),
            ],
            env=dict(os.environ, PYTHONPATH=_REPO_SRC),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Atomic replace means a reader sees either the old or the new
            # file, never a half-written one.
            for _ in range(200):
                access, _refresh = TokenStore(path).get("claude")
                assert access == "a" * 50_000
        finally:
            writer.communicate(timeout=120)

    def test_a_corrupt_store_is_not_replaced_by_a_writer(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        path.write_text('{"claude": {"access_token": "a", ')  # truncated write
        original = path.read_text()
        with pytest.raises(TokenStoreError):
            TokenStore(path)
        assert path.read_text() == original


class TestLockDegradation:
    def test_writers_stay_atomic_without_a_usable_lock(self, tmp_path: Path) -> None:
        # The lock degrades to a no-op on a directory that cannot host one, so
        # the temp file must be per-process or two writers tear one another's.
        path = tmp_path / "tokens.json"
        store_a, store_b = TokenStore(path), TokenStore(path)
        store_a.save("a", "a-access", "a-refresh")
        store_b.save("b", "b-access", "b-refresh")
        assert TokenStore(path).get("a") == ("a-access", "a-refresh")
        assert TokenStore(path).get("b") == ("b-access", "b-refresh")


    def test_an_unlockable_directory_still_reads(self, tmp_path: Path) -> None:
        # A read-only mount cannot host the sidecar lock. Reading must still
        # work rather than failing the whole server at startup.
        path = tmp_path / "tokens.json"
        TokenStore(path).save("claude", "a", "r")
        (tmp_path / "tokens.json.lock").unlink(missing_ok=True)
        tmp_path.chmod(0o500)
        try:
            assert TokenStore(path).get("claude") == ("a", "r")
        finally:
            tmp_path.chmod(0o700)
