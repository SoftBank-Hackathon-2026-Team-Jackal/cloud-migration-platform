{{- define "agent.name" -}}cmp-onprem-agent{{- end -}}
{{- define "agent.clusterName" -}}cmp-onprem-agent-{{ .Release.Namespace }}{{- end -}}
{{- define "agent.labels" -}}
app.kubernetes.io/name: cmp-onprem-agent
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
cmp.dev/managed: "true"
{{- end -}}
{{- define "agent.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}
{{- end -}}
