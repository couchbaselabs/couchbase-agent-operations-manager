{{/*
Base name of the chart, respecting nameOverride.
*/}}
{{- define "aom.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Full release name, respecting fullnameOverride, avoiding double-printing
the chart name when the release name already contains it.

Truncated to 40 (not the usual 63) for two stacked reasons, both found
by actually installing this chart (neither `helm lint` nor `helm
template` catches either one):

1. Every *Service/*ServiceName helper below appends a further component
   suffix on top of this value - the longest being "-operations-manager"
   / "-sample-mcp-servers" at 19 chars - and Kubernetes object names are
   capped at 63 bytes.
2. The couchbase StatefulSet's name is "<this>-couchbase" (+10), and
   Kubernetes' own StatefulSet controller then labels each pod with
   controller-revision-hash = "<statefulset-name>-<~10-char-hash>" - a
   SECOND, Kubernetes-injected suffix on top of ours, and label VALUES
   are also capped at 63 bytes. This is the binding constraint: this
   value + 10 ("-couchbase") + ~11 ("-<hash>") must stay <= 63, i.e.
   this value <= ~42. 40 leaves a couple of characters of margin.

Get this wrong and the symptom is confusing: the StatefulSet just never
creates pod-0 (`FailedCreate ... metadata.labels ... must be no more
than 63 bytes`), which leaves its PVC stuck in WaitForFirstConsumer
forever since nothing ever gets scheduled to consume it.
*/}}
{{- define "aom.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "aom.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "aom.labels" -}}
helm.sh/chart: {{ include "aom.chart" . }}
{{ include "aom.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "aom.selectorLabels" -}}
app.kubernetes.io/name: {{ include "aom.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Call with (merge (dict "component" "<name>") .) */}}
{{- define "aom.componentLabels" -}}
{{ include "aom.labels" . }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "aom.componentSelectorLabels" -}}
{{ include "aom.selectorLabels" . }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Call with (dict "root" $ "repository" <repo> "tag" <tag>).
Prefixes global.imageRegistry when set.
*/}}
{{- define "aom.image" -}}
{{- $registry := .root.Values.global.imageRegistry -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .repository .tag -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end -}}

{{- define "aom.authSecretName" -}}
{{- printf "%s-app-secrets" (include "aom.fullname" .) -}}
{{- end -}}

{{- define "aom.tlsSecretName" -}}
{{- if eq .Values.tls.mode "existingSecret" -}}
{{- .Values.tls.existingSecret -}}
{{- else -}}
{{- printf "%s-tls" (include "aom.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "aom.couchbaseServiceName" -}}
{{- printf "%s-couchbase" (include "aom.fullname" .) -}}
{{- end -}}

{{- /*
Resolves to the bundled couchbase Service's connection string
(couchbase://<service>) when couchbase.enabled is true, or to
operationsManager.couchbase.connectionString when it's false (external
Couchbase Enterprise server mode - see values.yaml).
*/ -}}
{{- define "aom.couchbaseConnectionString" -}}
{{- if .Values.couchbase.enabled -}}
{{- printf "couchbase://%s" (include "aom.couchbaseServiceName" .) -}}
{{- else -}}
{{- .Values.operationsManager.couchbase.connectionString -}}
{{- end -}}
{{- end -}}

{{- /*
Same idea as aom.couchbaseConnectionString above, but the bare hostname
used for REST calls (operations-manager's startup initContainer, and its
own Search/FTS requests) rather than the SDK connection string.
*/ -}}
{{- define "aom.couchbaseSearchHost" -}}
{{- if .Values.couchbase.enabled -}}
{{- include "aom.couchbaseServiceName" . -}}
{{- else -}}
{{- .Values.operationsManager.couchbase.searchHost -}}
{{- end -}}
{{- end -}}

{{- define "aom.sampleMcpServersServiceName" -}}
{{- printf "%s-sample-mcp-servers" (include "aom.fullname" .) -}}
{{- end -}}

{{- define "aom.operationsManagerServiceName" -}}
{{- printf "%s-operations-manager" (include "aom.fullname" .) -}}
{{- end -}}

{{- define "aom.uiServiceName" -}}
{{- printf "%s-ui" (include "aom.fullname" .) -}}
{{- end -}}

{{- /*
Whether the couchbase-init Job (and its ConfigMap) render: always for the
bundled Couchbase, and in external mode only when
couchbase.external.provision.enabled is true.
*/ -}}
{{- define "aom.couchbaseInitEnabled" -}}
{{- if .Values.couchbase.enabled -}}
true
{{- else if .Values.couchbase.external.provision.enabled -}}
true
{{- end -}}
{{- end -}}

{{- /* True when the external cluster is reached over TLS. */ -}}
{{- define "aom.couchbaseExternalTls" -}}
{{- if and (not .Values.couchbase.enabled) .Values.couchbase.external.tls -}}
true
{{- end -}}
{{- end -}}

{{- /* Hostname used for REST calls (management/query/search) in external mode. */ -}}
{{- define "aom.couchbaseExternalHost" -}}
{{- default .Values.operationsManager.couchbase.searchHost .Values.couchbase.external.host -}}
{{- end -}}

{{- /*
Base URL of the management REST API as the initContainers poll it: plain
8091 on the bundled service, https 18091 (or http 8091) on an external one.
*/ -}}
{{- define "aom.couchbaseMgmtUrl" -}}
{{- if .Values.couchbase.enabled -}}
http://{{ include "aom.couchbaseServiceName" . }}:8091
{{- else if .Values.couchbase.external.tls -}}
https://{{ include "aom.couchbaseExternalHost" . }}:18091
{{- else -}}
http://{{ include "aom.couchbaseExternalHost" . }}:8091
{{- end -}}
{{- end -}}

{{- /* curl flags for TLS trust against the external cluster. */ -}}
{{- define "aom.couchbaseCurlTlsFlags" -}}
{{- if include "aom.couchbaseExternalTls" . -}}
{{- if .Values.couchbase.external.tlsCaCert -}}
--cacert /etc/couchbase-ca/ca.pem
{{- else if .Values.couchbase.external.tlsInsecure -}}
--insecure
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "aom.couchbaseCaConfigMapName" -}}
{{- printf "%s-couchbase-ca" (include "aom.fullname" .) -}}
{{- end -}}
