{{/*
Artea resource names are a FIXED contract (see values.yaml header): always
"artea-<component>", independent of the release name, mirroring the compose
fixed container names. One release per namespace. Subcharts pin the same shape
via fullnameOverride (artea-gitea, artea-verdaccio).
*/}}
{{- define "artea.fullname" -}}
{{- printf "artea-%s" . -}}
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

{{/* Image reference with digest pin taking precedence over tag; pass the image map */}}
{{- define "artea.image" -}}
{{- if .digest -}}
{{ printf "%s@%s" .repository .digest }}
{{- else -}}
{{ printf "%s:%s" .repository .tag }}
{{- end -}}
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
