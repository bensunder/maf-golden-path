PY ?= python
PACKAGES := testing telemetry guardrails tools hosting channels knowledge
EXTRAS_hosting := [redis,cosmos]
EXTRAS_channels := [teams]
EXTRAS_knowledge := [blob]
PLAYWRIGHT_VERSION ?= 1.56.0
GEN_DIR ?= $(or $(TMPDIR),/tmp)/agentkit-template-check
TOOLS_DIR := $(CURDIR)/.tools/bin
BICEP_VERSION ?= v0.47.16
ACTIONLINT_VERSION ?= 1.7.7
export PATH := $(TOOLS_DIR):$(PATH)

# Pick tool builds for this machine: Linux or macOS, x86-64 or ARM (Apple Silicon).
UNAME_S ?= $(shell uname -s)
UNAME_M ?= $(shell uname -m)
BICEP_OS := $(if $(filter Darwin,$(UNAME_S)),osx,linux)
BICEP_ARCH := $(if $(filter arm64 aarch64,$(UNAME_M)),arm64,x64)
ACTIONLINT_OS := $(if $(filter Darwin,$(UNAME_S)),darwin,linux)
ACTIONLINT_ARCH := $(if $(filter arm64 aarch64,$(UNAME_M)),arm64,amd64)

.PHONY: install browser tools test test-packages test-example test-template test-infra smoke new-agent

install:            ## editable installs of all packages + the sample agent
	$(PY) -m pip install -q copier jsonschema
	$(foreach p,$(PACKAGES),$(PY) -m pip install -q -e "packages/agentkit-$(p)$(EXTRAS_$(p))";)
	$(PY) -m pip install -q -e "examples/order-status-agent[dev]"

browser:            ## optional: Playwright + Chromium for the web chat browser tests (skipped without it)
	$(PY) -m pip install -q "playwright==$(PLAYWRIGHT_VERSION)"
	$(PY) -m playwright install chromium

tools:              ## download bicep + actionlint for this OS/CPU into .tools/bin (offline infra validation)
	mkdir -p $(TOOLS_DIR)
	test -x $(TOOLS_DIR)/bicep || curl -fsSL -o $(TOOLS_DIR)/bicep https://github.com/Azure/bicep/releases/download/$(BICEP_VERSION)/bicep-$(BICEP_OS)-$(BICEP_ARCH)
	chmod +x $(TOOLS_DIR)/bicep
	test -x $(TOOLS_DIR)/actionlint || curl -fsSL https://github.com/rhysd/actionlint/releases/download/v$(ACTIONLINT_VERSION)/actionlint_$(ACTIONLINT_VERSION)_$(ACTIONLINT_OS)_$(ACTIONLINT_ARCH).tar.gz | tar xz -C $(TOOLS_DIR) actionlint

test: test-packages test-example test-template test-infra smoke

RESULTS_DIR ?= $(CURDIR)/.results

test-packages:
	mkdir -p $(RESULTS_DIR)
	$(PY) -m pytest packages -q --junitxml=$(RESULTS_DIR)/packages.xml

test-example:
	mkdir -p $(RESULTS_DIR)
	cd examples/order-status-agent && $(PY) -m pytest -q --junitxml=$(RESULTS_DIR)/example.xml

TEAMS := --data enable_teams=true --data enable_web_chat=false --data enable_knowledge=true

test-template:      ## render both install modes, with and without Teams; run the generated projects' tests
	rm -rf $(GEN_DIR)
	copier copy --defaults --vcs-ref HEAD -q --data project_name="Template Check" --data agentkit_source=feed . $(GEN_DIR)/feed
	copier copy --defaults --vcs-ref HEAD -q --data project_name="Template Check" --data agentkit_source=git . $(GEN_DIR)/git
	copier copy --defaults --vcs-ref HEAD -q --data project_name="Teams Check" --data agentkit_source=feed $(TEAMS) . $(GEN_DIR)/feed-teams
	copier copy --defaults --vcs-ref HEAD -q --data project_name="Teams Check" --data agentkit_source=git --data enable_teams=true --data enable_knowledge=true . $(GEN_DIR)/git-teams
	$(PY) scripts/check_rendered.py $(GEN_DIR)/git
	$(PY) scripts/check_rendered.py $(GEN_DIR)/git-teams
	$(PY) -m pip install -q --no-deps -e $(GEN_DIR)/feed
	cd $(GEN_DIR)/feed && $(PY) -m pytest -q -p no:cacheprovider
	$(PY) -m pip install -q --no-deps -e $(GEN_DIR)/feed-teams
	cd $(GEN_DIR)/feed-teams && $(PY) -m pytest -q -p no:cacheprovider

test-infra: tools   ## Bicep build+lint, azure.yaml schema, workflow lint, platform↔service contract
	test -d $(GEN_DIR)/git-teams || $(MAKE) test-template
	$(PY) scripts/check_infra.py --service $(GEN_DIR)/git
	$(PY) scripts/check_infra.py --service $(GEN_DIR)/git-teams
	$(PY) scripts/check_infra.py --service examples/order-status-agent

smoke:
	$(PY) scripts/e2e_smoke.py --app order_status_agent.app:app

new-agent:          ## make new-agent DEST=../my-agent
	@test -n "$(DEST)" || (echo "usage: make new-agent DEST=../my-agent" && exit 1)
	copier copy . $(DEST)
