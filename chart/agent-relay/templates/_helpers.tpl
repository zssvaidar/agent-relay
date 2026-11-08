{{- define "relay.labels" -}}
app.kubernetes.io/name: agent-relay
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{- define "relay.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end }}
