# Couchbase Agent Operations Manager - Helm chart

Deploys the same five-piece topology as the repository's
[`docker-compose.yml`](../../docker-compose.yml) onto Kubernetes: Couchbase
Server, a one-time provisioning step, the bundled sample MCP servers, the
operations-manager API, and the nginx-served dashboard. Read this whole
file before your first `helm install` - a couple of things necessarily work
differently here than they do under Docker Compose.

## Before you install

**Build the images first.** No prebuilt images are published: the three
custom images (`operations-manager`, `sample-mcp-servers`, `ui`) are built
from this repository and pushed to a registry your cluster can pull from.
Couchbase's own image (`couchbase:enterprise-7.6.2`) is public and needs no
build step.

```bash
# from the repository root - any registry your cluster can pull from
REGISTRY=registry.example.com/your-team
TAG=$(git rev-parse --short HEAD)

docker build -t $REGISTRY/couchbase-aom-operations-manager:$TAG ./operations-manager
docker build -t $REGISTRY/couchbase-aom-sample-mcp-servers:$TAG ./sample-mcp-servers
docker build -t $REGISTRY/couchbase-aom-ui:$TAG ./ui

docker push $REGISTRY/couchbase-aom-operations-manager:$TAG
docker push $REGISTRY/couchbase-aom-sample-mcp-servers:$TAG
docker push $REGISTRY/couchbase-aom-ui:$TAG
```

Then install with `--set global.imageRegistry=$REGISTRY` and the three
`*.image.tag` values set to `$TAG` (or set each `*.image.repository` to a
full path yourself - see `values.yaml`). Use a unique tag per build, as
above: re-using one tag means `helm upgrade` sees no change to the pod spec
and keeps running the old image.

On a single-node K3s cluster you can skip the registry and import the
images straight into K3s's containerd instead - see
[`deploy/deploy.sh`](../../deploy/deploy.sh), which does that.

## Install

```bash
helm install agent-ops ./helm/couchbase-agent-operations-manager \
  --namespace agent-ops --create-namespace
```

(Plus `--set global.imageRegistry=$REGISTRY` and the image tags from
"Before you install" above.)

Or point `-f` at a values file with your registry, credentials, and API
keys filled in - see the comments in `values.yaml` and
[LLM provider API keys](#llm-provider-api-keys) below. `helm install`
prints connection instructions (`NOTES.txt`) once it completes.

`helm uninstall agent-ops -n agent-ops` tears it down; add `--set
couchbase.persistence.size=...` etc. up front if you want more than the
20Gi/5Gi defaults for Couchbase data / the embedding-model cache.

## Monitoring

`operations-manager` serves Prometheus metrics on `/metrics`; the chart can
annotate its Service for scraping (default), render a `ServiceMonitor`,
`PrometheusRule` and a Grafana dashboard ConfigMap for kube-prometheus-stack
(`metrics.*` in `values.yaml`). See [MONITORING.md](../../MONITORING.md).
`terraform/` can install the whole stack next to the chart.

## Using an external Couchbase Enterprise server or Capella

By default this chart deploys and fully manages its own Couchbase Server
StatefulSet - `couchbase-init-job.yaml` runs the same cluster init/RAM
quota/bucket/scope/collection/index provisioning as `couchbase-init/init.sh`
does in Docker Compose, as a post-install/post-upgrade hook. To point the
appliance at a Couchbase Server Enterprise cluster or a Capella database you
already run instead, set `couchbase.enabled=false` and describe the server
under `couchbase.external` and `operationsManager.couchbase`.

**The `couchbase-init` Job still runs** in external mode
(`couchbase.external.provision.enabled`, default true), but in a
provisioning-only form that does nothing cluster-wide: it never runs
cluster init or touches RAM quotas, and everything it creates is inside the
appliance's own bucket, with `IF NOT EXISTS`, on first boot and again on
every `helm upgrade`:

| Step | Enterprise (`kind: enterprise`) | Capella (`kind: capella`) |
|---|---|---|
| Preflight: Enterprise Edition, Data + Index + Query + Search present | yes | yes (Search must be enabled on the database) |
| Create the bucket (`operationsManager.couchbase.bucket`) | if missing - needs an admin credential, see `provision.username` | never possible from the cluster API: create it in Capella first (`terraform/capella` does this) - the Job fails with a pointer if it's absent |
| Scope, collections | yes, via the Query service | yes |
| Primary + secondary GSI indexes | yes | yes |
| `settings::provisioned` marker document (what ran, when) | yes | yes |

The Search/vector indexes are created by operations-manager itself at
startup, as before, and its startup also re-creates any collection or
index the Job didn't (`couchbase_client.py`'s `_ensure_collections` /
`SECONDARY_INDEXES`), so the two paths agree.

Keep the credentials in a values file that stays out of git (see
[Keeping secrets off the command line](#keeping-secrets-off-the-command-line)):

```yaml
# secrets.values.yaml - add this file to .gitignore
operationsManager:
  couchbase:
    username: aom            # least-privilege user from the main README, or a Capella database credential
    password: <password>
couchbase:
  external:
    provision:
      username: Administrator   # enterprise only: an admin that may create the bucket
      password: <admin-password>
```

Self-managed Enterprise cluster:

```bash
helm install agent-ops ./helm/couchbase-agent-operations-manager \
  -f secrets.values.yaml \
  --set couchbase.enabled=false \
  --set couchbase.external.kind=enterprise \
  --set operationsManager.couchbase.connectionString=couchbases://cb.example.internal \
  --set operationsManager.couchbase.searchHost=cb.example.internal \
  --set-file couchbase.external.tlsCaCert=corp-ca.pem \
  --namespace agent-ops --create-namespace
```

Capella:

```bash
helm install agent-ops ./helm/couchbase-agent-operations-manager \
  -f secrets.values.yaml \
  --set couchbase.enabled=false \
  --set couchbase.external.kind=capella \
  --set operationsManager.couchbase.connectionString=couchbases://cb.abcd1234.cloud.couchbase.com \
  --set operationsManager.couchbase.searchHost=cb.abcd1234.cloud.couchbase.com \
  --namespace agent-ops --create-namespace
```

Notes:

- `couchbase.external.tls` defaults to true: the Job and the
  operations-manager initContainer talk to `https://<host>:18091` and
  `:18093`, and operations-manager's Search admin calls move to
  `https://<host>:18094` (`COUCHBASE_SEARCH_SCHEME`/`COUCHBASE_SEARCH_PORT`
  are set for you). The ports that must be reachable from the cluster are
  therefore **11207, 18091, 18093 and 18094** - on Capella that means the
  node pool's egress IP on the database's allowlist, or a private endpoint.
- `tlsCaCert` is for a private/corporate CA; leave it empty for Capella.
  `tlsInsecure: true` disables verification and is for a lab only.
- `provision.username/password` is the credential the Job uses. On
  enterprise, creating the bucket needs `cluster_admin` or `bucket_admin`,
  so give the Job an admin here and keep `operationsManager.couchbase.*` on
  the least-privilege user from the main README's
  [Preparing an external cluster](../../README.md#preparing-an-external-cluster).
  On Capella, one Read/Write database credential does everything - leave
  `provision.*` empty.
- A failed Job fails the install: `kubectl logs job/<release>-couchbase-init`
  names the step and the object. The usual first-boot failures are, in
  order, the allowlist/firewall (the Job times out reaching `:18093`
  after three minutes), a missing Capella bucket, and the Search service
  not enabled on the target.
- `couchbase.external.provision.enabled=false` turns the Job off entirely
  and restores the previous behaviour: you prepare the bucket and scope
  yourself and operations-manager creates the rest at startup.
- The `wait-for-dependencies` initContainer waits for the bucket and scope
  to exist on the external server before starting the main container, now
  over the same scheme and trust settings as the Job.

Switch back to the bundled StatefulSet at any time with
`--set couchbase.enabled=true` (and unset the `operationsManager.couchbase.connectionString`/`searchHost` overrides) on a `helm upgrade`.

To provision the cloud infrastructure around this chart - EKS, AKS or GKE,
the registries, and Capella buckets/credentials/allowlist - see
[`terraform/README.md`](../../terraform/README.md).

## LLM provider API keys

operations-manager's LLM caching gateway can forward cache misses to
Claude (Anthropic), ChatGPT (OpenAI) and Gemini (Google). Set the keys
under `operationsManager.providerApiKeys` in a values file (see the
example above). They're stored in the chart's `<release>-...-app-secrets`
Secret (`templates/secret-app.yaml`) and wired into the
operations-manager Pod as `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and
`GEMINI_API_KEY`, the same variables Docker Compose reads from `.env`.

- **All three are optional.** Set only the providers you'll use. A
  provider without a key answers cache misses from a labelled
  deterministic stub, so caching, the savings dashboard and invalidation
  all work with no outbound access - you can install first and add keys
  later. Databricks Model Serving works the same way:
  `providerApiKeys.databricks` plus `databricks.host` (and optionally
  `databricks.servingEndpoints`) - the token is ignored without the host.
- **Pass them on every upgrade.** Unlike the generated Couchbase password
  and agent API keys, provider keys aren't read back from the existing
  Secret: a `helm upgrade` without `-f secrets.values.yaml` (or
  `--reuse-values`) silently resets them to empty, and the providers drop
  back to the stub.
- **A configured key that's wrong fails loudly.** The gateway returns an
  error instead of caching made-up text, so a typo shows up on the first
  cache miss.
- **Adding or rotating a key later.** Update the value and `helm upgrade`,
  then restart the API so it picks up the new environment - keys are read
  at startup:

  ```bash
  helm upgrade agent-ops ./helm/couchbase-agent-operations-manager \
    -n agent-ops --reuse-values -f secrets.values.yaml
  kubectl rollout restart deployment -n agent-ops \
    -l app.kubernetes.io/instance=agent-ops,app.kubernetes.io/component=operations-manager
  ```

- **Checking it worked.** **LLM Caching -> Providers & Policy** in the
  dashboard shows which providers have a key configured (never the key
  itself; `GET /v1/llm/providers` returns the same `api_key_configured`
  flag). Send a test completion from the
  Providers & Policy page and confirm the answer doesn't start with
  `[offline stub - no ..._API_KEY configured]`.
- **Air-gapped clusters.** Leave the keys out. Real provider calls need
  outbound HTTPS from the operations-manager Pod to `api.anthropic.com`,
  `api.openai.com` and `generativelanguage.googleapis.com` - check your
  NetworkPolicies and egress proxy if a configured key times out.

## Keeping secrets off the command line

Anything passed with `--set` ends up in your shell history and in the
process list while Helm runs. Anything set through Helm values at all -
`--set` or `-f` - is also stored in the release, so anyone who can run
`helm get values agent-ops -n agent-ops` can read the Couchbase password
and the provider API keys. So:

- **Use a values file kept out of git** (`-f secrets.values.yaml`, as
  above) rather than `--set`, and limit who can read Secrets and run
  `helm get values` in the namespace with Kubernetes RBAC.
- **`--set` is fine on a laptop**, not for anything shared.
- **Bring-your-own Secret isn't supported yet.** The chart always renders
  and owns `<release>-...-app-secrets`, so a Secret created by External
  Secrets, Sealed Secrets or Vault would be overwritten on the next
  upgrade. Until the chart gains an `existingSecret` option, keys have to
  go through Helm values.

## What's identical to docker-compose.yml

- Every environment variable operations-manager reads (`config.py`) is
  wired the same way. The one difference is credentials: where
  `.env.example` ships demo values for local evaluation, the chart
  generates a random Couchbase admin password and random agent API keys on
  first install (see `NOTES.txt` for how to read them back).
- The same non-root UID (10001), dropped Linux capabilities, and
  `allowPrivilegeEscalation: false` on operations-manager and
  sample-mcp-servers; the same four capabilities
  (`NET_BIND_SERVICE, CHOWN, SETUID, SETGID`) added back on `ui` for the
  same reason (nginx's root master process needs them to chown its cache
  dirs and setuid/setgid into the unprivileged `nginx` user before
  forking workers).
- Couchbase's admin console/data ports are not exposed outside the
  cluster (the headless Service has no external ClusterIP) - use
  `kubectl port-forward` for admin console access, same tradeoff as
  compose's `127.0.0.1`-only port publishing.
- init.sh's actual provisioning logic (cluster init, bucket/scope/
  collection/index creation) is unchanged and just as idempotent.

## What's deliberately different, and why

**Provisioning runs as a Job, not an always-on idling container.**
`docker-compose.yml`'s `couchbase-init` service touches a sentinel file and
idles forever after provisioning finishes, purely so Docker Desktop's UI
doesn't show it as a stopped/unhealthy-looking container. That workaround
doesn't apply to Kubernetes: a Job that reaches `Completed` is already the
normal, expected-green state in every Kubernetes dashboard. This chart runs
init.sh as a `post-install,post-upgrade` Helm hook Job instead, with
`hook-delete-policy: before-hook-creation,hook-succeeded` so a fresh Job
replaces the old one on every `helm upgrade` (safe - init.sh is fully
idempotent).

**Dependency ordering uses an initContainer, not `depends_on` conditions.**
Kubernetes has no direct equivalent of compose's `depends_on: condition:
service_healthy` / `service_completed_successfully`. Left alone, a
Deployment starts its container the moment the Pod is scheduled - exactly
the race that made operations-manager crash with `ScopeNotFoundException`
before those conditions existed in compose. `operations-manager`'s Pod
carries an `initContainer` that polls the same readiness signals directly
(Couchbase's web console, then authenticated access, then the actual
scope existing; then sample-mcp-servers' health endpoint) before the main
container starts, using the operations-manager image itself (it already
has `curl` for its own Docker `HEALTHCHECK`) rather than adding a
Kubernetes-API-polling sidecar with its own RBAC.

**The shared TLS certificate is a generated Secret, not a shared volume.**
`docker-compose.yml` shares one certificate between `operations-manager`
and `ui` via a single named volume mounted read-write into both
containers. Kubernetes has no built-in equivalent of "one volume,
read-write, mounted into two different Pods" unless your cluster's default
StorageClass supports `ReadWriteMany` (NFS, EFS, Azure Files, Longhorn,
...) - most single-node/dev clusters (kind, minikube, Docker Desktop's own
Kubernetes) do **not** support this out of the box, so this chart doesn't
default to it. Instead (`tls.mode` in `values.yaml`):

- `generated` (default): the chart creates a self-signed certificate once
  on first install and stores it in a Kubernetes Secret, mounted
  **read-only** into both `operations-manager` and `ui`. Stable across
  `helm upgrade` (it reads the existing Secret back via Helm's `lookup`
  function instead of regenerating). Works on every cluster, no storage
  requirements.
- `existingSecret`: bring your own `kubernetes.io/tls` Secret (e.g. from
  cert-manager) via `tls.existingSecret`.

**Trade-off of both modes:** the in-app Settings → HTTPS Certificate
upload page (`operations-manager/app/user_auth.py`'s
`install_server_certificate()`) writes a new cert/key to its mount at
runtime, and a Secret-backed mount is read-only from inside the Pod, so
that upload flow will fail there with a permission/read-only-filesystem
error. For a Kubernetes deployment, install a real certificate by updating
the Secret (or your cert-manager `Certificate` resource) and rolling both
Deployments, instead of using the in-app upload page.

*If you specifically need the in-app upload page to work exactly like
compose's does*, and your cluster's StorageClass supports
`ReadWriteMany`: replace the Secret volumes in
`templates/operations-manager-deployment.yaml` and
`templates/ui-deployment.yaml` with a `ReadWriteMany` PVC mounted
read-write at `/app/tls` and `/etc/nginx/tls` respectively (drop the
`readOnly: true` and the `items` key-remapping, since real
`server.crt`/`server.key` files are expected at those paths rather than a
Secret's standard `tls.crt`/`tls.key` keys) - this wasn't built in by
default specifically because it would silently fail to schedule on the
common dev-cluster case above.

**operations-manager is not yet safe to scale beyond 1 replica.** Login
lockout tracking (`app/user_auth.py`) is in-process memory, not stored in
Couchbase, so multiple replicas would each track failed-login counts
independently instead of sharing state - a real gap, already called out in
the main repo's `SECURITY_HARDENING.md` - and the background sweepers,
monitors and memory consolidation assume a single instance. The Deployment
uses the `Recreate` strategy for the same reason. `operationsManager.replicaCount`
exists for future use, not to be raised today.

## Values reference

See the comments directly in `values.yaml` - every setting is documented
there. Credentials left blank (`operationsManager.couchbase.password`,
the three `operationsManager.apiKeys.*`) are generated on first install;
set anything under `operationsManager.providerApiKeys` you intend to use
(see [LLM provider API keys](#llm-provider-api-keys)).
