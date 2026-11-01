# IncidentLens: local workflow on kind.
# Needs: docker, make, bash, curl. kind and kubectl are downloaded into .bin/
# if they are not already on PATH.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

CLUSTER   ?= incidentlens
NAMESPACE ?= shopsearch
TAG       ?= dev
SERVICES  := gateway search-api catalog indexer

KIND_VERSION    ?= v0.33.0
KUBECTL_VERSION ?= v1.37.2

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

# Use kind/kubectl from PATH when present, else download them into .bin/.
# PATH hits are quoted because on Windows they often live under "Program Files".
KIND_PATH    := $(shell command -v kind 2>/dev/null)
KUBECTL_PATH := $(shell command -v kubectl 2>/dev/null)
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
K = $(KUBECTL) --context kind-$(CLUSTER) -n $(NAMESPACE)

.bin/kind$(EXE):
	@mkdir -p .bin
	curl -fsSLo $@ https://kind.sigs.k8s.io/dl/$(KIND_VERSION)/kind-$(OS)-$(ARCH)
	chmod +x $@

.bin/kubectl$(EXE):
	@mkdir -p .bin
	curl -fsSLo $@ https://dl.k8s.io/release/$(KUBECTL_VERSION)/bin/$(OS)/$(ARCH)/kubectl$(EXE)
	chmod +x $@

# --- targets ------------------------------------------------------------------
.PHONY: help
help: ## Show this help
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

.PHONY: tools
tools: $(TOOL_DEPS) ## Download kind and kubectl if missing

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
	  $(KIND) create cluster --name $(CLUSTER) --config infra/kind/cluster.yaml --wait 120s; \
	fi

.PHONY: load
load: tools ## Load the built images into the kind node
	$(KIND) load docker-image --name $(CLUSTER) $(foreach s,$(SERVICES),incidentlens/$(s):$(TAG))

.PHONY: secrets
secrets: tools ## Create the Postgres secret with a random password (once)
	$(KUBECTL) --context kind-$(CLUSTER) apply -f gitops/local/namespace.yaml
	@if ! $(K) get secret postgres-credentials >/dev/null 2>&1; then \
	  $(K) create secret generic postgres-credentials \
	    --from-literal=password="$$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"; \
	fi

.PHONY: deploy
deploy: secrets ## Apply the plain manifests and wait for every rollout
	$(KUBECTL) --context kind-$(CLUSTER) apply -k gitops/local
	$(K) rollout status statefulset/postgres --timeout=180s
	@for d in redis catalog search-api indexer gateway; do \
	  $(K) rollout status deployment/$$d --timeout=240s; \
	done

.PHONY: up
up: ## Create cluster, build, load, deploy and smoke test everything
	@start=$$(date +%s); \
	$(MAKE) --no-print-directory cluster build load; \
	existed=$$($(K) get deployment gateway -o name 2>/dev/null || true); \
	$(MAKE) --no-print-directory deploy; \
	if [ -n "$$existed" ]; then \
	  echo "==> restarting workloads to pick up the new :$(TAG) images"; \
	  $(K) rollout restart deployment $(SERVICES); \
	  $(MAKE) --no-print-directory wait; \
	fi; \
	$(MAKE) --no-print-directory smoke; \
	echo "make up finished in $$(( $$(date +%s) - start ))s. Gateway: http://localhost:8080"

.PHONY: wait
wait: tools
	@for d in catalog search-api indexer gateway; do \
	  $(K) rollout status deployment/$$d --timeout=240s; \
	done

.PHONY: redeploy
redeploy: build load ## Rebuild images and restart the workloads
	$(K) rollout restart deployment $(SERVICES)
	@$(MAKE) --no-print-directory wait

.PHONY: smoke
smoke: tools ## End-to-end check through the gateway, including the indexer queue
	KUBECTL=$(KUBECTL) CONTEXT="kind-$(CLUSTER)" NAMESPACE="$(NAMESPACE)" scripts/smoke.sh

.PHONY: status
status: tools ## Show pods
	$(K) get pods -o wide

.PHONY: logs
logs: tools ## Tail logs of all ShopSearch services
	$(K) logs -f --max-log-requests 10 --prefix -l app.kubernetes.io/part-of=shopsearch

.PHONY: down
down: tools ## Delete the kind cluster
	$(KIND) delete cluster --name $(CLUSTER)
