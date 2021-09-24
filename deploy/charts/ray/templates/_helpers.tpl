{{/*
Compute clusterMaxWorkers as the sum of per-pod-type max workers.
*/}}
{{- define "ray.clusterMaxWorkers" -}}
{{- $total := 0 }}
{{- range .Values.podTypes }}
{{- $total = add $total .maxWorkers }}
{{- end }}
{{- $total }}
{{- end }}

{{- define "coeus.nodeSelector" -}}
{{- range $key, $value := .Values.coeusConfig.nodeSelector }}
{{ $key }}: {{ $value }}
{{- end }}
{{- end -}}

{{- define "coeus.hostAliases" -}}
- hostnames:
  - "{{ .Values.coeusConfig.hostAliases.hostname }}"
  ip: "{{ .Values.coeusConfig.hostAliases.ip }}"
{{- end -}}

{{- define "coeus.imagePullSecrets" -}}
- name: "{{ .Values.coeusConfig.imagePullSecrets}}"
{{- end -}}

{{- define "coeus.annotations" -}}
{{- range $key, $value := .Values.coeusConfig.annotations }}
{{ $key }}: {{ $value }}
{{- end }}
{{- end -}}

{{- define "coeus.envs" -}}
{{- range $key, $value := .Values.coeusConfig.envs }}
- name: {{ $key }}
  value: {{ $value | quote}}
{{- end }}
{{- end -}}

{{- define "coeus.dnsConfig" -}}
dnsConfig:
  nameservers:
  - {{ .Values.coeusConfig.dnsServer }}
  - 10.68.200.244
  - 10.66.200.202
  options:
  - name: ndots
    value: "5"
  - name: timeout
    value: "1"
  searches:
  - {{ .Values.coeusConfig.searchNamespace }}.svc.paas.local
  - svc.paas.local
  - paas.local
  - host.bilibili.co
{{- end -}}