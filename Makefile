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
# Host port for the gateway. Fixed when the cluster is created: change it
# with `make down` and then `make up HOST_PORT=...`.
HOST_PORT ?= 8080

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
	  sed 's/hostPort: 8080/hostPort: $(HOST_PORT)/' infra/kind/cluster.yaml > .bin/kind-cluster.yaml; \
	  $(KIND) create cluster --name $(CLUSTER) --config .bin/kind-cluster.yaml --wait 120s; \
	fi

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
	echo "make up finished in $$(( $$(date +%s) - start ))s. Gateway: http://localhost:$(HOST_PORT)"

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

.PHONY: canary-test
canary-test: tools ## Prove canary promotion, automatic abort and admission policies
	GATEWAY="http://localhost:$(HOST_PORT)" KUBECTL=$(KUBECTL) CONTEXT="kind-$(CLUSTER)" scripts/canary-test.sh

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
	KUBECONFORM="$${KUBECONFORM:-kubeconform}" KYVERNO="$${KYVERNO:-kyverno}" HELM=$(HELM) CHARTS="$(CHARTS)" scripts/gitops-lint.sh

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
