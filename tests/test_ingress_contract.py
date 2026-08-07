from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.security import HTTPBearer

from usage_dashboard.server.api import create_app
from usage_dashboard.server.db import Database

REPO_ROOT = Path(__file__).resolve().parents[1]
INGRESS_MANIFEST = REPO_ROOT / "k8s" / "server-ingress.yaml"
EXTERNAL_INGRESS_NAME = "usage-dashboard-readings-ext"

# The external ingress is the public, bearer-protected surface. /dashboard is
# deliberately unauthenticated (the private-network phone view) and must never
# be reachable on it — a bare `/` prefix would expose it. The contract:
# every authed route is routed here, and no unauthed route is. See
# plans/003-versioned-api-and-ingress-contract.md (WI-024, Plan 003 WP-3).

# pyyaml is not a project dependency, so the manifest is parsed with a small,
# strict reader tuned to this file's shape (two `---`-separated Ingress docs,
# `path:` / `pathType:` pairs under `rules[].http.paths`). The bite-test in the
# WI-024 report verifies this reader actually inspects the manifest.
_DOC_SEP = re.compile(r"(?m)^---\s*$")
_META_NAME = re.compile(r"(?m)^metadata:\s*\n\s+name:\s*(\S+)\s*$")
_PATH_ENTRY = re.compile(r"(?m)^\s+-\s+path:\s*(\S+)\s*\n\s+pathType:\s*(\S+)")


def _external_paths(text: str) -> dict[str, str]:
    for doc in _DOC_SEP.split(text):
        name = _META_NAME.search(doc)
        if name and name.group(1) == EXTERNAL_INGRESS_NAME:
            return {path: ptype for path, ptype in _PATH_ENTRY.findall(doc)}
    raise AssertionError(
        f"{EXTERNAL_INGRESS_NAME!r} not found in {INGRESS_MANIFEST}; the "
        "external ingress contract cannot be checked without it"
    )


def _iter_dependants(dependant):
    yield dependant
    for sub in getattr(dependant, "dependencies", None) or []:
        yield from _iter_dependants(sub)


def _auth_split(app) -> tuple[set[str], set[str]]:
    # A route is authenticated iff its dependency tree carries the bearer
    # scheme (an HTTPBearer instance). Read from the live FastAPI app's
    # dependant graph rather than parsed from decorators/source, so renaming
    # or reordering the auth dependency cannot silently hide a route. If the
    # scheme changes, this returns an empty authed set and the tests fail
    # loudly — a change to the auth mechanism must force a contract re-check.
    authed: set[str] = set()
    unauthed: set[str] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        carries_bearer = any(
            isinstance(getattr(dep, "call", None), HTTPBearer)
            for dep in _iter_dependants(route.dependant)
        )
        (authed if carries_bearer else unauthed).add(route.path)
    return authed, unauthed


class TestIngressContract:
    def _app(self, tmp_path):
        db = Database(str(tmp_path / "ingress_contract.db"))
        db.initialize()
        return create_app("ingress-contract-key", db)

    def test_every_authenticated_route_is_routed_externally(self, tmp_path):
        app = self._app(tmp_path)
        authed, _unauthed = _auth_split(app)
        paths = _external_paths(INGRESS_MANIFEST.read_text())
        missing = authed - set(paths)
        assert not missing, (
            f"authenticated routes missing from external ingress "
            f"{EXTERNAL_INGRESS_NAME!r}: {sorted(missing)} — the Pi fleet would "
            f"404 on these until k8s/server-ingress.yaml is updated"
        )
        wrong_type = {p for p in authed if paths.get(p) != "Prefix"}
        assert not wrong_type, (
            f"authenticated routes not routed as Prefix on external ingress "
            f"{EXTERNAL_INGRESS_NAME!r}: {sorted(wrong_type)}"
        )

    def test_no_unauthenticated_route_is_routed_externally(self, tmp_path):
        app = self._app(tmp_path)
        _authed, unauthed = _auth_split(app)
        paths = _external_paths(INGRESS_MANIFEST.read_text())
        leaked = set(paths) & unauthed
        assert not leaked, (
            f"unauthenticated routes exposed on external ingress "
            f"{EXTERNAL_INGRESS_NAME!r}: {sorted(leaked)} — /dashboard is the "
            f"private-network phone view and must stay off the public host"
        )
