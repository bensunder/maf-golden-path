# Working in Order Status Agent

This repo was generated from the agentkit golden path (see `.copier-answers.yml`). Coding assistants and humans follow the same rules:

- The agent is defined in `src/order_status_agent/agent.py` via `agentkit.hosting.build_agent`. Do not construct `agent_framework.Agent` or chat clients directly. `build_agent` adds the gateway client, Entra auth, guardrails, telemetry and run limits.
- Add capabilities as `@tool` functions in `src/order_status_agent/tools.py` and register them in `TOOLS`. State-changing tools need `approval_mode="always_require"` or a validator in `TOOL_POLICY`.
- Behaviour changes go in `src/order_status_agent/instructions/system.md`, plus a case in `evals/cases.yaml`.
- Tests never call a real model: use `agentkit.testing.ScriptedChatClient` with `reply(...)` / `tool_call(...)`.
- Configuration is `AGENTKIT_*` environment variables (`AgentKitSettings`). Never hard-code endpoints or keys.
- Run `pytest` before proposing changes. To pull template updates, run `copier update`.
