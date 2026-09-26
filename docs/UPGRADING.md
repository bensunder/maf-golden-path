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
   - `FunctionInvocationConfiguration` keys (run limits);
   - `tool(func, name=, description=, schema=, additional_properties=)` with a `**kwargs` function (OpenAPI tools);
   - `MCPStreamableHTTPTool(http_client=, allowed_tools=)` (gateway MCP helper);
   - `azure.identity.aio.OnBehalfOfCredential(client_assertion_func=, user_assertion=)` (on-behalf-of auth);
   - approvals: `AgentResponse.user_input_requests`, `Content.to_function_approval_response()`, `ToolApprovalMiddleware(auto_approval_rules=)` (rule receives the function call; requires a session; surfaces queued approvals one at a time), and the pending-approval state MAF keeps in `session.state["tool_approval"]`. Also check whether the spurious "did not match the active approval occurrence" warning (demoted by `agentkit.hosting.approvals`) is fixed upstream.
   - knowledge: `azure-search-documents` (`SearchClient.search(filter=, vector_queries=[VectorizedQuery], vector_filter_mode=, query_type=, semantic_configuration_name=)`, `SearchIndex` models); `packages/agentkit-knowledge/tests/test_search_wire.py` pins the request body on the wire.
   - channels: `microsoft-agents-hosting-*` (pinned `<2`): `AgentApplication(ApplicationOptions(storage=…))`, `adaptive_card.action_execute`, `CloudAdapter(connection_manager=, host_validator=)`, `continue_conversation_with_claims`, `Conversation.store_item_to_json/from_json_to_store_item`, `jwt_authorization_decorator`; `ag-ui-protocol` (pinned `<0.2`): `RunAgentInput.resume`, `RunFinishedEvent.outcome` interrupts. The Teams and AG-UI tests exercise all of these offline.
4. **Runner images.** CI and the reusable pipelines run on `ubuntu-24.04`, not `ubuntu-latest` (which moves to Ubuntu 26 on 2026-10-19). Move them deliberately: bump the label on a branch and let `make test` in CI prove Redis, Chromium and the tools still install.
5. **Infra drift.** Bump `BICEP_VERSION` in the Makefile deliberately. Azure API versions in the Bicep are pinned; `make test-infra` fails on any new linter warning, so review them on upgrade.
6. **Release.** Tag `vX.Y.Z`; generated services move by bumping the tag in `pyproject.toml` (git mode) or the version range (feed mode). Template changes reach existing services with `copier update`.
7. **Semver for teams.** Kit patch = no action. Kit minor = new defaults, may need `copier update`. Kit major = breaking API in `build_agent` / settings, with migration notes here.


## Kit release notes for services

### 0.7 → 0.8 (console)

- New: the console at `/console` ([console.md](console.md)). `copier update` asks `enable_console` (default: on when web chat is on). It adds `Console(...)` to `app.py`, `COPY evals ./evals` to the Dockerfile, four optional Bicep parameters (build commit, run link, time, workbook id) and a test. `app.py` is generated, so accept the kit's change unless you edited it.
- `agent-deploy` runs the offline tests once more before building the image, with `--agentkit-eval-report evals/gate-report.json`, so the console shows the gate result for the exact commit deployed. It also passes the commit, run link and time.
- `scripts/platform_env.py` also emits `AGENTKIT_OPS_WORKBOOK_ID` when the platform outputs it. Set it as a GitHub variable to link the console to the operations workbook.
- The pytest plugin gains `--agentkit-eval-report PATH` (a gate report from the offline eval run). `CaseStats` in gate reports gains `mean_duration_s`.
- `ConversationService.agent` (read-only property) and `agentkit.channels.security_posture()` are new. The knowledge tool now describes its configuration in `additional_properties["agentkit.knowledge"]` (no endpoints).
- The live check reads the console's posture and, in prod, fails unless sign-in, Prompt Shields, tool-output scanning, session isolation, audit and no-content-capture are all on.
- **Behaviour change:** a streamed approval decision (AG-UI, the web chat, the console) now runs to completion even if the client disconnects. Before, a disconnect could cancel the resumed run after the tool ran, leaving the approval pending (so approving again ran the tool twice).
- **Behaviour change:** the AG-UI interrupt's `metadata.awaiting` is `"requester"` when the caller may decide (including an approver in approver mode without separation), `"approver"` when someone else must. It used to say `"approver"` whenever an approver role was set.

### 0.6 → 0.7 (live validation, operations)

- The platform deploys an operations workbook and five alerts (`alertEmail` for email). Re-deploy the platform to get them.
- `agentkit.agent.runs` gains a `blocked_reason` dimension; `agentkit.knowledge.searches` is new.
- Callers without a principal name (app-only tokens through Easy Auth) are now identified by `x-ms-client-principal-id` instead of being refused. Set `AGENTKIT_USER_FALLBACK_HEADER=` (empty) to restore the old behaviour.
- `agentkit-gate` gains `--calibrate`, `--skip`, `--skip-user-cases`, and reads `AGENTKIT_EVAL_USERS`.
- CI and the reusable pipelines run on `ubuntu-24.04`.

### 0.5 → 0.6 (knowledge)

- `copier update` asks `enable_knowledge` (default no). With it: `knowledge/` + `acl.yaml`, the knowledge tool in `tools.py`, citation rules in `system.md`, a test fixture in `conftest.py`, two eval cases, and Search, Document Intelligence and Blob in Bicep. `tools.py`, `system.md` and `conftest.py` are yours, so copier may ask you to merge: keep your lines and add the kit's.
- The platform deploys an embedding model (`embeddingDeploymentName`, default `text-embedding-3-small`) next to the chat model. Re-run the platform deployment before enabling knowledge in a service.
- JSON responses gain `citations` (empty unless a knowledge tool was used); the stream's `done` event too.
- Eval cases accept `user:`, and `expect` accepts `cites:` and `must_not_retrieve:`.
- Graph permission `GroupMember.Read.All` for the service identity (and the pipeline identity, for live evals) when documents are trimmed by group.

### 0.4 → 0.5 (channels)

- `copier update` asks two new questions: `enable_web_chat` (default yes) and `enable_teams` (default no). See [channels.md](channels.md).
- `create_app(..., channels=[...])` is new; existing calls without it behave as before.
- The run and approval rules moved from the HTTP routes into `agentkit.hosting.ConversationService`. The HTTP API is unchanged (same paths, bodies, status codes).
- **Behaviour change:** every `POST` must send `Content-Type: application/json`; anything else gets `415`. Clients sending JSON without that header need a one-line fix.
- The audit log entries gain `decided_by_name` and `channel`.
- With web chat on, the Bicep switches Easy Auth to redirect browsers to sign-in. Add the redirect URI and enable ID tokens on the app registration ([channels.md](channels.md#easy-auth-for-browsers)).
