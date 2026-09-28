#!/usr/bin/env bash
# Build the operations-manager + sample-mcp-servers + ui images and
# deploy AOM onto a single-node K3s (or Rancher-managed K3s) cluster, with
# the dashboard on port 3111 of the node's IP (see
# helm/couchbase-agent-operations-manager/values-k3s.yaml).
#
# IMPORTANT: this script builds Docker images and imports them straight
# into K3s's own containerd image store (`k3s ctr images import`), so it
# must run ON the K3s node itself - not on your laptop - unless you push to
# a registry both machines can reach instead (see the REGISTRY option
# below).
#
# Usage (run on the K3s node):
#   ./deploy/deploy.sh
#
# Env vars you can override:
#   IMAGE_TAG   image tag to build/deploy (default: k3s)
#   NAMESPACE   Kubernetes namespace (default: agent-ops)
#   RELEASE     Helm release name (default: aom - matches
#               values-k3s.yaml's fullnameOverride, so don't change one
#               without the other)
#   REGISTRY    if set, push to this registry instead of importing
#               directly into containerd (e.g. REGISTRY=localhost:5000)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

IMAGE_TAG="${IMAGE_TAG:-k3s}"
NAMESPACE="${NAMESPACE:-agent-ops}"
RELEASE="${RELEASE:-aom}"
OPS_MANAGER_IMAGE="couchbase-aom-operations-manager"
MCP_SERVERS_IMAGE="couchbase-aom-sample-mcp-servers"
UI_IMAGE="couchbase-aom-ui"

echo "==> Building ${OPS_MANAGER_IMAGE}:${IMAGE_TAG} ..."
docker build -t "${OPS_MANAGER_IMAGE}:${IMAGE_TAG}" ./operations-manager
echo "==> Building ${MCP_SERVERS_IMAGE}:${IMAGE_TAG} ..."
docker build -t "${MCP_SERVERS_IMAGE}:${IMAGE_TAG}" ./sample-mcp-servers
echo "==> Building ${UI_IMAGE}:${IMAGE_TAG} ..."
docker build -t "${UI_IMAGE}:${IMAGE_TAG}" ./ui

if [ -n "${REGISTRY:-}" ]; then
  echo "==> Tagging and pushing to ${REGISTRY} ..."
  for img in "${OPS_MANAGER_IMAGE}" "${MCP_SERVERS_IMAGE}" "${UI_IMAGE}"; do
    docker tag "${img}:${IMAGE_TAG}" "${REGISTRY}/${img}:${IMAGE_TAG}"
    docker push "${REGISTRY}/${img}:${IMAGE_TAG}"
  done
  OPS_MANAGER_REPO="${REGISTRY}/${OPS_MANAGER_IMAGE}"
  MCP_SERVERS_REPO="${REGISTRY}/${MCP_SERVERS_IMAGE}"
  UI_REPO="${REGISTRY}/${UI_IMAGE}"
else
  echo "==> Importing images into K3s's containerd ..."
  docker save "${OPS_MANAGER_IMAGE}:${IMAGE_TAG}" | sudo k3s ctr images import -
  docker save "${MCP_SERVERS_IMAGE}:${IMAGE_TAG}" | sudo k3s ctr images import -
  docker save "${UI_IMAGE}:${IMAGE_TAG}" | sudo k3s ctr images import -
  OPS_MANAGER_REPO="${OPS_MANAGER_IMAGE}"
  MCP_SERVERS_REPO="${MCP_SERVERS_IMAGE}"
  UI_REPO="${UI_IMAGE}"
fi

# Pick up live LLM provider API keys from a local .env, if you've made
# one (`cp .env.example .env` then fill in a key - see .env.example).
# .env is gitignored and is ONLY read here for these three specific keys
# - it's the supported way to give this deployment a live key without
# putting it in values-k3s.yaml or a shell history
# entry. Re-run this script any time you change .env to push the new key.
#
# Deliberately NOT sourced as a shell file (`. ./.env`) - .env is copied
# from the full .env.example template, which has plenty of lines (other
# vars, comments, etc.) that aren't safe/valid to execute as bash. Pull
# just the one line per key instead.
get_env_var() {
  [ -f .env ] || return 0
  grep -E "^$1=" .env | tail -n1 | cut -d'=' -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"
}

PROVIDER_KEY_ARGS=()
ANTHROPIC_KEY="$(get_env_var ANTHROPIC_API_KEY)"
OPENAI_KEY="$(get_env_var OPENAI_API_KEY)"
GEMINI_KEY="$(get_env_var GEMINI_API_KEY)"
[ -n "$ANTHROPIC_KEY" ] && PROVIDER_KEY_ARGS+=(--set-string "operationsManager.providerApiKeys.anthropic=${ANTHROPIC_KEY}")
[ -n "$OPENAI_KEY" ] && PROVIDER_KEY_ARGS+=(--set-string "operationsManager.providerApiKeys.openai=${OPENAI_KEY}")
[ -n "$GEMINI_KEY" ] && PROVIDER_KEY_ARGS+=(--set-string "operationsManager.providerApiKeys.gemini=${GEMINI_KEY}")

echo "==> helm upgrade --install ${RELEASE} -n ${NAMESPACE} ..."
helm upgrade --install "${RELEASE}" ./helm/couchbase-agent-operations-manager \
  --namespace "${NAMESPACE}" --create-namespace \
  -f ./helm/couchbase-agent-operations-manager/values-k3s.yaml \
  --set operationsManager.image.repository="${OPS_MANAGER_REPO}" \
  --set operationsManager.image.tag="${IMAGE_TAG}" \
  --set sampleMcpServers.image.repository="${MCP_SERVERS_REPO}" \
  --set sampleMcpServers.image.tag="${IMAGE_TAG}" \
  --set ui.image.repository="${UI_REPO}" \
  --set ui.image.tag="${IMAGE_TAG}" \
  "${PROVIDER_KEY_ARGS[@]}" \
  "$@"

# Every build reuses the same image tag (:k3s by default), so Helm sees an
# unchanged pod spec and Kubernetes would keep running the OLD images
# indefinitely. Restart this release's Deployments (operations manager, UI,
# sample MCP servers) so they pick up what was just imported. Couchbase is a
# StatefulSet and is deliberately left alone.
echo "==> Restarting deployments so they run the freshly built images ..."
kubectl -n "${NAMESPACE}" rollout restart deployment -l "app.kubernetes.io/instance=${RELEASE}"
for d in $(kubectl -n "${NAMESPACE}" get deployment -l "app.kubernetes.io/instance=${RELEASE}" -o name); do
  kubectl -n "${NAMESPACE}" rollout status "$d" --timeout=300s || true
done

echo "==> Done. Status:"
kubectl get pods,svc -n "${NAMESPACE}"
echo
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)
echo "Dashboard (once the couchbase-init Job completes and pods are Ready):"
echo "  https://${NODE_IP:-<node-ip>}:3111"
