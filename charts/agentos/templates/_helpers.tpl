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

{{/* ---- Default-credential gate (issue #198) ----
     When security.checkDefaultCredentials is on, refuse to render if a Langfuse
     bootstrap identity still carries the published dev default from values.yaml.
     Unlike the nine store/control-plane secrets, these init identities seed the
     org/project on first boot (a different lifecycle), so #57 deliberately
     excludes them from its render-time gate; this closes that gap. The published
     admin password is a Langfuse admin-takeover risk on a reachable UI, and the
     project secret key also feeds the OTel Collector auth header. The operator
     clears the gate by overriding the value or supplying langfuse.existingSecret
     (the #169 secretKeyRef escape carries both keys).

     Off by default so the flagship zero-secret bare install stays green and the
     dev/e2e overlays render unchanged; flip it on for a shared/production
     cluster. #57 will fold the store/control-plane secrets into this same helper
     (hence the general name) once its design pass lands. */}}
{{- define "agentos.checkDefaultCredentials" -}}
{{- if .Values.security.checkDefaultCredentials -}}
{{- if not .Values.langfuse.existingSecret -}}
{{- if eq .Values.langfuse.init.projectSecretKey "sk-lf-agentos-dev" -}}
{{- fail "security.checkDefaultCredentials is on but langfuse.init.projectSecretKey is still the published dev default \"sk-lf-agentos-dev\". Override it (or set langfuse.existingSecret) before installing on a shared/production cluster -- this key also feeds the OTel Collector auth header." -}}
{{- end -}}
{{- if eq .Values.langfuse.init.userPassword "agentos-dev-password" -}}
{{- fail "security.checkDefaultCredentials is on but langfuse.init.userPassword is still the published dev default \"agentos-dev-password\". Override it (or set langfuse.existingSecret) before installing on a shared/production cluster -- the published admin password allows Langfuse admin takeover on a reachable UI." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* ---- Auto-generated per-release chart credential (issue #195) ----
     Resolve one chart-owned secret value, generating a strong random per release
     for a sealed install instead of shipping the published dev default. Call with
     a dict: root (the top context), key (the stringData key, matching an existing
     Secret's data), value (.Values.<path>), default (the published dev default),
     hex (true for the 64-hex encryption key, else false).

     The existing Secret's data is looked up ONCE by the caller (secrets.yaml) and
     passed in as `.existingData` (an always-present dict, empty under `helm
     template`/--dry-run/first install), so this helper does no per-key lookup.

     Four branches, in PRECEDENCE order, and WHY this order is correct:
       1. allowDevDefaults: the deterministic dev/CI escape hatch (values-dev.yaml
          sets it true). Return the value verbatim so the dev/e2e path renders the
          published defaults unchanged, byte-for-byte reproducible. Taking this
          first also means `--dev` reverts to the defaults even if a random was
          previously generated into the release Secret. Gate on positive equality
          against the literal "true" (`eq (toString ...) "true"`), NOT plain
          truthiness: Go templates treat any non-empty string as truthy, so a
          quoted `--set security.allowDevDefaults="false"` would otherwise read as
          truthy and ship the published default -- a fail-OPEN regression.
       2. Explicit override: if the operator/CLI supplied a value that differs from
          the published default (`ne value default`), it WINS -- even on `helm
          upgrade`. This is operator intent (a rotation, a recovery, a `--set`, or
          an `existingSecret`-equivalent value), so it must beat the persisted
          value; matches Bitnami's `providedPasswordValue`-first precedence. It
          MUST sit ahead of the persist branch or an explicit rotation on
          upgrade would be silently ignored.
       3. Persist existing: no override, so if a prior install already GENERATED
          this key, re-use it. `helm upgrade` must NEVER rotate a live store
          credential (Postgres would reject the new password against its persisted
          data), so we return the stored value from `.existingData` when present.
          Generated secrets always have value==published-default (nobody set them),
          so they never take branch 2 and always land here on upgrade -- exactly
          the "upgrade must not rotate" guarantee. `.existingData` is always a dict
          (the caller applies `| default dict`), empty under `helm
          template`/--dry-run and on first install, so a missing key falls through
          to generation.
       4. Generate: a first sealed install (value still equals the published
          default, no prior Secret) gets a strong random. `randAlphaNum` is
          crypto-backed (Sprig). hex=true hashes it to 64 lowercase-hex chars (the
          encryption key format); otherwise a 32-char alphanumeric.

     Net effect: an operator who forgets to re-pass `--set` on a later upgrade
     safely reverts value to the default, which then reuses the persisted generated
     value via branch 3 rather than rotating it. */}}
{{- define "agentos.managedSecret" -}}
{{- if eq (toString .root.Values.security.allowDevDefaults) "true" -}}{{/* string-coercion safety -- a quoted "false" must not read as truthy and silently ship a published default (fail closed to generation). */}}
{{- .value -}}
{{- else if ne (toString .value) (toString .default) -}}
{{- .value -}}
{{- else if hasKey .existingData .key -}}
{{- index .existingData .key | b64dec -}}
{{- else if .hex -}}
{{- randAlphaNum 32 | sha256sum -}}
{{- else -}}
{{- randAlphaNum 32 -}}
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

{{/* Platform-API connection env for the first-party services that CALL the API
     (today the dispatcher; the chart worker's identical gap is a tracked
     follow-up). Exists as a helper for the same reason agentos.env.postgres and
     agentos.env.valkey do: AGENTOS_API_BASE_URL has now been forgotten three
     times on new callers, while the store envs never recurred, because those had
     a helper to include and this did not. Wire a new API caller by including
     this rather than re-deriving the URL inline.

     The BYO override is .Values.dispatcher.apiBaseUrl. Note the deliberate
     absence of a `required` call for the api.deploy=false case that the sibling
     `X.host` helpers use: an empty override with api.deploy=false yields a
     CrashLoopBackOff by design (documented in NOTES.txt and the README), not a
     render-time failure. Include with `nindent 12` to land at a container's env
     column. */}}
{{- define "agentos.env.api" -}}
# Where the platform API lives. The dispatcher POSTs an approval
# resolve here when someone clicks Approve in Slack, so an unwired
# value means the click dead-ends: the code default
# http://localhost:8000 is, inside this pod, the dispatcher itself.
# Empty dispatcher.apiBaseUrl (the default) derives the in-chart API
# Service; a set value renders verbatim and is the BYO answer, and
# the only correct one when api.deploy is false. The port comes from
# api.service.port so the two sides cannot drift.
- name: AGENTOS_API_BASE_URL
  value: {{ .Values.dispatcher.apiBaseUrl | default (printf "http://%s-api:%v" (include "agentos.fullname" .) .Values.api.service.port) | quote }}
# The same chart Secret key api.yaml consumes as API_KEY, so the
# caller and the API cannot drift apart. By reference only: an inline
# value would put the shared platform key into `helm get manifest`
# output and into any rendered artifact CI uploads.
- name: AGENTOS_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "agentos.secretName" . }}
      key: apiKey
{{- end -}}

{{/* Heartbeat exec probes for the worker and dispatcher. Neither has an HTTP
     port, so an exec probe checks AGENTOS_HEARTBEAT_FILE freshness (< 30s)
     instead of hitting a port. Each Deployment sets its own heartbeat path via
     that env var, so the probe body is path-agnostic and both callers share
     identical timings -- the helper therefore takes no params. Include with
     `nindent 10` so the probe keys land at the container's 10-space column. */}}
{{- define "agentos.heartbeatProbes" -}}
readinessProbe:
  exec:
    command:
      - python
      - -c
      - |
        import os, sys, time
        p = os.environ["AGENTOS_HEARTBEAT_FILE"]
        sys.exit(0 if os.path.exists(p) and time.time() - os.path.getmtime(p) < 30 else 1)
  initialDelaySeconds: 10
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 3
livenessProbe:
  exec:
    command:
      - python
      - -c
      - |
        import os, sys, time
        p = os.environ["AGENTOS_HEARTBEAT_FILE"]
        sys.exit(0 if os.path.exists(p) and time.time() - os.path.getmtime(p) < 30 else 1)
  initialDelaySeconds: 30
  periodSeconds: 15
  timeoutSeconds: 5
  failureThreshold: 4
{{- end }}

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
     runner pod. off -> empty; require -> className; auto -> className when the
     chart itself creates the RuntimeClass (installRuntimeClass=true), otherwise
     only if the class is found by `lookup`. The installRuntimeClass shortcut
     exists because `lookup` cannot see the RuntimeClass the same install is about
     to create (nor anything under `helm template`/--dry-run), which would leave
     first-install runner pods with no runtimeClassName despite the chart
     guaranteeing the object. */}}
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
{{- else if $g.installRuntimeClass -}}
{{- $name -}}
{{- else -}}
{{- if lookup "node.k8s.io/v1" "RuntimeClass" "" $name -}}
{{- $name -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* ---- gVisor enforcement gate ----
     agentos.gvisor.preflightRequired: non-empty ("true") when the blocking
     gVisor enforcement preflight Job must render, else empty. It renders in
     `require` (always) and in `auto` WHEN the runner runs a real (non-fake)
     model -- i.e. untrusted agent code executes, so a missing/downgraded runsc
     RuntimeClass must fail the install CLOSED instead of silently landing on
     the host kernel. `auto` with the fake model (the bare-install default)
     still degrades gracefully with only a NOTES warning; `off` never renders.
     Real-model detection mirrors the AGENTOS_FAKE_MODEL gate in
     agent-sandbox.yaml (fake is in effect only when runner.fakeModel AND NOT
     inference.deploy), so real code runs when `(not fakeModel) OR inference.deploy`.
     Also respects security.gvisorPreflight.enabled and agentSandbox.deploy. */}}
{{- define "agentos.gvisor.preflightRequired" -}}
{{- $mode := .Values.security.gvisor.mode | default "auto" -}}
{{- $realModel := or (not .Values.agentSandbox.runner.fakeModel) .Values.inference.deploy -}}
{{- if and .Values.agentSandbox.deploy .Values.security.gvisorPreflight.enabled -}}
{{- if or (eq $mode "require") (and (eq $mode "auto") $realModel) -}}
true
{{- end -}}
{{- end -}}
{{- end -}}

{{/* ---- First-party image reference ----
     Render a fully-qualified image ref for a first-party (GHCR) workload,
     preferring an immutable content digest over a mutable tag. Call with a dict:
       repository  the image repo (e.g. ghcr.io/curie-eng/agentos-api)
       tag         optional explicit tag; empty falls back to defaultTag
       digest      optional "sha256:..." -- when set, wins and pins by digest
       defaultTag  the fallback tag when `tag` is empty (pass .Chart.AppVersion)
     - digest set -> "<repository>@sha256:..."  (fully immutable + verifiable)
     - else       -> "<repository>:<tag|defaultTag>"
     An empty tag defaulting to the chart appVersion is what makes a given chart
     version render a deterministic image ref (same chart version -> same ref,
     installable and rollback-able) without every install pinning a field.  */}}
{{- define "agentos.image" -}}
{{- $repo := required "image.repository is required" .repository -}}
{{- if .digest -}}
{{- printf "%s@%s" $repo .digest -}}
{{- else -}}
{{- printf "%s:%s" $repo (.tag | default .defaultTag | default "latest") -}}
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
