{{/*
Shared by every policy: match pods created or updated in the shopsearch
namespace, and collect all of their containers' images once.
*/}}
{{- define "policies.matchPods" -}}
matchConstraints:
  resourceRules:
    - apiGroups: [""]
      apiVersions: [v1]
      operations: [CREATE, UPDATE]
      resources: [pods]
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: shopsearch
{{- end -}}

{{- define "policies.containers" -}}
- name: containers
  expression: object.spec.containers + object.spec.?initContainers.orValue([])
{{- end -}}
