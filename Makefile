CLUSTER   ?= agent-relay
NS        ?= agent-relay
RELEASE   ?= relay
IMAGE     ?= agent-relay:dev
BASE_URL  ?= http://localhost:8000
CHART     := chart/agent-relay
# Extra helm flags, e.g. HELM_ARGS="--set workers.roles.reviewer.replicas=3"
HELM_ARGS ?=

KUBECTL   := kubectl --context kind-$(CLUSTER) -n $(NS)
HELM      := helm --kube-context kind-$(CLUSTER) -n $(NS)
HELM_UP   := $(HELM) upgrade --install $(RELEASE) $(CHART) --create-namespace --wait --timeout 3m $(HELM_ARGS)

# Optional filter for logs-workers, e.g. ROLE=reviewer
comma := ,
ROLE_SELECTOR = app=agent-relay-worker$(if $(ROLE),$(comma)role=$(ROLE))

# ARC (Actions Runner Controller)
ARC_VERSION     ?= 0.15.1
ARC_CHARTS      := oci://ghcr.io/actions/actions-runner-controller-charts
ARC_NS          ?= arc-systems
ARC_RUNNERS_NS  ?= arc-runners
# Name used as `runs-on:` in workflows
ARC_RUNNER_SET  ?= agent-relay-runner
GITHUB_CONFIG_URL ?= https://github.com/zssvaidar/agent-relay
ARC_HELM        := helm --kube-context kind-$(CLUSTER)

export NS BASE_URL

.DEFAULT_GOAL := help
.PHONY: help up cluster image deploy bootstrap template lint smoke status \
        logs logs-web logs-workers restart redeploy scale-workers uninstall \
        check-projects project-secret llm-secret smoke-project \
        test down clean arc arc-runners arc-status arc-uninstall

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

up: cluster image deploy ## Create cluster, build+load image, deploy everything

cluster: ## Create the kind cluster (1 control-plane + 2 workers)
	@kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config k8s/kind-config.yaml
	kubectl config use-context kind-$(CLUSTER)

image: ## Build the image and load it into kind
	docker build -t $(IMAGE) .
	kind load docker-image $(IMAGE) --name $(CLUSTER)

# Two helm passes: the first brings up web (+ workers whose Secret already
# exists), bootstrap registers any new roles, the second adds their workers.
deploy: ## helm install/upgrade, register new roles, deploy their workers
	$(HELM_UP)
	@$(MAKE) --no-print-directory bootstrap
	$(HELM_UP)

bootstrap: ## Register roles from values.yaml that have no secret/worker-<role> yet
	@roles="$$($(KUBECTL) get configmap relay-roles -o jsonpath='{.data.roles}')"; \
	for role in $$roles; do ./scripts/bootstrap-worker.sh $$role || exit 1; done

template: ## Render the chart to stdout (all workers shown) without installing
	helm template $(RELEASE) $(CHART) -n $(NS) --set workers.waitForSecret=false $(HELM_ARGS)

lint: ## Lint the chart
	helm lint $(CHART)

smoke: ## Send tasks to every role and check they complete (COUNT=3)
	./scripts/smoke-test.sh

smoke-project: ## Ask the orchestrator to deploy PROJECT and follow the chain (REF=, ACTION=, TASK=, PR=)
	@test -n "$(PROJECT)" || { echo "usage: make smoke-project PROJECT=<key> [REF=main] [ACTION=deploy]"; exit 1; }
	PROJECT=$(PROJECT) REF=$(REF) TASK="$(TASK)" PR=$(PR) ACTION=$(or $(ACTION),deploy) ./scripts/smoke-project.sh

# The registry is the `projects:` block of values.yaml; projects.py validates it.
check-projects: ## Validate the project registry and check each credentials Secret exists
	@uv run python projects.py $(CHART)/values.yaml
	@missing=; for s in $$(uv run python projects.py $(CHART)/values.yaml --secrets); do \
	  if $(KUBECTL) get secret $$s >/dev/null 2>&1; then echo "ok  secret/$$s"; \
	  else echo "MISSING secret/$$s -> make project-secret PROJECT=<key> FROM=<token file>"; missing=1; fi; \
	done; test -z "$$missing"

project-secret: ## Store a project's bot token: make project-secret PROJECT=<key> FROM=<token file>
	@test -n "$(PROJECT)" -a -f "$(FROM)" || { echo "usage: make project-secret PROJECT=<key> FROM=<file containing the token>"; exit 1; }
	@name=$$(uv run python projects.py $(CHART)/values.yaml $(PROJECT)) && \
	$(KUBECTL) create secret generic $$name --from-file=token=$(FROM) --dry-run=client -o yaml | $(KUBECTL) apply -f - && \
	$(KUBECTL) label secret $$name app=agent-relay-project project=$(PROJECT) --overwrite >/dev/null && \
	echo "stored secret/$$name (key: token)"

llm-secret: ## Store the Anthropic API key for agent roles: make llm-secret FROM=<key file>
	@test -f "$(FROM)" || { echo "usage: make llm-secret FROM=<file containing the API key>"; exit 1; }
	@$(KUBECTL) create secret generic relay-anthropic --from-file=ANTHROPIC_API_KEY=$(FROM) \
	  --dry-run=client -o yaml | $(KUBECTL) apply -f - && echo "stored secret/relay-anthropic"

status: ## Show helm release, pods (with nodes), services and PVC
	$(HELM) list
	$(KUBECTL) get pods,svc,pvc -o wide

logs: logs-workers ## Alias for logs-workers

logs-web: ## Follow web logs
	$(KUBECTL) logs -f deploy/agent-relay-web

logs-workers: ## Follow worker logs (all roles, or ROLE=reviewer)
	$(KUBECTL) logs -f -l $(ROLE_SELECTOR) --prefix --max-log-requests=20

restart: ## Restart web and all workers (data persists on the PVC)
	$(KUBECTL) rollout restart deploy -l app.kubernetes.io/instance=$(RELEASE)
	@for d in $$($(KUBECTL) get deploy -l app.kubernetes.io/instance=$(RELEASE) -o name); do \
	  $(KUBECTL) rollout status $$d --timeout=120s || exit 1; \
	done

redeploy: image restart ## Rebuild image after code changes and restart pods

scale-workers: ## Change one role's replicas, e.g. make scale-workers ROLE=reviewer N=3
	@test -n "$(ROLE)" -a -n "$(N)" || { echo "usage: make scale-workers ROLE=<role> N=<count>"; exit 1; }
	$(HELM) upgrade $(RELEASE) $(CHART) --reuse-values --wait --set workers.roles.$(ROLE).replicas=$(N)

uninstall: ## helm uninstall (keeps PVC, enrollment and worker secrets)
	$(HELM) uninstall $(RELEASE)

arc: ## Install ARC controller (single pod) into $(ARC_NS)
	$(ARC_HELM) upgrade --install arc $(ARC_CHARTS)/gha-runner-scale-set-controller \
	  --version $(ARC_VERSION) -n $(ARC_NS) --create-namespace --wait \
	  -f k8s/arc/controller-values.yaml

arc-runners: ## Register a runner scale set (max 1 runner); needs GITHUB_TOKEN
	@test -n "$(GITHUB_TOKEN)" || { echo "usage: GITHUB_TOKEN=<pat with repo scope> make arc-runners"; exit 1; }
	$(ARC_HELM) upgrade --install $(ARC_RUNNER_SET) $(ARC_CHARTS)/gha-runner-scale-set \
	  --version $(ARC_VERSION) -n $(ARC_RUNNERS_NS) --create-namespace --wait \
	  -f k8s/arc/runner-set-values.yaml \
	  --set githubConfigUrl=$(GITHUB_CONFIG_URL) \
	  --set githubConfigSecret.github_token=$(GITHUB_TOKEN)

arc-status: ## Show ARC controller, listener and runner pods
	kubectl --context kind-$(CLUSTER) get pods -n $(ARC_NS) -o wide
	kubectl --context kind-$(CLUSTER) get pods -n $(ARC_RUNNERS_NS) -o wide 2>/dev/null || true

arc-uninstall: ## Remove runner scale set and ARC controller
	-$(ARC_HELM) uninstall $(ARC_RUNNER_SET) -n $(ARC_RUNNERS_NS) --wait
	-$(ARC_HELM) uninstall arc -n $(ARC_NS) --wait

test: ## Run the local pytest suite
	uv run pytest -q

down: ## Delete the kind cluster (all data is lost)
	kind delete cluster --name $(CLUSTER)

clean: down ## Delete cluster and local image
	-docker rmi $(IMAGE)
