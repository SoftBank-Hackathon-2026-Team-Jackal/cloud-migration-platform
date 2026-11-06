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

{{/* Ticket 52 (D13): proxy variables in both spellings (Python urllib and Go/kubectl). */}}
{{- define "agent.proxyEnv" -}}
{{- with .Values.proxy }}
{{- if .https }}
- name: HTTPS_PROXY
  value: {{ .https | quote }}
- name: https_proxy
  value: {{ .https | quote }}
{{- end }}
{{- if .http }}
- name: HTTP_PROXY
  value: {{ .http | quote }}
- name: http_proxy
  value: {{ .http | quote }}
{{- end }}
{{- if or .https .http }}
- name: NO_PROXY
  value: {{ .noProxy | quote }}
- name: no_proxy
  value: {{ .noProxy | quote }}
{{- end }}
{{- end }}
{{- end }}
