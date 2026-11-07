#!/bin/sh
# Apply the committed cluster manifests, wait for the rollout, verify no drift.
#
# DELIBERATELY SKIPS k8s/server-secret.yaml: the committed file holds
# placeholders, and applying it would overwrite the live credential values
# with their committed placeholder values (this accident class is why
# scripts/verify_drift.py exists). Secret keys are provisioned out-of-band
# with `kubectl patch`; the drift check compares the key SET, never values.
#
# Requires kubectl pointed at the target cluster. The image itself is built
# and pushed to ghcr.io by GitHub Actions on push to main — this script only
# applies manifests; to pick up a new :main image without a manifest change:
#   kubectl -n usage-dashboard rollout restart deployment/usage-dashboard-server
set -eu
cd "$(dirname "$0")/../k8s"

kubectl apply -f namespace.yaml
kubectl apply -f server-pvc.yaml
kubectl apply -f server-service.yaml
kubectl apply -f server-schedules-configmap.yaml
kubectl apply -f server-deployment.yaml
kubectl apply -f server-ingress.yaml

kubectl -n usage-dashboard rollout status deployment/usage-dashboard-server --timeout=180s

echo "-- drift check --"
exec python3 "$(dirname "$0")/../scripts/verify_drift.py" --k8s-dir "$(pwd)"
