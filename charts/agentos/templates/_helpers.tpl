{{/*
Shared template helpers for the AgentOS umbrella chart.

Naming: every backing store's Service name is derived here so both the store's
own template and its consumers (Langfuse, the OTel Collector) agree. When a
store is BYO (`<dep>.deploy: false`), the helper returns the operator-supplied
host instead of the in-cluster Service name. This is the single-block BYO idiom
lifted from Langfuse's chart: flip `deploy` and fill `host` on the same block.
*/}}

{{- define "agentos.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agentos.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "agentos.labels" -}}
app.kubernetes.io/name: {{ include "agentos.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{/* Component selector labels. Pass a dict with "root" (the top context) and
     "component" (the component name). */}}
{{- define "agentos.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agentos.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Secret name that carries all credential material. */}}
{{- define "agentos.secretName" -}}
{{- printf "%s-secrets" (include "agentos.fullname" .) -}}
{{- end -}}

{{/* ---- Backing-store hosts (in-cluster Service name, or BYO host) ---- */}}

{{- define "agentos.postgres.host" -}}
{{- if .Values.postgres.deploy -}}
{{- printf "%s-postgres" (include "agentos.fullname" .) -}}
{{- else -}}
{{- required "postgres.deploy is false: set postgres.host to your external Postgres" .Values.postgres.host -}}
{{- end -}}
{{- end -}}

{{- define "agentos.valkey.host" -}}
{{- if .Values.valkey.deploy -}}
{{- printf "%s-valkey" (include "agentos.fullname" .) -}}
{{- else -}}
{{- required "valkey.deploy is false: set valkey.host to your external Valkey/Redis" .Values.valkey.host -}}
{{- end -}}
{{- end -}}

{{- define "agentos.clickhouse.host" -}}
{{- if .Values.clickhouse.deploy -}}
{{- printf "%s-clickhouse" (include "agentos.fullname" .) -}}
{{- else -}}
{{- required "clickhouse.deploy is false: set clickhouse.host to your external ClickHouse" .Values.clickhouse.host -}}
{{- end -}}
{{- end -}}

{{- define "agentos.minio.host" -}}
{{- if .Values.minio.deploy -}}
{{- printf "%s-minio" (include "agentos.fullname" .) -}}
{{- else -}}
{{- required "minio.deploy is false: set minio.host to your external S3-compatible endpoint" .Values.minio.host -}}
{{- end -}}
{{- end -}}

{{- define "agentos.langfuse.webHost" -}}
{{- printf "%s-langfuse-web" (include "agentos.fullname" .) -}}
{{- end -}}

{{/* base64("<publicKey>:<secretKey>") for the OTel Collector's Authorization
     header. Uses the operator override when set, otherwise derives it from the
     Langfuse init keys so the trace path authenticates with no manual step. */}}
{{- define "agentos.otlpAuthHeader" -}}
{{- if .Values.otelCollector.otlpAuthHeader -}}
{{- .Values.otelCollector.otlpAuthHeader -}}
{{- else -}}
{{- printf "Basic %s" (printf "%s:%s" .Values.langfuse.init.projectPublicKey .Values.langfuse.init.projectSecretKey | b64enc) -}}
{{- end -}}
{{- end -}}

{{/* ---- Shared first-party-app environment fragments ---- */}}

{{/* Postgres connection env for the app services. POSTGRES_PASSWORD comes from
     the Secret and DATABASE_URL is composed with $(POSTGRES_PASSWORD) so the
     password never lands in the rendered manifest. Both the API and the worker
     use the asyncpg driver and the dedicated `agentos` schema. */}}
{{- define "agentos.env.postgres" -}}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.postgres.existingSecret | default (include "agentos.secretName" .) }}
      key: postgresPassword
- name: DATABASE_URL
  value: postgresql+asyncpg://{{ .Values.postgres.auth.username }}:$(POSTGRES_PASSWORD)@{{ include "agentos.postgres.host" . }}:{{ .Values.postgres.port }}/{{ .Values.postgres.auth.database }}
- name: DB_SCHEMA
  value: agentos
{{- end -}}

{{/* Valkey connection env for the app services (host/port + password from the
     Secret). The apps build their own redis DSN from these parts. */}}
{{- define "agentos.env.valkey" -}}
- name: VALKEY_HOST
  value: {{ include "agentos.valkey.host" . | quote }}
- name: VALKEY_PORT
  value: {{ .Values.valkey.port | quote }}
- name: VALKEY_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.valkey.existingSecret | default (include "agentos.secretName" .) }}
      key: valkeyPassword
{{- end -}}

{{/* ---- Langfuse shared environment (mirrors compose.dev.yaml's
        x-langfuse-env anchor). Rendered into both web and worker. ---- */}}
{{- define "agentos.langfuse.env" -}}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "agentos.secretName" . }}
      key: postgresPassword
- name: DATABASE_URL
  value: postgresql://{{ .Values.postgres.auth.username }}:$(POSTGRES_PASSWORD)@{{ include "agentos.postgres.host" . }}:{{ .Values.postgres.port }}/{{ .Values.postgres.auth.database }}
- name: SALT
  valueFrom:
    secretKeyRef:
      name: {{ include "agentos.secretName" . }}
      key: langfuseSalt
- name: ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "agentos.secretName" . }}
      key: langfuseEncryptionKey
- name: TELEMETRY_ENABLED
  value: {{ .Values.langfuse.telemetryEnabled | quote }}
- name: LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES
  value: {{ .Values.langfuse.enableExperimentalFeatures | quote }}
- name: CLICKHOUSE_MIGRATION_URL
  value: clickhouse://{{ include "agentos.clickhouse.host" . }}:{{ .Values.clickhouse.nativePort }}
- name: CLICKHOUSE_URL
  value: http://{{ include "agentos.clickhouse.host" . }}:{{ .Values.clickhouse.httpPort }}
- name: CLICKHOUSE_USER
  value: {{ .Values.clickhouse.auth.username | quote }}
- name: CLICKHOUSE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "agentos.secretName" . }}
      key: clickhousePassword
- name: CLICKHOUSE_CLUSTER_ENABLED
  value: {{ .Values.clickhouse.clusterEnabled | quote }}
- name: REDIS_HOST
  value: {{ include "agentos.valkey.host" . }}
- name: REDIS_PORT
  value: {{ .Values.valkey.port | quote }}
- name: REDIS_AUTH
  valueFrom:
    secretKeyRef:
      name: {{ include "agentos.secretName" . }}
      key: valkeyPassword
- name: LANGFUSE_S3_EVENT_UPLOAD_BUCKET
  value: {{ .Values.minio.bucket | quote }}
- name: LANGFUSE_S3_EVENT_UPLOAD_REGION
  value: auto
- name: LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID
  value: {{ .Values.minio.auth.rootUser | quote }}
- name: LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "agentos.secretName" . }}
      key: minioRootPassword
- name: LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT
  value: http://{{ include "agentos.minio.host" . }}:{{ .Values.minio.port }}
- name: LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE
  value: "true"
- name: LANGFUSE_S3_EVENT_UPLOAD_PREFIX
  value: events/
- name: LANGFUSE_S3_MEDIA_UPLOAD_BUCKET
  value: {{ .Values.minio.bucket | quote }}
- name: LANGFUSE_S3_MEDIA_UPLOAD_REGION
  value: auto
- name: LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID
  value: {{ .Values.minio.auth.rootUser | quote }}
- name: LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "agentos.secretName" . }}
      key: minioRootPassword
- name: LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT
  value: http://{{ include "agentos.minio.host" . }}:{{ .Values.minio.port }}
- name: LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE
  value: "true"
- name: LANGFUSE_S3_MEDIA_UPLOAD_PREFIX
  value: media/
{{- end -}}

{{/* ---- gVisor tri-state (security.gvisor.mode: auto|require|off) ----

     agentos.gvisor.className: the RuntimeClass NAME to use/verify when gVisor is
     intended at all (empty only for mode=off). Deterministic (no cluster lookup);
     used by the enforcement preflight, the optional RuntimeClass object, and the
     probe's admission test.

     agentos.gvisor.runtimeClassName: the EFFECTIVE runtimeClassName to stamp on a
     runner pod. off -> empty; require -> className; auto -> className only if the
     RuntimeClass is found by `lookup`. `lookup` returns empty under
     `helm template`/--dry-run, so auto renders the no-gvisor shape there. */}}
{{- define "agentos.gvisor.className" -}}
{{- $g := .Values.security.gvisor -}}
{{- if eq ($g.mode | default "auto") "off" -}}
{{- else -}}
{{- $g.runtimeClassName | default "gvisor" -}}
{{- end -}}
{{- end -}}

{{- define "agentos.gvisor.runtimeClassName" -}}
{{- $g := .Values.security.gvisor -}}
{{- $mode := $g.mode | default "auto" -}}
{{- $name := $g.runtimeClassName | default "gvisor" -}}
{{- if eq $mode "off" -}}
{{- else if eq $mode "require" -}}
{{- $name -}}
{{- else -}}
{{- if lookup "node.k8s.io/v1" "RuntimeClass" "" $name -}}
{{- $name -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* ---- Dispatcher gating ----
     The Slack dispatcher only deploys when it has both tokens; without them it
     would crash-loop the reconnect supervisor forever, so a token-less default
     install skips the Deployment entirely (NOTES prints the connect command). */}}
{{- define "agentos.dispatcher.enabled" -}}
{{- if and .Values.dispatcher.deploy .Values.dispatcher.slack.appToken .Values.dispatcher.slack.botToken -}}
true
{{- end -}}
{{- end -}}
