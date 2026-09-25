PY ?= python
PACKAGES := testing telemetry guardrails hosting
GEN_DIR ?= $(or $(TMPDIR),/tmp)/agentkit-template-check

.PHONY: install test test-packages test-example test-template smoke new-agent

install:            ## editable installs of all packages + the sample agent
	$(PY) -m pip install -q copier
	$(foreach p,$(PACKAGES),$(PY) -m pip install -q -e packages/agentkit-$(p);)
	$(PY) -m pip install -q -e "examples/order-status-agent[dev]"

test: test-packages test-example test-template smoke

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

smoke:
	$(PY) scripts/e2e_smoke.py --app order_status_agent.app:app

new-agent:          ## make new-agent DEST=../my-agent
	@test -n "$(DEST)" || (echo "usage: make new-agent DEST=../my-agent" && exit 1)
	copier copy . $(DEST)
