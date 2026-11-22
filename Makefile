# IncidentLens: local workflow on kind, plus the cloud session targets.
# Needs: docker, make, bash, curl. kind, kubectl and helm are downloaded into
# .bin/ if they are not already on PATH. The infra-* and cloud targets also need
# terraform and terragrunt; the cloud targets need the aws CLI as well.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

CLUSTER   ?= incidentlens
NAMESPACE ?= shopsearch
TAG       ?= dev
SERVICES  := gateway search-api catalog indexer
# Host ports for the gateway and Grafana. Fixed when the cluster is created:
# change them with `make down` and then `make up HOST_PORT=... GRAFANA_PORT=...`.
HOST_PORT    ?= 8080
GRAFANA_PORT ?= 3000

KIND_VERSION    ?= v0.33.0
KUBECTL_VERSION ?= v1.37.2
HELM_VERSION    ?= v4.3.1

# Branch ArgoCD follows. `make up` uses the current one, so it has to be pushed.
REVISION ?= $(shell b=$$(git rev-parse --abbrev-ref HEAD 2>/dev/null); [ "$$b" = HEAD ] || [ -z "$$b" ] && echo main || echo $$b)

# --- tool discovery -----------------------------------------------------------
UNAME_S := $(shell uname -s)
UNAME_M := $(shell uname -m)
ifneq (,$(findstring MINGW,$(UNAME_S))$(findstring MSYS,$(UNAME_S))$(findstring CYGWIN,$(UNAME_S)))
  OS  := windows
  EXE := .exe
else ifeq ($(UNAME_S),Darwin)
  OS  := darwin
else
  OS  := linux
endif
ifneq (,$(filter $(UNAME_M),arm64 aarch64))
  ARCH := arm64
else
  ARCH := amd64
endif

# Use kind/kubectl/helm from PATH when present, else download them into .bin/.
# PATH hits are quoted because on Windows they often live under "Program Files".
KIND_PATH    := $(shell command -v kind 2>/dev/null)
KUBECTL_PATH := $(shell command -v kubectl 2>/dev/null)
HELM_PATH    := $(shell command -v helm 2>/dev/null)
ifeq ($(KIND_PATH),)
  KIND := .bin/kind$(EXE)
  TOOL_DEPS += $(KIND)
else
  KIND := "$(KIND_PATH)"
endif
ifeq ($(KUBECTL_PATH),)
  KUBECTL := .bin/kubectl$(EXE)
  TOOL_DEPS += $(KUBECTL)
else
  KUBECTL := "$(KUBECTL_PATH)"
endif
ifeq ($(HELM_PATH),)
  HELM := .bin/helm$(EXE)
  TOOL_DEPS += $(HELM)
else
  HELM := "$(HELM_PATH)"
endif
K = $(KUBECTL) --context kind-$(CLUSTER) -n $(NAMESPACE)

.bin/kind$(EXE):
	@mkdir -p .bin
	curl -fsSLo $@ https://kind.sigs.k8s.io/dl/$(KIND_VERSION)/kind-$(OS)-$(ARCH)
	chmod +x $@

.bin/kubectl$(EXE):
	@mkdir -p .bin
	curl -fsSLo $@ https://dl.k8s.io/release/$(KUBECTL_VERSION)/bin/$(OS)/$(ARCH)/kubectl$(EXE)
	chmod +x $@

.bin/helm$(EXE):
	@mkdir -p .bin
ifeq ($(OS),windows)
	curl -fsSLo .bin/helm.zip https://get.helm.sh/helm-$(HELM_VERSION)-$(OS)-$(ARCH).zip
	unzip -qoj .bin/helm.zip $(OS)-$(ARCH)/helm.exe -d .bin
	rm .bin/helm.zip
else
	curl -fsSL https://get.helm.sh/helm-$(HELM_VERSION)-$(OS)-$(ARCH).tar.gz | tar -xz --strip-components=1 -C .bin $(OS)-$(ARCH)/helm
endif

# --- targets ------------------------------------------------------------------
.PHONY: help
help: ## Show this help
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-17s\033[0m %s\n", $$1, $$2}'

.PHONY: tools
tools: $(TOOL_DEPS) ## Download kind, kubectl and helm if missing

.PHONY: test
test: ## Run every service's unit tests inside Docker (same as CI)
	@for s in $(SERVICES); do \
	  echo "==> test $$s"; \
	  docker build --target test -t incidentlens/$$s:test services/$$s; \
	done

.PHONY: build
build: ## Build the runtime image of every service
	@for s in $(SERVICES); do \
	  echo "==> build $$s"; \
	  docker build --target runtime -t incidentlens/$$s:$(TAG) services/$$s; \
	done

.PHONY: cluster
cluster: tools ## Create the kind cluster if it does not exist
	@if $(KIND) get clusters 2>/dev/null | grep -qx '$(CLUSTER)'; then \
	  echo "kind cluster $(CLUSTER) already exists"; \
	else \
	  mkdir -p .bin; \
	  sed -e 's/hostPort: 8080/hostPort: $(HOST_PORT)/' -e 's/hostPort: 3000/hostPort: $(GRAFANA_PORT)/' \
	    infra/kind/cluster.yaml > .bin/kind-cluster.yaml; \
	  $(KIND) create cluster --name $(CLUSTER) --config .bin/kind-cluster.yaml --wait 120s; \
	fi
	@NODE=$(CLUSTER)-control-plane scripts/kind-inotify.sh

.PHONY: load
load: tools ## Load the built images into the kind node
	$(KIND) load docker-image --name $(CLUSTER) $(foreach s,$(SERVICES),incidentlens/$(s):$(TAG))

.PHONY: gitops
gitops: tools ## Install ArgoCD and hand the cluster to Git
	ENVIRONMENT=kind REVISION="$(REVISION)" LOCAL_IMAGES=true CONTEXT="kind-$(CLUSTER)" \
	  KUBECTL=$(KUBECTL) HELM=$(HELM) scripts/bootstrap-gitops.sh

.PHONY: wait
wait: tools ## Wait until ArgoCD reports everything Synced and Healthy
	CONTEXT="kind-$(CLUSTER)" KUBECTL=$(KUBECTL) scripts/wait-gitops.sh

.PHONY: up
up: ## Create cluster, build, load, sync through ArgoCD and smoke test
	@start=$$(date +%s); \
	$(MAKE) --no-print-directory cluster build load; \
	existed=$$($(K) get rollout gateway -o name 2>/dev/null || true); \
	$(MAKE) --no-print-directory gitops wait; \
	if [ -n "$$existed" ]; then $(MAKE) --no-print-directory restart; fi; \
	$(MAKE) --no-print-directory smoke; \
	echo "make up finished in $$(( $$(date +%s) - start ))s. Gateway: http://localhost:$(HOST_PORT), Grafana: http://localhost:$(GRAFANA_PORT)"

.PHONY: restart
restart: tools ## Restart the services to pick up new :dev images
	@now=$$(date -u +%Y-%m-%dT%H:%M:%SZ); \
	for s in $(SERVICES); do \
	  $(K) patch rollout $$s --type merge -p "{\"spec\":{\"restartAt\":\"$$now\"}}"; \
	done
	@$(MAKE) --no-print-directory wait

.PHONY: redeploy
redeploy: build load restart ## Rebuild images and restart the services

.PHONY: smoke
smoke: tools ## End-to-end check through the gateway, including the indexer queue
	GATEWAY="http://localhost:$(HOST_PORT)" KUBECTL=$(KUBECTL) CONTEXT="kind-$(CLUSTER)" NAMESPACE="$(NAMESPACE)" scripts/smoke.sh

.PHONY: obs-test
obs-test: tools ## Gate G1: metrics, a trace down to the database span, its logs, SLOs, dashboards
	GATEWAY="http://localhost:$(HOST_PORT)" KUBECTL=$(KUBECTL) CONTEXT="kind-$(CLUSTER)" scripts/obs-test.sh

.PHONY: grafana
grafana: tools ## Print the Grafana URL and admin password (anonymous users can view)
	@echo "Grafana: http://localhost:$(GRAFANA_PORT)  (cloud: kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80)"
	@echo "admin password: $$($(KUBECTL) --context kind-$(CLUSTER) -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d)"

.PHONY: alert-webhook
alert-webhook: tools ## Send SLO alerts to a Discord or Slack webhook: make alert-webhook URL=https://...
	@test -n "$(URL)" || { echo "usage: make alert-webhook URL=https://discord.com/api/webhooks/..."; exit 1; }
	@$(KUBECTL) --context kind-$(CLUSTER) -n monitoring create secret generic alert-webhook --from-literal=url='$(URL)' \
	  --dry-run=client -o yaml | $(KUBECTL) --context kind-$(CLUSTER) apply -f - >/dev/null
	@echo "stored; Alertmanager picks it up within a minute. For Slack set alerting.receiver: slack in gitops/observability/values.yaml"

.PHONY: canary-test
canary-test: tools ## Prove canary promotion, automatic abort and admission policies
	GATEWAY="http://localhost:$(HOST_PORT)" KUBECTL=$(KUBECTL) CONTEXT="kind-$(CLUSTER)" scripts/canary-test.sh

# --- chaos (chaos/README.md) ---------------------------------------------------
# chaos-runner lives in its own virtualenv under .bin/. Labels are written to
# ~/.incidentlens/labels, never into this repository (the AIOps layer reads it).
PYTHON ?= $(shell command -v python3 || command -v python)
CHAOS_VENV := .bin/chaos-venv
ifeq ($(OS),windows)
  CHAOS_PY := $(CHAOS_VENV)/Scripts/python.exe
else
  CHAOS_PY := $(CHAOS_VENV)/bin/python
endif
CHAOS = PYTHONPATH=chaos/runner $(CHAOS_PY) -m chaos_runner --context kind-$(CLUSTER) \
  --kubectl $(KUBECTL) --kind $(KIND) --kind-cluster $(CLUSTER) --base-tag $(TAG)

$(CHAOS_PY): chaos/runner/requirements.txt chaos/runner/requirements-dev.txt
	$(PYTHON) -m venv $(CHAOS_VENV)
	$(CHAOS_PY) -m pip install -q -r chaos/runner/requirements-dev.txt
	@touch $@

.PHONY: chaos-unit
chaos-unit: $(CHAOS_PY) ## Unit test chaos-runner and validate the fault catalog (no cluster)
	cd chaos/runner && "$(CURDIR)/$(CHAOS_PY)" -m pytest -q

.PHONY: chaos-list
chaos-list: $(CHAOS_PY) ## Show the fault catalog
	@$(CHAOS) list

.PHONY: chaos-test
chaos-test: tools $(CHAOS_PY) ## Dry run one fault: make chaos-test FAULT=F05 [TARGET=catalog] [INTENSITY=4]
	@test -n "$(FAULT)" || { echo "usage: make chaos-test FAULT=F05 [TARGET=...] [INTENSITY=...] [SCREENSHOT_DIR=...]"; exit 1; }
	GRAFANA_URL="http://localhost:$(GRAFANA_PORT)" $(CHAOS) inject $(FAULT) --check \
	  $(if $(TARGET),--target '$(TARGET)') $(if $(INTENSITY),--intensity '$(INTENSITY)') \
	  $(if $(SCREENSHOT_DIR),--on-symptom 'scripts/grafana-screenshot.sh "$(SCREENSHOT_DIR)"')

.PHONY: chaos-run
chaos-run: tools $(CHAOS_PY) ## Dataset run: every fault 5 times at random, about 70 h; sealed labels
	@mkdir -p eval/seals
	$(CHAOS) run --seal-out "eval/seals/$$(date -u +%Y%m%dT%H%M%SZ).json"

.PHONY: chaos-cleanup
chaos-cleanup: tools $(CHAOS_PY) ## Undo whatever an interrupted chaos run left behind
	$(CHAOS) cleanup

# --- detection and the dataset (aiops/README.md, eval/README.md) ----------------
# Recordings, models and labels live under ~/.incidentlens, never in this
# repository. Only the seals (eval/seals) and the dataset lock (eval/dataset)
# are committed. The recorder reads the cluster as the aiops service account.
AIOPS_VENV := .bin/aiops-venv
ifeq ($(OS),windows)
  AIOPS_PY := $(AIOPS_VENV)/Scripts/python.exe
  PATHSEP := ;
else
  AIOPS_PY := $(AIOPS_VENV)/bin/python
  PATHSEP := :
endif
AIOPS = PYTHONPATH="aiops$(PATHSEP)chaos/runner$(PATHSEP)eval" $(AIOPS_PY) -m
AIOPS_KUBE = --context kind-$(CLUSTER) --kubectl $(KUBECTL)

$(AIOPS_PY): aiops/requirements.txt aiops/requirements-dev.txt chaos/runner/requirements.txt
	$(PYTHON) -m venv $(AIOPS_VENV)
	$(AIOPS_PY) -m pip install -q -r aiops/requirements-dev.txt -r chaos/runner/requirements.txt
	@touch $@

.PHONY: aiops-unit
aiops-unit: $(AIOPS_PY) ## Unit test recorder, detectors, correlator, triage, remediator, loop and harness (no cluster, no model)
	cd aiops && "$(CURDIR)/$(AIOPS_PY)" -m pytest -q
	cd eval && "$(CURDIR)/$(AIOPS_PY)" -m pytest -q
	$(AIOPS) harness gate-check

.PHONY: record
record: tools $(AIOPS_PY) ## Record telemetry as the aiops account: make record NAME=baseline [HOURS=72]
	@test -n "$(NAME)" || { echo "usage: make record NAME=baseline [HOURS=72] [FROM=now-1h]"; exit 1; }
	$(AIOPS) recorder record $(AIOPS_KUBE) --name '$(NAME)' $(if $(HOURS),--hours $(HOURS)) $(if $(FROM),--from '$(FROM)')

# The recorder starts an hour and five minutes back (detection needs that much
# history before the first fault), runs while chaos-runner works through the
# catalog, and stops 25 minutes after the last recovery.
.PHONY: dataset-run
dataset-run: tools $(AIOPS_PY) $(CHAOS_PY) ## Record the dataset: chaos-run with the recorder alongside, about 70 h
	@name=dataset-$$(date -u +%Y%m%dT%H%M%SZ); mkdir -p .bin; \
	$(AIOPS) recorder record $(AIOPS_KUBE) --name "$$name" --from now-65m > ".bin/$$name.log" 2>&1 & rec=$$!; \
	trap 'kill $$rec 2>/dev/null || true' EXIT; \
	echo "recording $$name alongside the run (log: .bin/$$name.log)"; \
	$(MAKE) --no-print-directory chaos-run; \
	echo "run over; recording 25 more minutes of recovery"; sleep 1500; \
	echo "done. Next: make dataset-freeze RUN=<run id above> RECORDING=$$name MODEL=<baseline model>"

.PHONY: detector-train
detector-train: $(AIOPS_PY) ## Fit the Isolation Forests on a fault-free recording: make detector-train RECORDING=baseline
	@test -n "$(RECORDING)" || { echo "usage: make detector-train RECORDING=<fault-free recording>"; exit 1; }
	$(AIOPS) detector train --recording '$(RECORDING)'

.PHONY: detect
detect: $(AIOPS_PY) ## Alerts and B0/B1/B2 incidents for a recording: make detect RECORDING=... [MODEL=baseline]
	@test -n "$(RECORDING)" || { echo "usage: make detect RECORDING=<name> [MODEL=<model name>]"; exit 1; }
	$(AIOPS) detector run --recording '$(RECORDING)' $(if $(MODEL),--model '$(MODEL)')
	@for v in B0 B1 B2; do $(AIOPS) correlator --recording '$(RECORDING)' --variant $$v; done

.PHONY: detect-score
detect-score: $(AIOPS_PY) ## Detection metrics on the tuning set: make detect-score RUN=run-... RECORDING=...
	@test -n "$(RUN)" && test -n "$(RECORDING)" || { echo "usage: make detect-score RUN=<run id> RECORDING=<name>"; exit 1; }
	$(AIOPS) harness score --run '$(RUN)' --recording '$(RECORDING)'

# Week 8: triage. B2 (the rule baseline) is free; A1 to A3 call Claude and cost
# money (ANTHROPIC_API_KEY or an `ant auth login` profile); A4 and MODEL=qwen3:8b
# need a local Ollama. Runs land in eval/runs/<run>/tuning, scores next to the labels.
.PHONY: triage
triage: $(AIOPS_PY) ## Triage and score the tuning set: make triage RUN=... RECORDING=... [VARIANTS="B2 A1"] [SEEDS=0] [MODEL=] [LIMIT=]
	@test -n "$(RUN)" && test -n "$(RECORDING)" || { echo "usage: make triage RUN=<run id> RECORDING=<name> [VARIANTS=\"B2 A1\"] [SEEDS=\"0 1 2\"] [MODEL=qwen3:8b] [LIMIT=3]"; exit 1; }
	$(AIOPS) harness triage --run '$(RUN)' --recording '$(RECORDING)' --variant $(or $(VARIANTS),B2) --seeds $(or $(SEEDS),0) $(if $(MODEL),--model '$(MODEL)') $(if $(LIMIT),--limit $(LIMIT))

.PHONY: triage-one
triage-one: $(AIOPS_PY) ## Replay one incident: make triage-one RECORDING=... INCIDENT=inc-0007 [VARIANT=A1] [MODEL=]
	@test -n "$(RECORDING)" && test -n "$(INCIDENT)" || { echo "usage: make triage-one RECORDING=<name> INCIDENT=<id> [VARIANT=A1] [MODEL=qwen3:8b]"; exit 1; }
	$(AIOPS) triage run --recording '$(RECORDING)' --incident '$(INCIDENT)' --variant $(or $(VARIANT),A1) $(if $(MODEL),--model '$(MODEL)')

.PHONY: dataset-freeze
dataset-freeze: $(AIOPS_PY) ## Gate G2: split tuning/test, hash-lock the test set: make dataset-freeze RUN=... RECORDING=... MODEL=...
	@test -n "$(RUN)" && test -n "$(RECORDING)" || { echo "usage: make dataset-freeze RUN=<run id> RECORDING=<name> [MODEL=<name>]"; exit 1; }
	$(AIOPS) harness freeze --run '$(RUN)' --recording '$(RECORDING)' $(if $(MODEL),--model '$(MODEL)')

.PHONY: dataset-verify
dataset-verify: $(AIOPS_PY) ## Check labels, recording and split against the committed lock
	@test -n "$(RUN)" && test -n "$(RECORDING)" || { echo "usage: make dataset-verify RUN=<run id> RECORDING=<name> [MODEL=<name>]"; exit 1; }
	$(AIOPS) harness verify --run '$(RUN)' --recording '$(RECORDING)' $(if $(MODEL),--model '$(MODEL)')

# --- week 9: remediation, the live loop and its console -------------------------
# The loop reads as `aiops` and acts as `aiops-remediator`, by impersonation from
# your kubeconfig. VARIANT=B2 is free; A1 calls Claude. DRY_RUN=1 sends every
# change with --dry-run=server. State and audit log: ~/.incidentlens/live/.
CONSOLE_DIR := aiops/console

.PHONY: console-build
console-build: ## Build the operator console (aiops/console -> dist/)
	cd $(CONSOLE_DIR) && npm ci --no-fund --no-audit && npm run build

.PHONY: console
console: tools $(AIOPS_PY) ## Live loop + console on http://127.0.0.1:8090: make console [VARIANT=B2|A1] [DRY_RUN=1]
	@test -f $(CONSOLE_DIR)/dist/index.html || $(MAKE) --no-print-directory console-build
	$(AIOPS) loop serve --context kind-$(CLUSTER) --kubectl $(KUBECTL) --variant $(or $(VARIANT),B2) \
	  --grafana http://localhost:$(GRAFANA_PORT) $(if $(DRY_RUN),--dry-run) $(if $(MODEL),--model '$(MODEL)') \
	  $(if $(RECORD_DIR),--record-dir '$(RECORD_DIR)')

.PHONY: console-demo
console-demo: $(AIOPS_PY) ## The console over a made-up incident, no cluster: http://127.0.0.1:8090
	@test -f $(CONSOLE_DIR)/dist/index.html || $(MAKE) --no-print-directory console-build
	@mkdir -p .bin
	$(AIOPS) loop demo --db .bin/console-demo.db
	$(AIOPS) loop serve --no-loop --db .bin/console-demo.db

.PHONY: remediate-test
remediate-test: tools $(AIOPS_PY) ## Remediator on a running cluster: its RBAC, and its patches dry-run against the API server
	CONTEXT="kind-$(CLUSTER)" KUBECTL=$(KUBECTL) AIOPS_PY="$(AIOPS_PY)" PATHSEP="$(PATHSEP)" scripts/remediate-test.sh

# The live test (week 9): a short chaos run with the recorder alongside. Run it
# once with `make console` going in another terminal (approve what it proposes),
# once without, then compare with `make mttr`.
LIVE_FAULTS ?= F01,F02,F03,F11,F12
.PHONY: live-test
live-test: tools $(AIOPS_PY) $(CHAOS_PY) ## Chaos run of LIVE_FAULTS x REPEATS (default 2) with the recorder alongside
	@name=live-$$(date -u +%Y%m%dT%H%M%SZ); mkdir -p .bin; \
	$(AIOPS) recorder record $(AIOPS_KUBE) --name "$$name" --from now-65m > ".bin/$$name.log" 2>&1 & rec=$$!; \
	trap 'kill $$rec 2>/dev/null || true' EXIT; \
	echo "recording $$name alongside the run (log: .bin/$$name.log)"; \
	$(CHAOS) run --faults $(LIVE_FAULTS) --repeats $(or $(REPEATS),2) --fault-free 0 --benign 0 --seal-out ".bin/$$name.seal.json"; \
	echo "run over; recording 25 more minutes of recovery"; sleep 1500; \
	echo "done. Next: make detect RECORDING=$$name, then make mttr RUN=<run id above> RECORDING=$$name [BASELINE_RUN=... BASELINE_RECORDING=...]"

.PHONY: mttr
mttr: $(AIOPS_PY) ## Time to recover of a live-test run against one without the loop: make mttr RUN=... RECORDING=... [BASELINE_RUN=...]
	@test -n "$(RUN)" && test -n "$(RECORDING)" || { echo "usage: make mttr RUN=<run id> RECORDING=<name> [BASELINE_RUN=... BASELINE_RECORDING=...]"; exit 1; }
	$(AIOPS) harness mttr --run '$(RUN)' --recording '$(RECORDING)' \
	  $(if $(BASELINE_RUN),--baseline-run '$(BASELINE_RUN)' --baseline-recording '$(BASELINE_RECORDING)')

# --- week 10: evaluation runs (gate G3) -----------------------------------------
# Once: every detector and every variant on the locked test set, 3 seeds, under
# one spending cap (BUDGET, USD, default 10). The detectors run again first, on
# the frozen config. A stopped run resumes only while nothing that decides a
# verdict has changed (eval/dataset/<run>.g3.json says what did).
.PHONY: eval-final
eval-final: $(AIOPS_PY) ## Gate G3, once: make eval-final RUN=... RECORDING=... MODEL=... [BUDGET=10] [VARIANTS="B2 A1 A4 A2 A3"]
	@test -n "$(RUN)" && test -n "$(RECORDING)" && test -n "$(MODEL)" || { echo "usage: make eval-final RUN=<run id> RECORDING=<name> MODEL=<baseline model> [BUDGET=10]"; exit 1; }
	$(MAKE) --no-print-directory detect RECORDING='$(RECORDING)' MODEL='$(MODEL)'
	$(AIOPS) harness final --run '$(RUN)' --recording '$(RECORDING)' --model '$(MODEL)' \
	  $(if $(BUDGET),--budget $(BUDGET)) $(if $(VARIANTS),--variant $(VARIANTS))

.PHONY: results
results: $(AIOPS_PY) ## eval/results.md, results.json, charts and the README table: make results RUN=... [MTTR_RUN=live-run] [SET=tuning]
	@test -n "$(RUN)" || { echo "usage: make results RUN=<run id> [MTTR_RUN=<live-test run>] [SET=tuning] [PARTIAL=1]"; exit 1; }
	$(AIOPS) harness results --run '$(RUN)' $(if $(SET),--set $(SET)) $(if $(MTTR_RUN),--mttr-run '$(MTTR_RUN)') $(if $(PARTIAL),--partial)

.PHONY: detect-test
detect-test: tools $(AIOPS_PY) ## Detection layer on a running cluster: rules, the aiops account's reach, a recorded chunk
	CONTEXT="kind-$(CLUSTER)" KUBECTL=$(KUBECTL) AIOPS_PY="$(AIOPS_PY)" PATHSEP="$(PATHSEP)" scripts/detect-test.sh

.PHONY: status
status: tools ## Show ArgoCD applications, rollouts and pods
	$(KUBECTL) --context kind-$(CLUSTER) -n argocd get applications
	$(K) get rollouts,pods -o wide

.PHONY: logs
logs: tools ## Tail logs of all ShopSearch services
	$(K) logs -f --max-log-requests 10 --prefix -l app.kubernetes.io/part-of=shopsearch

.PHONY: down
down: tools ## Delete the kind cluster
	$(KIND) delete cluster --name $(CLUSTER)

# --- GitOps checks, no cluster needed -------------------------------------------
CHARTS := gateway search-api catalog indexer redis postgres loadgen

.PHONY: gitops-lint
gitops-lint: tools ## Lint and schema-check the charts, and unit test the policies
	KUBECONFORM="$${KUBECONFORM:-kubeconform}" KYVERNO="$${KYVERNO:-kyverno}" PROMTOOL="$${PROMTOOL:-promtool}" \
	  HELM=$(HELM) CHARTS="$(CHARTS)" scripts/gitops-lint.sh

# --- Terraform checks, no cloud account needed ----------------------------------
TF_MODULES := $(wildcard infra/modules/aws/*) $(wildcard infra/modules/oci/*) infra/modules/kind

.PHONY: infra-test
infra-test: ## Validate every Terraform module and run its offline tests
	@for m in $(TF_MODULES); do \
	  echo "==> $$m"; \
	  terraform -chdir=$$m init -backend=false -input=false >/dev/null; \
	  terraform -chdir=$$m validate; \
	  if [ -d $$m/tests ]; then terraform -chdir=$$m test; fi; \
	done

.PHONY: infra-lint
infra-lint: ## Run terraform fmt, tflint and checkov
	terraform fmt -check -recursive infra/modules
	cd infra && tflint --init --config "$$PWD/.tflint.hcl" && tflint --recursive --config "$$PWD/.tflint.hcl"
	checkov -d infra/modules --framework terraform --compact --quiet

# --- AWS session: real money, always finish with `make destroy` -----------------
# ADMIN_CIDR (your IP as x.x.x.x/32) and BUDGET_ALERT_EMAIL have no defaults.
# `state` keeps a local backend and is only ever touched by cloud-bootstrap.
AWS_UNITS_EXCLUDED := --filter '!./state'

.PHONY: aws-account
aws-account:
	@aws sts get-caller-identity --query Account --output text >/dev/null

.PHONY: cloud-bootstrap
cloud-bootstrap: aws-account ## Once per account: create the state bucket, lock table, budget and CI role
	export AWS_ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text); \
	cd infra/live/aws/state && terragrunt run --non-interactive -- apply -auto-approve; \
	cd ../budget && terragrunt run --non-interactive -- apply -auto-approve; \
	cd ../ci && terragrunt run --non-interactive -- apply -auto-approve

.PHONY: cloud-plan
cloud-plan: aws-account ## Show what `make cloud` would change
	export AWS_ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text); \
	cd infra/live/aws && terragrunt run --all --non-interactive $(AWS_UNITS_EXCLUDED) -- plan

.PHONY: cloud
cloud: tools aws-account ## Build the EKS environment and hand it to ArgoCD
	@start=$$(date +%s); \
	export AWS_ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text); \
	(cd infra/live/aws && terragrunt run --all --non-interactive $(AWS_UNITS_EXCLUDED) -- apply); \
	aws eks update-kubeconfig --name $(CLUSTER) --alias eks-$(CLUSTER); \
	ENVIRONMENT=aws REVISION=main CONTEXT=eks-$(CLUSTER) KUBECTL=$(KUBECTL) HELM=$(HELM) scripts/bootstrap-gitops.sh; \
	echo "make cloud finished in $$(( $$(date +%s) - start ))s. Run make destroy when you are done."

.PHONY: destroy
destroy: tools aws-account ## Remove the EKS environment and prove nothing billable is left
	export AWS_ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text); \
	CLUSTER=$(CLUSTER) KUBECTL=$(KUBECTL) scripts/cloud-destroy.sh

.PHONY: verify-destroyed
verify-destroyed: aws-account ## Ask AWS whether anything billable is still running
	CLUSTER=$(CLUSTER) scripts/verify-destroyed.sh
