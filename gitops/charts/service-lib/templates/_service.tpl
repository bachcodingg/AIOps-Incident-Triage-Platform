{{/*
Everything one ShopSearch service needs. A service chart holds only values and a
one-line template that calls this:

  {{ include "service-lib.all" . }}
*/}}

{{- define "service-lib.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/part-of: shopsearch
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "service-lib.selector" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
{{- end }}

{{- define "service-lib.image" -}}
{{- .Values.image.repository }}:{{ .Values.image.tag }}
{{- with .Values.image.digest }}@{{ . }}{{ end }}
{{- end }}

{{- define "service-lib.all" -}}
{{- include "service-lib.serviceaccount" . }}
{{- if .Values.service.enabled }}
---
{{ include "service-lib.service" . }}
{{- end }}
---
{{ include "service-lib.rollout" . }}
---
{{ include "service-lib.pdb" . }}
---
{{ include "service-lib.hpa" . }}
---
{{ include "service-lib.podmonitor" . }}
---
{{ include "service-lib.analysis" . }}
{{- end }}

{{- define "service-lib.serviceaccount" -}}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ .Chart.Name }}
  labels:
    {{- include "service-lib.labels" . | nindent 4 }}
automountServiceAccountToken: false
{{- end }}

{{- define "service-lib.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ .Chart.Name }}
  labels:
    {{- include "service-lib.labels" . | nindent 4 }}
spec:
  type: {{ .Values.service.type }}
  selector:
    {{- include "service-lib.selector" . | nindent 4 }}
  ports:
    - name: http
      port: {{ .Values.service.port }}
      targetPort: {{ .Values.containerPort.name }}
      {{- with .Values.service.nodePort }}
      nodePort: {{ . }}
      {{- end }}
{{- end }}

{{- define "service-lib.rollout" -}}
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: {{ .Chart.Name }}
  labels:
    {{- include "service-lib.labels" . | nindent 4 }}
spec:
  # No `replicas`: the HorizontalPodAutoscaler owns it, and Git must not fight it.
  revisionHistoryLimit: 3
  selector:
    matchLabels:
      {{- include "service-lib.selector" . | nindent 6 }}
  # A release that never becomes ready (F01, a crash loop) aborts on its own.
  progressDeadlineSeconds: {{ .Values.rollout.progressDeadlineSeconds }}
  progressDeadlineAbort: true
  strategy:
    canary:
      maxSurge: 1
      maxUnavailable: 0
      analysis:
        templates:
          - templateName: {{ .Chart.Name }}
        # After the first setWeight, once a canary pod exists and is ready.
        startingStep: 1
        args:
          - name: canary-hash
            valueFrom:
              podTemplateHashValue: Latest
          - name: stable-hash
            valueFrom:
              podTemplateHashValue: Stable
      steps:
        - setWeight: 10
        - pause: {duration: {{ .Values.rollout.pauseSeconds }}s}
        - setWeight: 30
        - pause: {duration: {{ .Values.rollout.pauseSeconds }}s}
        - setWeight: 100
  template:
    metadata:
      labels:
        {{- include "service-lib.labels" . | nindent 8 }}
    spec:
      serviceAccountName: {{ .Chart.Name }}
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: {{ .Values.runAsUser }}
        seccompProfile:
          type: RuntimeDefault
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              {{- include "service-lib.selector" . | nindent 14 }}
      containers:
        - name: {{ .Chart.Name }}
          image: {{ include "service-lib.image" . | quote }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - name: {{ .Values.containerPort.name }}
              containerPort: {{ .Values.containerPort.port }}
          env:
            {{- range $name, $value := .Values.env }}
            - name: {{ $name }}
              value: {{ $value | quote }}
            {{- end }}
            {{- with .Values.extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          {{- with .Values.startupProbe }}
          startupProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          readinessProbe:
            {{- toYaml .Values.readinessProbe | nindent 12 }}
          livenessProbe:
            {{- toYaml .Values.livenessProbe | nindent 12 }}
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir:
            sizeLimit: 64Mi
{{- end }}

{{- define "service-lib.pdb" -}}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ .Chart.Name }}
  labels:
    {{- include "service-lib.labels" . | nindent 4 }}
spec:
  # maxUnavailable, not minAvailable: a single replica must not block a node drain.
  maxUnavailable: 1
  selector:
    matchLabels:
      {{- include "service-lib.selector" . | nindent 6 }}
{{- end }}

{{- define "service-lib.hpa" -}}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ .Chart.Name }}
  labels:
    {{- include "service-lib.labels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: argoproj.io/v1alpha1
    kind: Rollout
    name: {{ .Chart.Name }}
  minReplicas: {{ .Values.autoscaling.minReplicas }}
  maxReplicas: {{ .Values.autoscaling.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .Values.autoscaling.targetCPUUtilization }}
{{- end }}

{{- define "service-lib.podmonitor" -}}
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: {{ .Chart.Name }}
  labels:
    {{- include "service-lib.labels" . | nindent 4 }}
spec:
  selector:
    matchLabels:
      {{- include "service-lib.selector" . | nindent 6 }}
  # Copies the release hash onto every series, so a query can tell canary from stable.
  podTargetLabels:
    - rollouts-pod-template-hash
  podMetricsEndpoints:
    - port: {{ .Values.containerPort.name }}
      path: {{ .Values.metrics.path }}
      interval: 15s
{{- end }}

{{/*
Canary analysis. It fails a release that is worse than the one it replaces, and
not only one that breaks the SLO: fault F03 adds 250 ms, which still fits under
the 300 ms objective, so an absolute threshold alone would let it through.

Without a traffic router, Service load balancing decides who sees which request,
so canary and stable are told apart by the hash label on the series.
*/}}
{{- define "service-lib.analysis" -}}
{{- $canary := printf "namespace=%q,job=%q,rollouts_pod_template_hash=\"{{args.canary-hash}}\"" .Release.Namespace (printf "%s/%s" .Release.Namespace .Chart.Name) -}}
{{- $stable := printf "namespace=%q,job=%q,rollouts_pod_template_hash=\"{{args.stable-hash}}\"" .Release.Namespace (printf "%s/%s" .Release.Namespace .Chart.Name) -}}
{{- $a := .Values.analysis -}}
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: {{ .Chart.Name }}
  labels:
    {{- include "service-lib.labels" . | nindent 4 }}
spec:
  args:
    - name: canary-hash
    - name: stable-hash
  metrics:
    {{- if eq $a.mode "http" }}
    # No requests means the canary is not serving, which is a failure, not a pass.
    - name: traffic
      initialDelay: {{ $a.initialDelay }}
      interval: {{ $a.interval }}
      failureLimit: 1
      successCondition: result[0] > 0
      provider:
        prometheus:
          address: {{ $a.prometheusAddress }}
          query: sum(rate({{ $a.requestsMetric }}{ {{- $canary -}} }[1m])) or vector(0)
    - name: error-rate-increase
      initialDelay: {{ $a.initialDelay }}
      interval: {{ $a.interval }}
      failureLimit: 1
      successCondition: result[0] <= {{ $a.maxErrorRateIncrease }}
      provider:
        prometheus:
          address: {{ $a.prometheusAddress }}
          query: |
            (
              sum(rate({{ $a.requestsMetric }}{ {{- $canary -}} ,{{ $a.errorMatcher }}}[1m]))
              / sum(rate({{ $a.requestsMetric }}{ {{- $canary -}} }[1m]))
              or vector(0)
            ) - (
              sum(rate({{ $a.requestsMetric }}{ {{- $stable -}} ,{{ $a.errorMatcher }}}[1m]))
              / sum(rate({{ $a.requestsMetric }}{ {{- $stable -}} }[1m]))
              or vector(0)
            )
    - name: p95-latency
      initialDelay: {{ $a.initialDelay }}
      interval: {{ $a.interval }}
      failureLimit: 1
      successCondition: isNaN(result[0]) || result[0] <= {{ $a.maxP95Seconds }}
      provider:
        prometheus:
          address: {{ $a.prometheusAddress }}
          query: histogram_quantile(0.95, sum by (le) (rate({{ $a.bucketMetric }}{ {{- $canary -}} }[1m])))
    - name: p95-latency-increase
      initialDelay: {{ $a.initialDelay }}
      interval: {{ $a.interval }}
      failureLimit: 1
      # NaN means one side had no requests in the window, so nothing to compare.
      successCondition: isNaN(result[0]) || result[0] <= {{ $a.maxP95IncreaseSeconds }}
      provider:
        prometheus:
          address: {{ $a.prometheusAddress }}
          query: |
            histogram_quantile(0.95, sum by (le) (rate({{ $a.bucketMetric }}{ {{- $canary -}} }[1m])))
            - histogram_quantile(0.95, sum by (le) (rate({{ $a.bucketMetric }}{ {{- $stable -}} }[1m])))
    {{- else }}
    # A worker has no request stream to compare: it must stay up and not dead-letter.
    - name: up
      initialDelay: {{ $a.initialDelay }}
      interval: {{ $a.interval }}
      failureLimit: 1
      successCondition: result[0] > 0
      provider:
        prometheus:
          address: {{ $a.prometheusAddress }}
          query: sum(up{ {{- $canary -}} }) or vector(0)
    - name: dead-letters
      initialDelay: {{ $a.initialDelay }}
      interval: {{ $a.interval }}
      failureLimit: 0
      successCondition: result[0] == 0
      provider:
        prometheus:
          address: {{ $a.prometheusAddress }}
          query: sum(increase(indexer_changes_total{ {{- $canary -}} ,outcome="dead-lettered"}[5m])) or vector(0)
    {{- end }}
{{- end }}
