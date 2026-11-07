"""Cluster drift guard: the structural diff functions, on synthetic objects.

The kubectl calls are the thin, environment-bound half of
``scripts/verify_drift.py``; everything worth regression-testing is the pure
comparison logic, exercised here without a cluster. Two properties matter
beyond the diffs themselves: drift findings never contain credential values,
and the 2026-11-07 failure mode (manifest-declared env absent live) is a
first-class finding.

The script is not importable as a package module (``scripts/`` is not a
package), so it is loaded from its path — the same file CI would run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_drift.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_usage_dashboard_verify_drift", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vd = _load()


def _deployment(
    env: list[dict], image: str = "ghcr.io/hraedon/usage-dashboard-server:main"
) -> dict:
    return {
        "metadata": {"name": "usage-dashboard-server"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "server", "env": env, "image": image,
                         "imagePullPolicy": "Always"}
                    ]
                }
            }
        },
    }


class TestDeploymentEnvDrift:
    def test_manifest_env_absent_live_is_a_finding(self) -> None:
        # The 2026-11-07 AGENT_API_KEY failure mode: secret patched, manifest
        # declared the env, the live deployment never carried it.
        secret_ref = {"secretKeyRef": {"name": "server-secrets", "key": "api-key"}}
        manifest = _deployment([
            {"name": "API_KEY", "valueFrom": secret_ref},
            {"name": "AGENT_API_KEY", "valueFrom": {"secretKeyRef": {
                "name": "server-secrets", "key": "agent-api-key", "optional": True}}},
        ])
        live = _deployment([{"name": "API_KEY", "valueFrom": secret_ref}])
        findings = vd.diff_deployment(manifest, live)
        assert any("AGENT_API_KEY" in f and "absent live" in f for f in findings)

    def test_optional_flag_mismatch_is_wiring_drift(self) -> None:
        manifest = _deployment([
            {"name": "K", "valueFrom": {"secretKeyRef": {
                "name": "s", "key": "k", "optional": True}}},
        ])
        live = _deployment([
            {"name": "K", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
        ])
        findings = vd.diff_deployment(manifest, live)
        assert any("K" in f and "optional=False" in f for f in findings)

    def test_literal_value_difference_is_reported_without_values(self) -> None:
        manifest = _deployment([{"name": "DB_PATH", "value": "/data/readings.db"}])
        live = _deployment([{"name": "DB_PATH", "value": "/elsewhere/readings.db"}])
        findings = vd.diff_deployment(manifest, live)
        assert len(findings) == 1
        assert "value differs" in findings[0]
        assert "/elsewhere" not in findings[0]

    def test_live_only_env_and_absent_container_are_findings(self) -> None:
        manifest = _deployment([{"name": "A", "value": "1"}])
        live = _deployment([{"name": "A", "value": "1"}, {"name": "B", "value": "2"}])
        assert any("B" in f for f in vd.diff_deployment(manifest, live))
        assert vd.diff_deployment(manifest, {}) == [
            "deployment/usage-dashboard-server: absent from the cluster"
        ]
        findings = vd.diff_deployment(manifest, {"metadata": {"name": "x"}, "spec": {}})
        assert findings == ["deployment/usage-dashboard-server: container 'server' absent live"]

    def test_clean_state_yields_no_findings(self) -> None:
        env = [
            {"name": "API_KEY", "valueFrom": {"secretKeyRef": {"name": "s", "key": "api-key"}}},
            {"name": "DB_PATH", "value": "/data/readings.db"},
        ]
        assert vd.diff_deployment(_deployment(env), _deployment(env)) == []


class TestSecretKeyDrift:
    def test_missing_manifest_key_and_extra_live_key(self) -> None:
        manifest = {"metadata": {"name": "server-secrets"},
                    "stringData": {"api-key": "REDACTED", "agent-api-key": ""}}
        live = {"metadata": {"name": "server-secrets"},
                "data": {"api-key": "b64", "umans-api-key": "b64"}}
        findings = vd.diff_secret(manifest, live)
        assert any("agent-api-key" in f and "absent live" in f for f in findings)
        assert any("umans-api-key" in f and "WARNING" in f for f in findings)
        assert all("REDACTED" not in f for f in findings)

    def test_values_are_never_compared_or_printed(self) -> None:
        manifest = {"metadata": {"name": "s"}, "stringData": {"api-key": "manifest-value"}}
        live = {"metadata": {"name": "s"}, "data": {"api-key": "b64tbWFuaWZlc3QtdmFsdWU="}}
        assert vd.diff_secret(manifest, live) == []


class TestIngressAndConfigMap:
    def test_ingress_path_drift_both_directions(self) -> None:
        def ingress(paths: list[str]) -> dict:
            return {
                "metadata": {"name": "ext"},
                "spec": {
                    "tls": [{"hosts": ["usage.example"]}],
                    "rules": [{"host": "usage.example", "http": {"paths": [
                        {"path": p, "pathType": "Prefix",
                         "backend": {"service": {"name": "svc", "port": {"number": 8080}}}}
                        for p in paths
                    ]}}],
                },
            }

        manifest = ingress(["/api", "/readings"])
        live = ingress(["/api"])
        findings = vd.diff_ingress(manifest, live)
        assert any("/readings" in f and "not live" in f for f in findings)
        assert vd.diff_ingress(ingress(["/api"]), ingress(["/api"])) == []

    def test_configmap_value_drift_is_reported_without_values(self) -> None:
        manifest = {"metadata": {"name": "cm"}, "data": {"monday.json": "{\"a\": 1}"}}
        live = {"metadata": {"name": "cm"}, "data": {"monday.json": "{\"a\": 2}"}}
        findings = vd.diff_configmap(manifest, live)
        assert findings == ["configmap/cm: key 'monday.json' value differs (values not shown)"]


class TestServiceDefaults:
    def test_manifest_protocol_defaults_to_tcp_like_kubectl(self) -> None:
        # The committed manifest omits protocol; kubectl materializes the
        # TCP default live. That is not drift.
        manifest = {"metadata": {"name": "svc"},
                    "spec": {"ports": [{"port": 8080, "targetPort": 8080}]}}
        live = {"metadata": {"name": "svc"},
                "spec": {"ports": [{"port": 8080, "targetPort": 8080, "protocol": "TCP"}]}}
        assert vd.diff_service(manifest, live) == []


class TestManifestParsing:
    def test_single_concatenated_and_list_outputs(self) -> None:
        # _objects_from_manifest shells out; test the unwrap logic by
        # stubbing _run with representative kubectl outputs (shape varies
        # by kubectl version and document count).
        original_run = vd._run
        try:
            vd._run = lambda kubectl, args: '{"kind": "Secret", "metadata": {"name": "s"}}'  # type: ignore[method-assign]
            objs = vd._objects_from_manifest("kubectl", Path("x.yaml"))
            assert [o["kind"] for o in objs] == ["Secret"]

            two = ('{"kind": "Ingress", "metadata": {"name": "a"}}\n'
                   '{"kind": "Ingress", "metadata": {"name": "b"}}')
            vd._run = lambda kubectl, args: two  # type: ignore[method-assign]
            objs = vd._objects_from_manifest("kubectl", Path("x.yaml"))
            assert [o["metadata"]["name"] for o in objs] == ["a", "b"]

            wrapped = '{"kind": "List", "items": [{"kind": "Ingress"}, {"kind": "Ingress"}]}'
            vd._run = lambda kubectl, args: wrapped  # type: ignore[method-assign]
            objs = vd._objects_from_manifest("kubectl", Path("x.yaml"))
            assert [o["kind"] for o in objs] == ["Ingress", "Ingress"]
        finally:
            vd._run = original_run  # type: ignore[assignment]
