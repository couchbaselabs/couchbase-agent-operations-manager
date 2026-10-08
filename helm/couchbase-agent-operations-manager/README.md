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
keys filled in - see the comments in `values.yaml`. `helm install` prints connection instructions (`NOTES.txt`)
once it completes.

`helm uninstall agent-ops -n agent-ops` tears it down; add `--set
couchbase.persistence.size=...` etc. up front if you want more than the
20Gi/5Gi defaults for Couchbase data / the embedding-model cache.

## Using an external Couchbase Enterprise server

By default this chart deploys and fully manages its own Couchbase Server
StatefulSet - `couchbase-init-job.yaml` runs the same cluster init/RAM
quota/bucket/scope/collection/index provisioning as `couchbase-init/init.sh`
does in Docker Compose, as a post-install/post-upgrade hook. To point
operations-manager at a Couchbase Enterprise server you already run and
manage yourself instead:

```bash
helm install agent-ops ./helm/couchbase-agent-operations-manager \
  --set couchbase.enabled=false \
  --set operationsManager.couchbase.connectionString=couchbases://cb.example.internal \
  --set operationsManager.couchbase.searchHost=cb.example.internal \
  --set operationsManager.couchbase.username=<your-username> \
  --set operationsManager.couchbase.password=<your-password> \
  --set operationsManager.llm.anthropicApiKey=<sk-ant-...> \
  --set operationsManager.llm.openaiApiKey=<sk-...> \
  --set operationsManager.llm.geminiApiKey=<AIza...> \
  --namespace agent-ops --create-namespace
```

- `couchbase.enabled=false` skips the bundled StatefulSet, its headless
  Service, and the `couchbase-init` Job entirely - none of them get
  created. That Job's cluster-init/RAM-quota steps are cluster-wide
  operations, fine to run against a Couchbase node this chart owns
  outright and not something you want run against a cluster other
  workloads share, so external mode doesn't run it at all rather than
  trying to make it "safe" for someone else's cluster.
- `connectionString` needs whatever scheme your server actually requires
  - `couchbases://` (TLS) is typical for a real external Enterprise
    cluster, unlike the bundled StatefulSet's plain `couchbase://`.
- Create the bucket named in `operationsManager.couchbase.bucket` and,
  inside it, the scope named in `operationsManager.couchbase.scope` on
  that server yourself first - these two are the only things
  operations-manager doesn't create on its own. Every collection, its
  primary index, and the Search/vector indexes are created automatically
  at startup (see `operations-manager/app/couchbase_client.py`) - the
  same self-healing path that lets an appliance upgrade add a new
  collection without rerunning the provisioning Job.
- operations-manager's startup `initContainer` still waits for that
  bucket/scope to actually exist before the main container starts,
  whether Couchbase is the bundled StatefulSet or your external server -
  only *what* it waits on changes.

Switch back to the bundled StatefulSet at any time with
`--set couchbase.enabled=true` (and unset the `operationsManager.couchbase.connectionString`/`searchHost` overrides) on a `helm upgrade`.

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
set anything under `operationsManager.providerApiKeys` you intend to use.
