# MAF version policy

MAF ships roughly weekly, and even minor releases break integration surfaces. The kit absorbs that churn so product teams don't read the upstream changelog.

1. **Pin.** Every package declares `agent-framework-core>=1.19,<1.20` (hosting also pins `agent-framework-openai` to a minor). One blessed MAF minor per kit release.
2. **Canary.** To move to a new MAF minor: bump the pins on a branch, run `make test` (packages, sample, template, e2e), then run the sample's live evals (`AGENTKIT_LIVE_EVALS=1`) against the dev gateway.
3. **Watch the seams.** These are the MAF internals the kit touches. Check them first when upgrading:
   - `BaseChatClient._inner_get_response` / `_build_response_stream` and the layer order `FunctionInvocationLayer, ChatMiddlewareLayer, ChatTelemetryLayer` (`agentkit.testing.ScriptedChatClient`);
   - `AgentContext.stream_result_hooks`, `context.result` short-circuit semantics (guardrails, metrics);
   - `ChatContext.messages` mutability (PII redaction);
   - `FunctionInvocationContext.arguments/result` (tool policy, tool-output shield);
   - `options["instructions"]` (testing `RecordedCall.instructions`);
   - `AgentSession.to_dict/from_dict` (session store);
   - `FunctionInvocationConfiguration` keys (run limits).
4. **Release.** Tag `vX.Y.Z`; generated services move by bumping the tag in `pyproject.toml` (git mode) or the version range (feed mode). Template changes reach existing services with `copier update`.
5. **Semver for teams.** Kit patch = no action. Kit minor = new defaults, may need `copier update`. Kit major = breaking API in `build_agent` / settings, with migration notes here.
