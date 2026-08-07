from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.security import HTTPBearer

from usage_dashboard.server.api import (
    EXPOSURE_EXTERNAL,
    EXPOSURE_INTERNAL_ONLY,
    create_app,
    route_exposure,
)
from usage_dashboard.server.db import Database

REPO_ROOT = Path(__file__).resolve().parents[1]
INGRESS_MANIFEST = REPO_ROOT / "k8s" / "server-ingress.yaml"
EXTERNAL_INGRESS_NAME = "usage-dashboard-readings-ext"

# The external ingress is the public, bearer-protected surface. /dashboard is
# deliberately unauthenticated (the private-network phone view) and must never
# be reachable on it — a bare `/` prefix would expose it. The contract:
# every authed route is routed here, and no unauthed route is. Coverage is
# by Ingress Prefix semantics, so the single `/api` rule routes the whole
# versioned surface without the list having to grow. See
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


def _covered_by(path: str, prefixes: set[str]) -> bool:
    """True if an Ingress `Prefix` rule routes *path*.

    k8s Prefix matching is path-ELEMENT-wise, not string-wise: `/api` matches
    `/api` and `/api/v1/readings`, but must NOT match a hypothetical `/apikeys`.
    Comparing with `str.startswith` alone would wrongly report that covered and
    let a genuinely unrouted endpoint pass.
    """
    for prefix in prefixes:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def _iter_dependants(dependant):
    yield dependant
    for sub in getattr(dependant, "dependencies", None) or []:
        yield from _iter_dependants(sub)


def _exposure_split(app) -> tuple[set[str], set[str], set[str]]:
    """(declared-external, declared-internal-only, undeclared-but-authed).

    Exposure is read from the route's own declaration, never inferred from
    whether it is authenticated. An authenticated route that declares nothing
    lands in the third set and fails the contract — adding a route must force
    the decision, not inherit one.
    """
    external: set[str] = set()
    internal: set[str] = set()
    undeclared: set[str] = set()
    authed, _unauthed = _auth_split(app)
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path not in authed:
            continue
        declared = route_exposure(route)
        if declared == EXPOSURE_EXTERNAL:
            external.add(route.path)
        elif declared == EXPOSURE_INTERNAL_ONLY:
            internal.add(route.path)
        else:
            undeclared.add(route.path)
    return external, internal, undeclared


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
        external, _internal, _undeclared = _exposure_split(app)
        paths = _external_paths(INGRESS_MANIFEST.read_text())
        non_prefix = {p for p, t in paths.items() if t != "Prefix"}
        assert not non_prefix, (
            f"external ingress {EXTERNAL_INGRESS_NAME!r} has non-Prefix paths "
            f"{sorted(non_prefix)}; coverage below assumes Prefix semantics"
        )
        prefixes = set(paths)
        missing = {p for p in external if not _covered_by(p, prefixes)}
        assert not missing, (
            f"authenticated routes missing from external ingress "
            f"{EXTERNAL_INGRESS_NAME!r}: {sorted(missing)} — the Pi fleet would "
            f"404 on these until k8s/server-ingress.yaml is updated"
        )

    def test_no_unauthenticated_route_is_routed_externally(self, tmp_path):
        app = self._app(tmp_path)
        _authed, unauthed = _auth_split(app)
        paths = _external_paths(INGRESS_MANIFEST.read_text())
        leaked = {p for p in unauthed if _covered_by(p, set(paths))}
        assert not leaked, (
            f"unauthenticated routes exposed on external ingress "
            f"{EXTERNAL_INGRESS_NAME!r}: {sorted(leaked)} — /dashboard is the "
            f"private-network phone view and must stay off the public host"
        )


class TestPrefixCoverage:
    """Direct tests for `_covered_by`.

    The contract tests above exercise it only through the real route set, which
    happens not to contain a path that shares a string prefix with an ingress
    rule — so a regression to plain `str.startswith` passes them all. These
    pin the element-wise semantics k8s actually implements.
    """

    def test_prefix_covers_itself_and_descendants(self):
        assert _covered_by("/api", {"/api"})
        assert _covered_by("/api/v1/readings", {"/api"})
        assert _covered_by("/readings", {"/readings"})

    def test_prefix_does_not_cover_a_string_prefix_sibling(self):
        # The regression that matters: `/api` must NOT route `/apikeys`.
        # k8s Prefix matching is path-element-wise; `str.startswith` is not.
        assert not _covered_by("/apikeys", {"/api"})
        assert not _covered_by("/readings-internal", {"/readings"})
        assert not _covered_by("/apiv1/readings", {"/api"})

    def test_trailing_slash_on_the_rule_is_equivalent(self):
        assert _covered_by("/api/v1/readings", {"/api/"})
        assert not _covered_by("/apikeys", {"/api/"})

    def test_uncovered_path_with_no_matching_rule(self):
        assert not _covered_by("/dashboard", {"/api", "/readings"})
        assert not _covered_by("/api/v1/readings", set())


class TestExposureIsDeclaredNotInferred:
    """Plan 003 WP-3, open question 1 — settled 2026-08-07 in favour of
    declaring exposure per route.

    Inferring "authenticated => external" is default-allow: with one `/api`
    Prefix on the external ingress, a new authed route is public the instant it
    exists. `/history` was exposed that way without a decision. These tests make
    the declaration mandatory and make an internal-only route's exposure fail.
    """

    def _app(self, tmp_path):
        db = Database(str(tmp_path / "exposure.db"))
        db.initialize()
        return create_app("exposure-key", db)

    def test_every_authenticated_route_declares_its_exposure(self, tmp_path):
        _ext, _int, undeclared = _exposure_split(self._app(tmp_path))
        assert not undeclared, (
            f"authenticated routes with no exposure declaration: "
            f"{sorted(undeclared)} — add **EXTERNAL or **INTERNAL_ONLY. Exposure "
            f"must be a decision, not a default."
        )

    def test_declared_internal_only_routes_are_not_externally_covered(self, tmp_path):
        _ext, internal, _und = _exposure_split(self._app(tmp_path))
        prefixes = set(_external_paths(INGRESS_MANIFEST.read_text()))
        leaked = {p for p in internal if _covered_by(p, prefixes)}
        assert not leaked, (
            f"routes declared internal-only but reachable on the external "
            f"ingress: {sorted(leaked)}"
        )

    def test_the_guard_rejects_an_undeclared_authed_route(self):
        # Synthetic app: the mechanism must fail on an undeclared route even
        # though the real app currently has none, so the check cannot rot into
        # a no-op that passes because the codebase happens to be clean.
        from fastapi import Depends, FastAPI

        from usage_dashboard.server.api import _make_auth_dependency
        app = FastAPI()
        auth = _make_auth_dependency("k")

        @app.get("/api/v1/undeclared")
        async def undeclared_route(_u: str = Depends(auth)) -> dict[str, str]:
            return {}

        _ext, _int, undeclared = _exposure_split(app)
        assert undeclared == {"/api/v1/undeclared"}

    def test_the_guard_flags_an_internal_only_route_left_under_api(self):
        # The failure this design exists to prevent: someone adds an admin
        # endpoint, marks it internal-only, but mounts it under /api — which
        # the external ingress routes wholesale.
        from fastapi import Depends, FastAPI

        from usage_dashboard.server.api import INTERNAL_ONLY, _make_auth_dependency
        app = FastAPI()
        auth = _make_auth_dependency("k")

        @app.get("/api/v1/admin/reset", **INTERNAL_ONLY)
        async def admin_reset(_u: str = Depends(auth)) -> dict[str, str]:
            return {}

        _ext, internal, _und = _exposure_split(app)
        assert internal == {"/api/v1/admin/reset"}
        assert _covered_by("/api/v1/admin/reset", {"/api"}), (
            "an internal-only route under /api IS externally covered — this is "
            "exactly what the contract test must reject"
        )
