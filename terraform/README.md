# Terraform — AOM on AWS EKS, Azure AKS or GCP GKE

Terraform provisions the managed Kubernetes cluster, a private container
registry, networking with a **stable egress IP**, and a block-storage
StorageClass, then installs this repo's Helm chart
(`helm/couchbase-agent-operations-manager`) with `helm_release`. The chart
remains the single definition of what runs in the cluster; Terraform never
defines Kubernetes objects of its own, so Docker Compose, hal/k3s and the
three clouds all run the same chart and the same `init.sh`-derived
provisioning.

```
terraform/
├── modules/aom-release/   # helm_release + namespace + optional kube-prometheus-stack
│   ├── main.tf  variables.tf  outputs.tf
│   └── values.tftpl       # rendered chart values - every key exists in values.yaml
├── capella/               # Capella bucket, database credential, IP allowlist
├── aws/   azure/   gcp/   # cluster roots: main.tf  variables.tf  outputs.tf  terraform.tfvars.example
└── .gitignore
```

## Three Couchbase modes

| `couchbase_mode` | Couchbase runs… | What provisions the bucket/scope/indexes |
|---|---|---|
| `bundled` (default) | as the chart's single-node EE StatefulSet — dev/eval | `couchbase-init` hook Job, full mode (cluster init, quotas, bucket) |
| `enterprise` | on a Couchbase Server EE cluster you already operate | `couchbase-init` in external mode: creates the bucket if missing, then scope, collections, indexes; never touches cluster quotas |
| `capella` | as a Capella database | Terraform (`terraform/capella`) creates the bucket + credential + allowlist; `couchbase-init` provisions inside the bucket |

In both external modes the init Job runs on first install and idempotently
on every upgrade, writes `settings::provisioned` into the bucket, and
operations-manager's own startup self-healing still adds anything a newer
image needs. Node sizing can drop from 16 GiB to 8 GiB class without the
bundled Couchbase pod. Details: the `couchbase.external` block in
`values.yaml` and the header of `couchbase-init/init.sh`.

## What each root builds

| | AWS | Azure | GCP |
|---|---|---|---|
| Network | `terraform-aws-modules/vpc` v6, one NAT GW (static EIP) | VNet + Standard static `azurerm_public_ip` as AKS outbound IP | VPC, private nodes, Cloud NAT on a reserved address |
| Cluster | `terraform-aws-modules/eks` **v21** (`name`, `kubernetes_version`, AWS provider 6), EBS CSI via Pod Identity | `azurerm_kubernetes_cluster` + `aom` user pool, kubelet `AcrPull` | Standard regional GKE, Workload Identity, node SA with `artifactregistry.reader` |
| Nodes | `m6i.xlarge` ×3 | `Standard_D4s_v5` ×3 (+1 system) | `e2-standard-4` ×1 per zone |
| Registry | ECR `aom/couchbase-aom-*` | ACR `<acr>.azurecr.io/aom` | Artifact Registry `<region>-docker.pkg.dev/<project>/aom` |
| StorageClass | `gp3` (created, default) | `managed-csi` (built-in) | `standard-rwo` (built-in) |
| Dashboard | ui `LoadBalancer` :443 → `dashboard_url` output | same | same |

16 GiB nodes are for bundled mode: Couchbase's validated `6Gi` limit plus
operations-manager's torch + embedding models (`EMBEDDING_MAX_LOADED_MODELS=2`,
~2 GB each for the large ones).

## Run order

```bash
cd terraform/aws            # or azure / gcp
cp terraform.tfvars.example terraform.auto.tfvars   # gitignored; fill in
export TAG=$(git rev-parse --short HEAD)

# 1. cluster + registry only
terraform init
terraform apply -target=module.eks                              # aws
# terraform apply -target=azurerm_kubernetes_cluster_node_pool.aom  # azure
# terraform apply -target=google_container_node_pool.aom           # gcp

# 2. build + push the three images (linux/amd64 - the node pools are x86)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $(terraform output -raw registry | cut -d/ -f1)
# az acr login --name $(terraform output -raw registry | cut -d. -f1)
# gcloud auth configure-docker us-central1-docker.pkg.dev
for svc in operations-manager ui sample-mcp-servers; do
  docker build --platform linux/amd64 -t $(terraform output -raw registry)/couchbase-aom-$svc:$TAG ./$svc
  docker push $(terraform output -raw registry)/couchbase-aom-$svc:$TAG
done

# 3. everything, including the Helm release
terraform apply -var image_tag=$TAG

# 4. open it
$(terraform output -raw kubeconfig_command)
terraform output dashboard_url
```

Push images before step 3 or the pods sit in `ImagePullBackOff` until the
900 s `helm_release` timeout. First browser visit lands on the "Set the
admin password" screen; the baked-in certificate is self-signed until one
is installed (Settings → HTTPS Certificate, or `tls.existingSecret`).

`auth_secret_key` must stay stable across applies — rotating it logs
everyone out and makes every Fernet-encrypted secret (LDAP bind password,
SIEM tokens, imported-model keys) unreadable.

## External Couchbase Enterprise

```hcl
couchbase_mode = "enterprise"
external_couchbase = {
  host               = "cb.example.internal"
  tls                = true
  tls_ca_pem         = file("corp-ca.pem")   # private CA only
  provision_username = "Administrator"      # used only by the init Job (bucket creation)
  provision_password = "..."
}
couchbase_password = "<least-privilege user from README "Preparing an external cluster">"
```

Open 11207 and 18091–18094 from the node subnet to the cluster: AWS
security-group rule from `module.eks.node_security_group_id`; Azure NSG
rule from the `nodes` subnet prefix; GCP `google_compute_firewall` from
the node service account. The `egress_ip` output is what an on-prem
firewall should allow.

## Capella

```hcl
couchbase_mode          = "capella"
capella_api_token       = "..."
capella_organization_id = "..."
capella_project_id      = "..."
capella_cluster_id      = "..."   # existing cluster with Data, Index, Query AND Search
```

`terraform/capella` creates the `agent_operations` bucket (full eviction,
1 GiB), a Read/Write database credential and an allowlist entry for the
cluster's egress IP, and hands host + credential to the chart; the init Job
does the rest. The Capella provider has renamed attributes between minor
versions — run `terraform validate` against the pinned version before the
first apply. A private endpoint (PrivateLink / Private Link / PSC) is the
better production answer than a public allowlist; add it once the
public-IP path is proven.

## Monitoring

```hcl
monitoring = {
  install_stack          = true     # kube-prometheus-stack in namespace "monitoring"
  grafana_admin_password = "..."
}
```

Installs Prometheus Operator + Prometheus + Alertmanager + Grafana, and
turns on the chart's ServiceMonitor, PrometheusRule and Grafana dashboard
ConfigMap (the sidecar watches all namespaces). `terraform output
grafana_port_forward` prints the port-forward. Already have the Operator?
Use `service_monitor = true` and `release_label = "<its release name>"`
instead. Full detail in `MONITORING.md`.

## Things that go wrong, and the fast check

| Symptom | Likely cause | Check |
|---|---|---|
| `helm_release` times out, `couchbase-0` restarting | Node too small — OOMKilled at 6Gi | `kubectl describe pod … \| sed -n '/^Containers:/,/^Conditions:/p'` → `Exit Code: 137` |
| PVC stuck `WaitForFirstConsumer` | Pod never created — the 63-byte label trap | `kubectl describe statefulset` for `FailedCreate` |
| `ImagePullBackOff` | Images not pushed before apply, or pull permission | `kubectl describe pod` → `Failed to pull image` |
| `couchbase-init` Job fails at "waiting for the Query service" | Allowlist / firewall / wrong host for the external cluster | `kubectl logs job/aom-couchbase-init`; `egress_ip` output vs Capella allowlist |
| `couchbase-init` fails on bucket on Capella | Bucket missing — Capella can't create one from the cluster REST | `terraform state list \| grep capella_bucket` |
| Pages load but take seconds | Indexes still building on a large collection | wait, then re-check — see `scripts/sync-helm-couchbase-init.py` |
| ServiceMonitor "no matches for kind" | Prometheus Operator CRDs absent | `install_stack = true`, or leave `service_monitor` off |

## Deliberately not included

- Ingress / cert-manager / DNS — the chart's LoadBalancer on 443 works on
  all three clouds; an Ingress is a per-customer choice.
- Couchbase Autonomous Operator — use `enterprise` mode against an
  Operator-managed cluster instead.
- Backups — bundled mode is one PVC; use the cloud's disk snapshot policy.
  External EE and Capella bring their own.
- Cluster autoscaler — fixed node counts keep the first install predictable.
