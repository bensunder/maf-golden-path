# Long-term memory

Every MAF agent built with `build_agent` remembers, with no memory code in the agent:

| Kind | What it holds | Keyed by | Who sees it |
|---|---|---|---|
| **Sessions** (short-term) | The conversation | The session | Its owner |
| **User memory** | Each signed-in person's stated preferences ("prefers bullet points") | The person | That person only |
| **Lessons** | What the agent learned about doing its job ("Stripe decline code 51 means insufficient funds") | The agent | The model, once a person approved it; memory admins |
| **Subject memory** | What happened to one customer, invoice or case (`customer:1042  recovery_draft_created  draft_4da32bba`) | The subject | The agent's runs; memory admins |
| **Run journal** | What each run did: when, what started it, outcome, tools, subjects. Never what was said | The agent and the hour | The agent's runs; memory admins |

It's governed like the rest of the kit: everything is screened before it's stored, bounded, retained for a set time and keyed so it can't be traced back to a person. Each person can review, correct, export or erase their own memory, and a person reviews every lesson before the agent may use it.

## Principles

1. **Memory follows the work, not the chat.** Preferences belong to a person; lessons belong to the agent; what happened to customer 1042 belongs to customer 1042. Each is keyed accordingly, so a scheduled run (nobody signed in) still knows what an earlier run did for that customer, and nobody's preferences leak to anyone else.
2. **People own their memory.** They see exactly what is kept about them, can change or delete any note, export all of it and erase everything. There is no route to read someone else's.
3. **Memory is data, never instructions.** Everything reaches the model fenced and labelled as information. Anything that reads like an instruction to the agent, and any personal data (contact details, card or ID numbers), is refused before it is stored.
4. **What every run reads, a person approves.** Lessons reach every later run, so a planted lesson would spread. A lesson goes live at once only when a memory admin states it in a run that hadn't read outside data; everything else waits for an admin's review. Lessons are attributed, versioned and restorable.
5. **Memory never takes an agent down.** A store outage turns into "memory is unavailable" for that run; the run carries on.

## Levels

`AGENTKIT_MEMORY` (default `full`) sets what an agent gets. An agent's code can ask for less (`build_agent(..., memory="user")`); the setting can only lower it, so operators always have an off switch.

| Level | Gets |
|---|---|
| `full` (default) | Sessions, user memory, lessons, subject memory, run journal |
| `user` | Sessions and user memory |
| `session` | Sessions only |

It applies when `agentkit-memory` is installed: in generated projects (unless generated with `--data memory=session`) and in every agent on a VPS stack. LangGraph agents get sessions only for now.

## What the agent sees and does

Before each run the kit's two context providers (MAF `ContextProvider`s) add to the instructions:

- **User memory** (`UserMemoryProvider`, signed-in callers only): when to remember, and the caller's notes inside `<remembered_about_this_user>`, with the tools `remember_user_preference(text)` and `forget_user_preference(memory_id)`, bound to the caller.
- **Agent memory** (`AgentMemoryProvider`, every run): guidance on lessons, subjects and the journal, the active lessons inside `<agent_lessons>`, and five tools:

| Tool | Does |
|---|---|
| `remember_lesson(text)` | Save a reusable lesson. Live at once only from a memory admin in an untainted run; otherwise it waits for review |
| `forget_lesson(lesson_id)` | Archive a lesson (memory admins only; restorable) |
| `get_subject_history(subject)` | What already happened to `customer:1042`, oldest first, fenced as data |
| `log_subject_event(subject, event, note="")` | Record what happened: a snake_case event and a short note with ids or dates |
| `recent_activity(subject=None, hours=24)` | What recent runs did, optionally for one subject |

`JournalMiddleware` records every run when it finishes: interactive, agent-to-agent, scheduled, refused by a guardrail, failed, or streamed.

### When a lesson may go live

| The run | The lesson |
|---|---|
| A memory admin states it, and the run hasn't called any tool except memory's own writes | **Active** at once (`origin: admin`) |
| A signed-in person who isn't an admin states it | **Pending** (`origin: user`) |
| The run had read outside data (any other tool result: an invoice, an e-mail, a document), or nobody is signed in (a schedule), or another agent asked | **Pending** (`origin: untrusted`) |

So a prompt injection in a tool result can't plant a live lesson, even in an admin's own run. Pending lessons never reach the model. They wait `AGENTKIT_MEMORY_LESSON_REVIEW_DAYS` (30) for an admin to approve or reject them. Rejected and archived lessons can be restored for the same time.

### Scheduled and event-driven runs

A scheduler or event handler marks its runs so the journal and the lesson rules know nobody is at the keyboard:

```python
from agentkit.telemetry import run_context

with run_context(trigger="schedule", request_id=f"stripe-{event_id}"):
    await agent.run(f"Payment for customer:{customer_id} failed ({decline_code}). Recover it.")
```

Without a signed-in person there is no user memory (there's no shared "anonymous" memory), but lessons, subject memory and the journal all work.

## Tell the agent what to record

In `instructions/system.md`, name the subjects and events your agent works with, for example:

```markdown
Customers are subjects `customer:<Odoo partner id>`. Before drafting anything for a customer, check
get_subject_history. After you create a draft, log `recovery_draft_created` with the draft id. If the
customer promises a date, log `payment_promised` with the date.
```

The record of truth (names, amounts, e-mails) stays in the business system. Subject memory keeps ids and outcomes. Put long numeric ids in the subject (`order:1000234567`), not the note: notes go through the personal-data screen, which reads a long run of digits as a phone number.

## Controls

| Control | Default | What happens |
|---|---|---|
| Screening | on | Notes, lessons and event notes are normalised, then refused if empty, longer than `AGENTKIT_MEMORY_MAX_CHARS`, carrying personal data (the guardrails' PII detector) or reading as an instruction (the injection detector). Refused text is never stored. Subjects must be ids (`type:id`); names, e-mails and card numbers are refused |
| Isolation | on | Every key is an HMAC with the salt: users by tenant and user id, lessons by agent and tenant, subjects by agent, tenant and subject, journal pages by agent. Agents sharing a store never see each other's memory, and stored keys can't be reversed without the salt |
| Review | on | Lessons wait for a memory admin (above). Admins are `AGENTKIT_MEMORY_ADMINS`; holders of `AGENTKIT_APPROVER_ROLE` (where roles exist) may also review in the API and console, but only listed admins make a lesson live by stating it in chat. With neither, lessons wait unreviewed and the Security page says so |
| Attribution | on | Each lesson keeps its origin, the run that proposed it and up to 10 revisions (who, when, what text). People appear as pseudonyms (`u_` + an HMAC) |
| Bounds | 50 notes per user; 100 active, 50 pending and 50 archived lessons; 200 events per subject; 500 journal entries per hour shard | The least recently updated item makes room. A full journal page counts what it drops |
| Retention | user notes 180 days, lessons 365 (pending and archived 30), subject events 365, journal 90 | Each record's TTL is set in Redis and Cosmos, so it's deleted even if the service never runs again |
| Rate limit | 30 writes per minute per key | More returns `429` with `Retry-After` |
| Resilience | 2 s timeout, circuit breaker | After 5 consecutive store failures memory pauses for 30 s, then one trial call decides whether to resume |
| Concurrency | optimistic | Each write is a compare-and-set on a version (Redis Lua script; Cosmos ETag). Concurrent writes retry with jitter. The journal spreads each hour over 4 shards |
| Telemetry | on | Spans `memory.*` and the counter `agentkit.memory.operations{memory.op, memory.outcome}`. Memory text is never recorded |

## The memory API

`create_app` mounts it at `/v1/memory` whenever the agent has long-term memory. Every call uses the same identity headers as the rest of the API; without a signed-in user it returns `401`. A kind of memory the agent doesn't keep answers `404 memory_off`.

**Your own memory** (any signed-in person):

| Method and path | Result |
|---|---|
| `GET /v1/memory` | `200` `{items, count, max_items, retention_days}`, newest first |
| `POST /v1/memory` `{text, kind?}` with an `Idempotency-Key` header (8–128 of `A-Z a-z 0-9 _ -`) | `201` with the new note; `200` with the original for a retried key or a repeated note |
| `PATCH /v1/memory/{id}` `{text}` | `200` with the updated note |
| `DELETE /v1/memory/{id}` | `204`, also if it was already gone |
| `DELETE /v1/memory` | `204`: everything forgotten |
| `GET /v1/memory/export` | `200` Markdown attachment, `Cache-Control: no-store` |

**The agent's own memory** (memory admins; others get `403 not_memory_admin`):

| Method and path | Result |
|---|---|
| `GET /v1/memory/agent` | `200` `{is_admin, admins_configured, lessons (counts, admins only), config}`, for anyone signed in |
| `GET /v1/memory/agent/lessons[?status=pending\|active\|archived]` | `200` `{items, counts}`, newest first, with history |
| `POST /v1/memory/agent/lessons` `{text}` | `201` an admin's lesson, active at once; `200` if already known |
| `PATCH /v1/memory/agent/lessons/{id}` `{text}` | `200` edited |
| `POST /v1/memory/agent/lessons/{id}/approve` · `/reject` · `/restore` `{}` | `200` the lesson in its new state |
| `POST /v1/memory/agent/lessons/{id}/revert` `{revision}` | `200` back to that revision's text (index into `history`) |
| `DELETE /v1/memory/agent/lessons/{id}` | `204` archived |
| `GET /v1/memory/agent/subjects[?limit=]` | `200` subjects with history, most recent first |
| `GET /v1/memory/agent/subjects/{subject}` | `200` `{subject, events}`, oldest first |
| `DELETE /v1/memory/agent/subjects/{subject}` | `204` its history erased |
| `GET /v1/memory/agent/journal[?hours=1..168&subject=&limit=]` | `200` `{entries, dropped, hours}`, newest first |

Every POST needs a JSON body (`{}` for actions): it's the kit's CSRF defence, and anything else gets `415`.

Errors have one shape, `{"detail": {"code": "...", "message": "..."}}`:

| Status | `code` |
|---|---|
| `401` | `unauthenticated` |
| `403` | `not_memory_admin` |
| `404` | `memory_not_found`, `memory_off` |
| `409` | `memory_conflict` (too many concurrent writes; retry) |
| `415` | a body that isn't JSON |
| `422` | `empty`, `too_long`, `personal_data`, `instructions`, `duplicate`, `bad_subject`, `bad_event`, `not_pending`, `not_archived`, `archived`, `review_queue_full`, or FastAPI's validation list |
| `429` | `memory_rate_limited`, with `Retry-After` |
| `503` | `memory_unavailable`, with `Retry-After` |

The full schema is in the service's OpenAPI document at `/docs`.

## The console

The **Memory** page shows the signed-in person what the agent remembers about them, with add, edit, delete, export and erase. Memory admins also get:

- **Agent lessons**: the review queue (with where each lesson came from and the run that proposed it), what's in use and the archive; approve, reject, edit, roll back to an earlier text, archive, restore, or teach a lesson directly;
- **Subjects**: subjects with history, each one's events, and erasing one;
- **Journal**: recent runs by period and subject: what started them, outcome, tools, subjects, lessons proposed.

Changes are announced to screen readers. The page is loaded only when opened.

The **Security** page has two controls: **Long-term memory** (user memory) and **Agent memory**. Each shows *partial* with what to fix when the salt isn't set, the store is in-process, or (for agent memory) no memory admin is set; **Long-term memory** shows *off* with the reason when memory couldn't start.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `AGENTKIT_MEMORY` | `full` | `full`, `user` or `session`; can only lower what the agent's code asks for |
| `AGENTKIT_MEMORY_SALT` | none | Secret mixed into every memory key; 16+ characters. **Required in prod.** Never change it on a live service: existing memory becomes unreachable (it then expires on its own) |
| `AGENTKIT_MEMORY_ADMINS` | none | Comma-separated user ids (as the user header carries them) who review lessons and may read subjects and the journal |
| `AGENTKIT_MEMORY_STORE` | `inherit` | `inherit` uses `AGENTKIT_SESSION_STORE` and its connection; or `memory`, `redis`, `cosmos` |
| `AGENTKIT_MEMORY_RETENTION_DAYS` | `180` | User notes: days kept after their last update (1–3650) |
| `AGENTKIT_MEMORY_MAX_ITEMS` | `50` | User notes per person (1–500) |
| `AGENTKIT_MEMORY_MAX_CHARS` | `280` | Characters per note, lesson or event note (20–1000) |
| `AGENTKIT_MEMORY_MAX_LESSONS` | `100` | Active lessons (1–500) |
| `AGENTKIT_MEMORY_LESSON_RETENTION_DAYS` | `365` | Active lessons: days kept after their last change |
| `AGENTKIT_MEMORY_LESSON_REVIEW_DAYS` | `30` | How long a lesson waits for review, and how long an archived one can be restored |
| `AGENTKIT_MEMORY_SUBJECT_RETENTION_DAYS` | `365` | Subject events: days kept after they were written |
| `AGENTKIT_MEMORY_MAX_SUBJECT_EVENTS` | `200` | Events per subject (the oldest go first) |
| `AGENTKIT_MEMORY_JOURNAL_RETENTION_DAYS` | `90` | Journal pages: days kept after their hour |
| `AGENTKIT_MEMORY_TIMEOUT_SECONDS` | `2` | Per store call |
| `AGENTKIT_MEMORY_CACHE_SECONDS` | `15` | How long a user's notes are cached in the replica (`0` = off) |
| `AGENTKIT_MEMORY_WRITES_PER_MINUTE` | `30` | Per key (`0` = no limit) |
| `AGENTKIT_MEMORY_COSMOS_CONTAINER` | session container | Cosmos container for memory documents (ids start `memory:`, so they never collide with sessions) |
| `AGENTKIT_MEMORY_DEV_USER` | none | Local and test only: who to remember for (and treat as a memory admin) when a request has no sign-in. Refused in prod |

With `AGENTKIT_ENVIRONMENT=prod`, memory refuses to start without a salt, with the in-process store or with a dev user. An agent that gets memory by default then **still starts, without long-term memory**, logs the reason and shows it on the Security page; an agent that wires memory providers itself fails to start instead.

## Operations

- **Azure.** `azd env set AGENTKIT_MEMORY_SALT "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"` before the first `azd up`, and `azd env set AGENTKIT_MEMORY_ADMINS "you@contoso.com"`. The generated Bicep passes the salt as a secret and the admins as a setting; in prod the `azd` preprovision hook stops before deploying without the salt. Memory documents live in the session container, which already has per-item TTL on.
- **VPS.** `agentctl.py render` gives every agent (and the sample) its own salt, kept in `agents.yaml`; set `MEMORY_ADMINS=you@example.com` in `.env` for the reviewers. Memory uses the agent's own Redis user (`GET`, `SET`, `DEL`, `EVAL`, `PING` on `agentkit:<name>:*`), which is all it needs; the package's tests run every kind of memory as exactly that user. Never delete or edit an agent's `memory_salt`: `render` refuses a damaged one rather than replacing it.
- **Erasure requests.** A person erases their own memory from the console or with `DELETE /v1/memory`. A memory admin erases a subject's history with `DELETE /v1/memory/agent/subjects/{subject}`. Everything also expires on its own.
- **Multiple replicas.** Each replica caches a user's notes for `cache_seconds`; lessons, subjects and the journal are read from the store on each run.

## Testing

Generated services get `tests/test_memory.py`: over HTTP, with a scripted model, it checks that a note is recalled in a later conversation, is never shown to another user, can be reviewed, exported and erased, and that personal data is refused; with full memory also that the next run knows what was done for a customer, and that a lesson from someone who isn't an admin waits for review. `conftest.py` resets memory between tests.

In your own tests, `agent_memory(settings)` returns the agent's lessons, subjects and journal (the same instance the agent uses), `MemoryService(InMemoryMemoryStore(), salt="...")` gives a private store, and `run_context(user_id=..., trigger="schedule")` sets the caller for direct `agent.run(...)` calls.

## Not included yet

- **Search over memory is opt-in.** A user's notes (50 at most) and the agent's active lessons (up to 30 shown) go into each run; subject history is read through its tool. With [analytics](analytics.md) on, the agent also gets `search_memory` (hybrid SQL + vector search over lessons, subject events and the person's own notes).
- **No automatic extraction.** The agent remembers only through its tools. Nothing is mined from transcripts, and long conversations aren't summarised into memory before they're trimmed.
- **No built-in scheduler.** Mark scheduled runs with `run_context(trigger="schedule")` from your own job or event handler (for example an Odoo automated action calling the agent's API).
- **Screening uses the heuristic injection detector, not Prompt Shields, even in prod.** Recalled memory is still fenced as data, chat turns still pass the agent's Prompt Shields input guard, and lessons are reviewed by a person. See the package README for a workaround.
- **Cosmos DB hasn't run against a real account yet.** The stores are tested against a fake container with ETags, 412s and TTL, and against a real Redis server with the VPS stack's restricted user.
- **Evals run one conversation per case**, so cross-conversation memory is covered by the generated pytest tests rather than `evals/cases.yaml`.
- **LangGraph agents** don't take context providers yet.
