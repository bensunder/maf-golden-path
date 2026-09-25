PY ?= python
PACKAGES := testing telemetry guardrails tools hosting
EXTRAS_hosting := [redis,cosmos]
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

.PHONY: install tools test test-packages test-example test-template test-infra smoke new-agent

install:            ## editable installs of all packages + the sample agent
	$(PY) -m pip install -q copier jsonschema
	$(foreach p,$(PACKAGES),$(PY) -m pip install -q -e "packages/agentkit-$(p)$(EXTRAS_$(p))";)
	$(PY) -m pip install -q -e "examples/order-status-agent[dev]"

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

test-template:      ## render in both modes; run the generated project's tests (feed mode uses local packages)
	rm -rf $(GEN_DIR)
	copier copy --defaults --vcs-ref HEAD -q --data project_name="Template Check" --data agentkit_source=feed . $(GEN_DIR)/feed
	copier copy --defaults --vcs-ref HEAD -q --data project_name="Template Check" --data agentkit_source=git . $(GEN_DIR)/git
	$(PY) scripts/check_rendered.py $(GEN_DIR)/git
	$(PY) -m pip install -q --no-deps -e $(GEN_DIR)/feed
	cd $(GEN_DIR)/feed && $(PY) -m pytest -q -p no:cacheprovider

test-infra: tools   ## Bicep build+lint, azure.yaml schema, workflow lint, platform↔service contract
	test -d $(GEN_DIR)/git || $(MAKE) test-template
	$(PY) scripts/check_infra.py --service $(GEN_DIR)/git
	$(PY) scripts/check_infra.py --service examples/order-status-agent

smoke:
	$(PY) scripts/e2e_smoke.py --app order_status_agent.app:app

new-agent:          ## make new-agent DEST=../my-agent
	@test -n "$(DEST)" || (echo "usage: make new-agent DEST=../my-agent" && exit 1)
	copier copy . $(DEST)
