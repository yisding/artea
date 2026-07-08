{{/*
Artea resource names are a FIXED contract (see values.yaml header): always
"artea-<component>", independent of the release name, mirroring the compose
fixed container names. One release per namespace. Subcharts pin the same shape
via fullnameOverride (artea-gitea, artea-verdaccio).
*/}}
{{- define "artea.fullname" -}}
{{- printf "artea-%s" . -}}
{{- end -}}

{{/* Configured private package namespace: Gitea org and npm scope. */}}
{{- define "artea.privateNamespace" -}}
{{- $ns := default "artea" .Values.global.privateNamespace -}}
{{- if not (regexMatch "^[a-z0-9]([a-z0-9-]*[a-z0-9])?$" $ns) -}}
{{- fail "global.privateNamespace must be a lowercase npm/Gitea-safe name: [a-z0-9]([a-z0-9-]*[a-z0-9])?" -}}
{{- end -}}
{{- $ns -}}
{{- end -}}

{{/* Admin username defaults to <privateNamespace>-admin unless explicitly set. */}}
{{- define "artea.adminUsername" -}}
{{- default (printf "%s-admin" (include "artea.privateNamespace" .)) .Values.secrets.adminUsername -}}
{{- end -}}

{{/* Common labels; pass (dict "ctx" $ "component" "<name>") */}}
{{- define "artea.labels" -}}
app.kubernetes.io/name: artea
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .ctx.Chart.Name .ctx.Chart.Version }}
{{- end -}}

{{/* Selector labels; pass (dict "ctx" $ "component" "<name>") */}}
{{- define "artea.selectorLabels" -}}
app.kubernetes.io/name: artea
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
{{- end -}}

{{/* Fail fast on a missing or malformed image digest; pass the image map. Single
home for the digest contract, shared by artea.image (which emits the ref) and
templates/validations.yaml (which pre-validates every owned image). */}}
{{- define "artea.validateImageDigest" -}}
{{- if and .requireDigest (not .digest) -}}
{{- fail (printf "image %s requires .digest; set a sha256 digest or explicitly disable requireDigest for local/dev installs" .repository) -}}
{{- end -}}
{{- if and .digest (not (regexMatch "^sha256:[a-f0-9]{64}$" .digest)) -}}
{{- fail (printf "image %s has invalid .digest %q; expected sha256:<64 lowercase hex chars>" .repository .digest) -}}
{{- end -}}
{{- end -}}

{{/* Image reference with digest pin taking precedence over tag; pass the image map */}}
{{- define "artea.image" -}}
{{- include "artea.validateImageDigest" . -}}
{{- if .digest -}}
{{ printf "%s@%s" .repository .digest }}
{{- else -}}
{{ printf "%s:%s" .repository .tag }}
{{- end -}}
{{- end -}}

{{/* The container `image:` (quoted, digest-pinned via artea.image) + `imagePullPolicy:`
pair shared by every own-template workload; pass the image map (.Values.<c>.image). */}}
{{- define "artea.containerImage" -}}
image: {{ include "artea.image" . | quote }}
imagePullPolicy: {{ .pullPolicy }}
{{- end -}}

{{/* A single `valueFrom.secretKeyRef` env entry; pass
(dict "name" "<ENV>" "secret" "<fullname-component>" "key" "<secret-key>"). */}}
{{- define "artea.secretEnv" -}}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ include "artea.fullname" .secret }}
      key: {{ .key }}
{{- end -}}

{{/* ClusterIP Service exposing the named "http" port; pass
(dict "ctx" $ "component" "<c>" "port" <p> "type" "<optional, default ClusterIP>") */}}
{{- define "artea.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "artea.fullname" .component }}
  labels:
    {{- include "artea.labels" (dict "ctx" .ctx "component" .component) | nindent 4 }}
spec:
  type: {{ .type | default "ClusterIP" }}
  ports:
    - name: http
      port: {{ .port }}
      targetPort: http
      protocol: TCP
  selector:
    {{- include "artea.selectorLabels" (dict "ctx" .ctx "component" .component) | nindent 4 }}
{{- end -}}

{{/* liveness+readiness HTTP probes (10s) on the named "http" port; pass (dict "path" "<path>") */}}
{{- define "artea.httpProbes" -}}
livenessProbe:
  httpGet:
    path: {{ .path }}
    port: http
  periodSeconds: 10
readinessProbe:
  httpGet:
    path: {{ .path }}
    port: http
  periodSeconds: 10
{{- end -}}

{{/* filter-artea plugin config, shared verbatim by the verdaccio config.yaml
filters and middlewares roles (one package, two plugin instances that must stay
in sync). Rendered in the umbrella context (config.yaml is tpl'd there). */}}
{{- define "artea.filterArteaConfig" -}}
policy_url: {{ printf "%s/policy/npm-rules.yaml" (include "artea.policySyncUrl" .) }}
upstream_policy_url: {{ printf "%s/policy/upstream-policy.yaml" (include "artea.policySyncUrl" .) }}
osv_url: {{ printf "%s/osv/querybatch" (include "artea.policySyncUrl" .) }}
osv_cache_ttl_ms: {{ .Values.verdaccio.arteaPolicy.osvCacheTtlMs }}
redirect_public_tarballs: {{ .Values.verdaccio.arteaPolicy.redirectPublicTarballs }}
poll_interval_ms: {{ .Values.verdaccio.arteaPolicy.pollIntervalMs }}
fail_grace_ms: {{ .Values.verdaccio.arteaPolicy.failGraceMs }}
{{- end -}}

{{/* Cluster-internal URLs of the fixed Services (the cross-component contract) */}}
{{- define "artea.giteaUrl" -}}
http://artea-gitea-http:3000
{{- end -}}
{{- define "artea.devpiUrl" -}}
http://artea-devpi:3141
{{- end -}}
{{- define "artea.policySyncUrl" -}}
http://artea-policy-sync:8920
{{- end -}}
{{- define "artea.gatewayUrl" -}}
http://artea-gateway
{{- end -}}
