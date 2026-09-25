# FAQ and escape hatches

The paved road is the default, not a cage. Here's how to step off it when you need to, and what you give up.

### Can I use MAF features agentkit doesn't wrap: workflows, context providers, MCP tools?
Yes. `build_agent` returns a plain `agent_framework.Agent`, and extra keyword arguments are passed through:

```python
build_agent(..., context_providers=[my_provider], compaction_strategy=my_strategy)
```

MCP tools (`MCPStreamableHTTPTool`, …) go in `tools` like any other tool. An agent built with `build_agent` can be a node in a MAF workflow. Put MCP servers behind the gateway where possible, so they share auth and logging.

### Can I use a different model provider?
Pass any MAF chat client as `client=`. You keep the guardrails, telemetry and hosting, but lose the gateway headers, Entra wiring and run limits from `create_chat_client`. If more than one team needs the provider, add it to `agentkit.hosting.clients` so everyone gets it.

### Can I add or remove a guardrail?
- **Add:** `extra_middleware=[...]` (see [guardrails.md](guardrails.md#adding-your-own-guardrail)).
- **Tune:** use settings (`AGENTKIT_REDACT_PII`, `AGENTKIT_SCAN_TOOL_OUTPUT`, `AGENTKIT_MAX_INPUT_CHARS`, …).
- **Replace the detector:** `detector=MyDetector()`.
- **Remove:** `AGENTKIT_GUARDRAIL_MODE=off` works outside prod. Prod refuses it on purpose.

### I don't want the FastAPI host (queue worker, Teams bot, function app).
Use `create_agent()` directly and wrap the call in `run_context(...)` so traces carry the user and session. Call `agentkit.telemetry.setup_telemetry(...)` once at startup. Sessions are yours to persist: `session.to_dict()` / `AgentSession.from_dict()`.

### Why Chat Completions and not the Responses API?
Every APIM GenAI policy (token limits, semantic cache, content safety) supports Chat Completions. If your gateway supports Responses and you need its features, build an `OpenAIChatClient` and pass it as `client=`.

### How do I get template improvements into an existing service?
Run `copier update` in the service repo. Copier replays your saved answers against the new template version and shows a diff, with conflicts marked where you changed generated files. Package fixes arrive by bumping the agentkit version in `pyproject.toml`.

### Which files should I avoid editing?
`.copier-answers.yml` (copier manages it), `app.py` and the `create_agent` wiring in `agent.py`. Editing them makes `copier update` noisy. Your code goes in `tools.py`, `instructions/`, `evals/`, `tests/` and new modules you add.

### How do I test against a real model locally?
`az login`, set `AGENTKIT_GATEWAY_ENDPOINT` and `AGENTKIT_AUTH_MODE=azure_cli`, then run `AGENTKIT_LIVE_EVALS=1 pytest tests/test_evals.py`.

### What does a MAF upgrade mean for my service?
Usually nothing. The platform team pins MAF, tests the upgrade against the kit and the sample, and releases a new kit version. You bump one version. See [UPGRADING.md](UPGRADING.md).

### Where do I ask for something the kit should do?
Open an issue or PR on `maf-golden-path`. If two teams need it, it belongs in the kit.
