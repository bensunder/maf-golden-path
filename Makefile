PY ?= python
PACKAGES := testing telemetry guardrails tools hosting
GEN_DIR ?= $(or $(TMPDIR),/tmp)/agentkit-template-check
TOOLS_DIR := $(CURDIR)/.tools/bin
BICEP_VERSION ?= v0.47.16
ACTIONLINT_VERSION ?= 1.7.7
export PATH := $(TOOLS_DIR):$(PATH)

.PHONY: install tools test test-packages test-example test-template test-infra smoke new-agent

install:            ## editable installs of all packages + the sample agent
	$(PY) -m pip install -q copier jsonschema
	$(foreach p,$(PACKAGES),$(PY) -m pip install -q -e packages/agentkit-$(p);)
	$(PY) -m pip install -q -e "examples/order-status-agent[dev]"

tools:              ## download bicep + actionlint into .tools/bin (offline infra validation)
	mkdir -p $(TOOLS_DIR)
	test -x $(TOOLS_DIR)/bicep || curl -sSL -o $(TOOLS_DIR)/bicep https://github.com/Azure/bicep/releases/download/$(BICEP_VERSION)/bicep-linux-x64
	chmod +x $(TOOLS_DIR)/bicep
	test -x $(TOOLS_DIR)/actionlint || curl -sSL https://github.com/rhysd/actionlint/releases/download/v$(ACTIONLINT_VERSION)/actionlint_$(ACTIONLINT_VERSION)_linux_amd64.tar.gz | tar xz -C $(TOOLS_DIR) actionlint

test: test-packages test-example test-template test-infra smoke

test-packages:
	$(PY) -m pytest packages -q

test-example:
	cd examples/order-status-agent && $(PY) -m pytest -q

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
