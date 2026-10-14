# Long-term memory

`agentkit-memory` lets an agent remember each signed-in user's lasting preferences across conversations ("prefers replies in bullet points", "works EMEA hours"). It is governed like the rest of the kit: notes are screened before they are stored, bounded, retained for a set time, keyed so they can't be traced back to a person, and every user can review, correct, export or erase their own memory.

Most agents don't need it. Sessions already give every agent memory *within* a conversation. Turn long-term memory on only when remembering a person across conversations clearly helps them.

## Principles

1. **Memory is a per-agent choice, not a platform default.** The template asks once (`memory`), and the default is `session`: the conversation only. An agent that remembers people says so in its code, its console and its Security page.
2. **The user owns their memory.** They see exactly what is kept, can change or delete any note, export all of it, and erase everything. The agent forgets when asked. Nobody else, including other users of the same agent, can read it.
3. **Memory is data, never instructions.** Notes reach the model fenced and labelled as information about the user. Anything that reads like an instruction to the agent, and any personal data (contact details, card or ID numbers), is refused before it is stored.

## Profiles

| `memory` | What the agent keeps | Where |
|---|---|---|
| `session` (default) | The conversation, for `AGENTKIT_SESSION_TTL_SECONDS` | The session store |
| `user` | The conversation, plus up to 50 short notes per user for 180 days after each was last updated | The session store's backend (Redis or Cosmos DB), under a separate key |

LangGraph agents get `session` only for now.

## Turn it on

New service:

```bash
copier copy --trust --vcs-ref v0.10.2 --data project_name="Payments" --data memory=user gh:bensunder/maf-golden-path payments
```

Or tick **Long-term memory** on the console's **Create agent** page.

Existing service: add the dependency `agentkit-memory[redis,cosmos]` and two lines:

```python
# agent.py
from agentkit.memory import UserMemoryProvider, memory_service

build_agent(..., context_providers=[UserMemoryProvider(memory_service(settings))])

# app.py
from agentkit.memory import MemoryChannel

create_app(create_agent, channels=[..., MemoryChannel()])
```

`memory_service(settings)` returns one shared instance per configuration, so the agent and the API see each other's changes at once. Then set a salt (below) before deploying to prod.

## What the agent sees and does

Before each run, `UserMemoryProvider` (a MAF `ContextProvider`) looks up the caller's notes and adds to the instructions:

- short guidance on when to remember: only what the user states as a lasting preference or asks to be remembered, never contact details, other people, or instructions about the agent's behaviour;
- the notes, newest first, inside `<remembered_about_this_user>`, prefixed by a line saying they are information, not instructions.

It also gives the agent two tools for this run, bound to the caller: `remember_user_preference(text)` and `forget_user_preference(memory_id)`. The agent can only ever write to the memory of the person it is talking to.

A run without a signed-in user gets no memory and no tools: there is no shared "anonymous" memory. If the store is unavailable, the agent is told memory is unavailable and carries on; a memory outage never fails a conversation.

## Controls

| Control | Default | What happens |
|---|---|---|
| Screening | on | Each note is normalised (whitespace, control characters), then refused if empty, longer than `max_chars`, contains personal data (the guardrails' PII detector), or reads as an instruction (the injection detector). Refused notes are never stored, even briefly |
| Isolation | on | The storage key is `HMAC-SHA256(salt, tenant ∥ user)`. Users, and the same user in two tenants, never share memory. The stored key can't be reversed to an identity without the salt |
| Bounds | 50 notes × 280 characters | At the limit, the least recently updated note is replaced. Saying the same thing again refreshes the existing note instead of adding a duplicate |
| Retention | 180 days | Each note expires `retention_days` after it was last updated; the stored record has a TTL, so Redis and Cosmos delete it even if the service never runs again |
| Rate limit | 30 writes per minute per user | More returns `429` with `Retry-After` |
| Resilience | 2 s timeout, circuit breaker | After 5 consecutive store failures memory pauses for 30 s (`503` with `Retry-After`; the agent is told it's unavailable), then one trial call decides whether to resume |
| Concurrency | optimistic | Each write is a compare-and-set on a version (Redis Lua script; Cosmos ETag). Concurrent writes retry with jitter, so none is lost |
| Telemetry | on | Spans `memory.recall`, `memory.remember`, `memory.edit`, `memory.delete`, `memory.forget_all`, `memory.inject` and the counter `agentkit.memory.operations{memory.op, memory.outcome}`. Note text is never recorded |

## The memory API

`MemoryChannel` serves `/v1/memory`. Every call acts on the caller's own memory (the same identity headers as the rest of the API); without a signed-in user it returns `401`.

| Method and path | Result |
|---|---|
| `GET /v1/memory` | `200` `{items, count, max_items, retention_days}`, newest first |
| `POST /v1/memory` `{text, kind?}` with an `Idempotency-Key` header (8–128 of `A-Z a-z 0-9 _ -`) | `201` with the new note; `200` with the original for a retried key or a repeated note |
| `PATCH /v1/memory/{id}` `{text}` | `200` with the updated note (it is now marked as added by the user) |
| `DELETE /v1/memory/{id}` | `204`, also if it was already gone |
| `DELETE /v1/memory` | `204`: everything forgotten |
| `GET /v1/memory/export` | `200` Markdown attachment of every note, `Cache-Control: no-store` |

Errors have one shape, `{"detail": {"code": "...", "message": "..."}}`, with a human-readable message:

| Status | `code` |
|---|---|
| `401` | `unauthenticated` |
| `404` | `memory_not_found` |
| `409` | `memory_conflict` (too many concurrent writes; retry) |
| `415` | non-JSON body (the kit's CSRF defence) |
| `422` | `empty`, `too_long`, `personal_data`, `instructions`, `duplicate`, or FastAPI's validation error list (e.g. a missing `Idempotency-Key`) |
| `429` | `memory_rate_limited`, with `Retry-After` |
| `503` | `memory_unavailable`, with `Retry-After` |

The full schema is in the service's OpenAPI document at `/docs`.

## The console

The **Memory** page shows the signed-in person what this agent remembers about them, and lets them add, edit, delete, export or erase everything. Changes appear at once and roll back if the service refuses them; every outcome is announced to screen readers. The page is loaded only when opened, so agents without memory don't ship it to browsers.

The **Security** page has a **Long-term memory** control on agents that use it. It shows *partial* when the salt isn't set or the store is in-process, with what to fix.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `AGENTKIT_MEMORY_SALT` | none | Secret mixed into every memory key; 16+ characters. **Required in prod.** Never change it on a live service: existing memory becomes unreachable (it then expires on its own) |
| `AGENTKIT_MEMORY_STORE` | `inherit` | `inherit` uses `AGENTKIT_SESSION_STORE` and its connection; or `memory`, `redis`, `cosmos` |
| `AGENTKIT_MEMORY_RETENTION_DAYS` | `180` | Days a note is kept after its last update (1–3650) |
| `AGENTKIT_MEMORY_MAX_ITEMS` | `50` | Notes per user (1–500) |
| `AGENTKIT_MEMORY_MAX_CHARS` | `280` | Characters per note (20–1000) |
| `AGENTKIT_MEMORY_TIMEOUT_SECONDS` | `2` | Per store call |
| `AGENTKIT_MEMORY_CACHE_SECONDS` | `15` | How long a user's notes are cached in the replica (`0` = off). Writes through this replica refresh it at once |
| `AGENTKIT_MEMORY_WRITES_PER_MINUTE` | `30` | Per user (`0` = no limit) |
| `AGENTKIT_MEMORY_COSMOS_CONTAINER` | session container | Cosmos container for memory documents (ids are `memory:<key>`, so they never collide with sessions) |
| `AGENTKIT_MEMORY_DEV_USER` | none | Local and test only: who to remember for when a request has no sign-in (the playground on a laptop). Refused in prod |

With `AGENTKIT_ENVIRONMENT=prod` the service refuses to start without a salt, with the in-process store, or with a dev user.

## Operations

- **Azure.** `azd env set AGENTKIT_MEMORY_SALT "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"` before the first `azd up`. The generated Bicep passes it to the Container App as a secret, and in prod the `azd` preprovision hook stops before deploying if it's missing. Memory documents live in the session container, which already has per-item TTL on.
- **VPS.** Memory uses the agent's own Redis user, which can only touch `agentkit:<name>:*` with `GET`, `SET`, `DEL`, `EVAL` and `PING`. That is all memory needs; the package's tests run the store as exactly that user. Add `AGENTKIT_MEMORY_SALT` to the agent's environment.
- **Erasure requests.** A user erases their own memory from the console or with `DELETE /v1/memory`. Everything also expires `retention_days` after the last update. There is no admin endpoint to read someone else's memory, by design.
- **Multiple replicas.** Each replica caches a user's notes for `cache_seconds`. A change made through another replica is seen within that time.

## Testing

Generated services with `memory=user` get `tests/test_memory.py`: over HTTP, with a scripted model, it checks that a note is recalled in a later conversation, is never shown to another user, can be reviewed, exported and erased, and that personal data is refused. `conftest.py` resets memory between tests.

In your own tests, `MemoryService(InMemoryMemoryStore(), salt="...")` gives a private instance, and `agentkit.telemetry.run_context(user_id=...)` sets the caller for direct `agent.run(...)` calls.

## Not included yet

- **No automatic extraction.** The agent remembers only through its tool, when the user states a preference or asks. Nothing is mined from transcripts in the background.
- **No search over memory.** All of a user's notes (50 short ones at most) go into each run; there are no embeddings.
- **Notes are screened with the heuristic injection detector, not Prompt Shields, even in prod.** Recalled notes are still fenced as data, and each chat turn still passes the agent's Prompt Shields input guard. See the package README for a workaround.
- **Cosmos DB hasn't run against a real account yet.** The store is tested against a fake container with ETags, 412s and TTL, and Redis against a real server.
- **Evals run one conversation per case**, so cross-conversation recall is covered by the generated pytest tests rather than `evals/cases.yaml`.
- **LangGraph agents** don't take context providers yet.
