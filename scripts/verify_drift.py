"""Cluster drift guard: do the live objects still match the committed manifests?

The repo's contract (see the header of ``k8s/server-ingress.yaml``) is that the
committed manifests are *the artifact that is applied*. Twice that silently
stopped being true: the live ingress was missing ``/history`` until 2026-08-07
(WI-024), and on 2026-11-07 the live deployment lacked the ``AGENT_API_KEY`` env
entry the manifest declared — the secret key was patched out-of-band, the pod
came up without the variable, and every agent request 401'd until someone
thought to diff the env lists. The ingress contract test can only inspect the
committed file; it cannot see the cluster. This script can.

Structural comparison only, and **values are never printed**: env entries and
secret keys are compared by NAME and by *source wiring* (which secret, which
key, optional or not), never by credential value. A literal env value that
differs is reported as "value differs" without quoting either side — the
manifest is not the place a secret should hide, and a drift report must be
safe to paste into a PR.

Manifests are parsed with ``kubectl create --dry-run=client -o json`` (kubectl
is required to run this check anyway, and pyyaml is deliberately not a
dependency — see test_ingress_contract.py).

Run from a kubectl-configured box:

    python3 scripts/verify_drift.py [--k8s-dir k8s] [--namespace usage-dashboard]

Exit codes: 0 no drift, 1 drift found, 2 operational error (kubectl missing or
a manifest unreadable).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Kinds whose structural contract this guard enforces. Namespace and PVC are
# identity-only (their live fields are operator capacity knobs, not contract),
# and the secret is compared on key NAMES only — never values.
_KINDS_CHECKED = {"Deployment", "Secret", "Ingress", "ConfigMap", "Service"}


def _run(kubectl: str, args: list[str]) -> str:
    result = subprocess.run(
        [kubectl, *args], capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join([kubectl, *args])} failed: {result.stderr.strip() or 'no stderr'}"
        )
    return result.stdout


def _objects_from_manifest(kubectl: str, path: Path) -> list[dict[str, Any]]:
    """Parse one manifest file into objects, via kubectl's own YAML reader.

    Output shape varies by kubectl version and document count: a single JSON
    object, concatenated objects, or a ``kind: List`` wrapper — accept all.
    """
    stdout = _run(kubectl, ["create", "--dry-run=client", "-o", "json", "-f", str(path)])
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    pos = 0
    while pos < len(stdout):
        while pos < len(stdout) and stdout[pos] in " \t\r\n":
            pos += 1
        if pos >= len(stdout):
            break
        obj, pos = decoder.raw_decode(stdout, pos)
        if obj.get("kind") == "List":
            objects.extend(obj.get("items") or [])
        else:
            objects.append(obj)
    return objects


def _live_object(kubectl: str, kind: str, name: str, namespace: str) -> dict[str, Any] | None:
    stdout = _run(kubectl, ["get", kind, name, "-n", namespace, "-o", "json"])
    return json.loads(stdout)


def _env_source(entry: dict[str, Any]) -> str:
    """Normalize one env entry to its source wiring — never its value."""
    if "value" in entry:
        return "literal"
    source = entry.get("valueFrom") or {}
    if "secretKeyRef" in source:
        ref = source["secretKeyRef"]
        return (
            f"secret/{ref.get('name')}/{ref.get('key')}"
            f"/optional={bool(ref.get('optional'))}"
        )
    if "configMapKeyRef" in source:
        ref = source["configMapKeyRef"]
        return (
            f"configmap/{ref.get('name')}/{ref.get('key')}"
            f"/optional={bool(ref.get('optional'))}"
        )
    if "fieldRef" in source:
        return f"field/{source['fieldRef'].get('fieldPath')}"
    return "other"


def _service_port(service: dict[str, Any]) -> Any:
    port = service.get("port") or {}
    return port.get("number") or port.get("name")


def diff_deployment(manifest: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """Compare the manifest's container (env wiring, image, pull policy) to live."""
    findings: list[str] = []
    name = manifest["metadata"]["name"]
    if not live:
        return [f"deployment/{name}: absent from the cluster"]
    m_spec = (manifest.get("spec", {}).get("template", {}).get("spec") or {})
    l_spec = (live.get("spec", {}).get("template", {}).get("spec") or {})
    for m_container in m_spec.get("containers") or []:
        cname = m_container.get("name")
        l_container = next(
            (c for c in l_spec.get("containers") or [] if c.get("name") == cname), None
        )
        if l_container is None:
            findings.append(f"deployment/{name}: container {cname!r} absent live")
            continue
        m_env = {e["name"]: _env_source(e) for e in m_container.get("env") or []}
        l_env = {e["name"]: _env_source(e) for e in l_container.get("env") or []}
        for env_name in sorted(set(m_env) - set(l_env)):
            findings.append(
                f"deployment/{name}: env {env_name!r} declared in the manifest, "
                "absent live (pod env silently empty — see the 2026-11-07 AGENT_API_KEY drift)"
            )
        for env_name in sorted(set(l_env) - set(m_env)):
            findings.append(f"deployment/{name}: env {env_name!r} live but not in the manifest")
        for env_name in sorted(set(m_env) & set(l_env)):
            if m_env[env_name] != l_env[env_name]:
                findings.append(
                    f"deployment/{name}: env {env_name!r} source wiring differs: "
                    f"manifest={m_env[env_name]} live={l_env[env_name]}"
                )
            elif m_env[env_name] == "literal":
                m_value = next(
                    e["value"] for e in m_container["env"] if e["name"] == env_name
                )
                l_value = next(
                    e.get("value") for e in l_container["env"] if e["name"] == env_name
                )
                if m_value != l_value:
                    findings.append(
                        f"deployment/{name}: env {env_name!r} value differs (values not shown)"
                    )
        if m_container.get("image") != l_container.get("image"):
            findings.append(
                f"deployment/{name}: image differs: manifest={m_container.get('image')} "
                f"live={l_container.get('image')}"
            )
        if m_container.get("imagePullPolicy") != l_container.get("imagePullPolicy"):
            findings.append(f"deployment/{name}: imagePullPolicy differs")
    return findings


def diff_secret(manifest: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """Key NAMES only. Extra live keys are warnings: hand-provisioned keys are
    legitimate between manifest updates, but the manifest should catch up."""
    name = manifest["metadata"]["name"]
    if not live:
        return [f"secret/{name}: absent from the cluster"]
    m_keys = set(manifest.get("stringData") or {}) | set(manifest.get("data") or {})
    l_keys = set(live.get("data") or {}) | set(live.get("stringData") or {})
    findings = [
        f"secret/{name}: key {key!r} declared in the manifest, absent live"
        for key in sorted(m_keys - l_keys)
    ]
    findings.extend(
        f"secret/{name}: WARNING key {key!r} live but not in the manifest "
        "(reconcile the manifest)"
        for key in sorted(l_keys - m_keys)
    )
    return findings


def _ingress_signature(obj: dict[str, Any]) -> tuple[list[str], list[tuple[Any, ...]]]:
    hosts: list[str] = []
    for tls in obj.get("spec", {}).get("tls") or []:
        hosts.extend(tls.get("hosts") or [])
    paths: list[tuple[Any, ...]] = []
    for rule in obj.get("spec", {}).get("rules") or []:
        for p in (rule.get("http") or {}).get("paths") or []:
            service = (p.get("backend") or {}).get("service") or {}
            paths.append(
                (p.get("path"), p.get("pathType"), service.get("name"),
                 _service_port(service))
            )
    return sorted(hosts), sorted(paths)


def diff_ingress(manifest: dict[str, Any], live: dict[str, Any]) -> list[str]:
    name = manifest["metadata"]["name"]
    if not live:
        return [f"ingress/{name}: absent from the cluster"]
    findings: list[str] = []
    m_hosts, m_paths = _ingress_signature(manifest)
    l_hosts, l_paths = _ingress_signature(live)
    if m_hosts != l_hosts:
        findings.append(
            f"ingress/{name}: TLS hosts differ: manifest={m_hosts} live={l_hosts}"
        )
    if m_paths != l_paths:
        only_m = sorted(set(m_paths) - set(l_paths))
        only_l = sorted(set(l_paths) - set(m_paths))
        if only_m:
            findings.append(f"ingress/{name}: paths in manifest but not live: {only_m}")
        if only_l:
            findings.append(f"ingress/{name}: paths live but not in manifest: {only_l}")
    return findings


def diff_configmap(manifest: dict[str, Any], live: dict[str, Any]) -> list[str]:
    name = manifest["metadata"]["name"]
    if not live:
        return [f"configmap/{name}: absent from the cluster"]
    findings: list[str] = []
    m_data = manifest.get("data") or {}
    l_data = live.get("data") or {}
    for key in sorted(set(m_data) - set(l_data)):
        findings.append(f"configmap/{name}: key {key!r} declared in the manifest, absent live")
    for key in sorted(set(l_data) - set(m_data)):
        findings.append(f"configmap/{name}: key {key!r} live but not in the manifest")
    for key in sorted(set(m_data) & set(l_data)):
        if m_data[key] != l_data[key]:
            findings.append(f"configmap/{name}: key {key!r} value differs (values not shown)")
    return findings


def diff_service(manifest: dict[str, Any], live: dict[str, Any]) -> list[str]:
    name = manifest["metadata"]["name"]
    if not live:
        return [f"service/{name}: absent from the cluster"]

    def ports(obj: dict[str, Any]) -> list[tuple[Any, ...]]:
        return sorted(
            (p.get("port"), p.get("targetPort"), p.get("protocol") or "TCP")
            for p in obj.get("spec", {}).get("ports") or []
        )

    if ports(manifest) != ports(live):
        return [f"service/{name}: port declaration differs"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--k8s-dir", default="k8s", help="manifest directory (default: k8s)")
    parser.add_argument(
        "--namespace", default=None,
        help="cluster namespace (default: from the Namespace manifest)",
    )
    parser.add_argument("--kubectl", default="kubectl", help="kubectl binary (default: kubectl)")
    args = parser.parse_args(argv)

    k8s_dir = Path(args.k8s_dir)
    if not k8s_dir.is_dir():
        print(f"verify-drift: manifest directory not found: {k8s_dir}", file=sys.stderr)
        return 2

    try:
        namespace = args.namespace
        findings: list[str] = []
        for path in sorted(k8s_dir.glob("*.yaml")):
            for obj in _objects_from_manifest(args.kubectl, path):
                kind, name = obj.get("kind"), obj.get("metadata", {}).get("name")
                if kind == "Namespace" and namespace is None:
                    namespace = name
                    continue
                if kind not in _KINDS_CHECKED:
                    continue
                live = _live_object(args.kubectl, kind, name, namespace or "default")
                differ = {
                    "Deployment": diff_deployment,
                    "Secret": diff_secret,
                    "Ingress": diff_ingress,
                    "ConfigMap": diff_configmap,
                    "Service": diff_service,
                }[kind]
                findings.extend(differ(obj, live))
    except (RuntimeError, OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"verify-drift: operational error: {exc}", file=sys.stderr)
        return 2

    if findings:
        print("DRIFT between committed manifests and the live cluster:")
        for line in findings:
            print(f"  - {line}")
        return 1
    print("verify-drift: no structural drift between k8s/ and the cluster.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
