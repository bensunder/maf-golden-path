# Agents that call other agents

On a VPS with the platform service, one agent can ask another for help: a claims agent asks the order agent for a refund, a legal intake agent asks a contracts specialist. Every call keeps the same rules as a person talking to that agent directly, and adds more of its own:

- **It acts for the person who started it.** The platform signs a delegation for each person's request. Agents pass it along and can't change it. No agent can pick another user, and nothing can be sent in the name of someone who isn't there.
- **Only where an admin allowed it.** Each agent has a list of the agents it may call ("Can call" on the Agents page). There are no loops. A chain goes at most 3 agents deep, and one person's request may make at most 8 calls in total.
- **The called agent runs everything it always runs.** Its prompt-injection checks, PII redaction, tool policy, output scanning, token budget and approvals all apply. The caller doesn't get to skip any of them.
- **Its approvals go back to the person.** When the called agent needs approval (a refund over the limit, say), the person sees it in the chat they're using, with exactly what will happen. The action runs only if what they approved matches what the other agent is still waiting for: the same action, the same arguments. Anything else, and nothing runs.
- **Every agent only accepts requests the platform signed for it.** Something else on the Docker network can't pose as a person or as another agent.
- **One trace per request.** Each call is a span in both agents' traces, so the whole chain shows up in Application Insights, OTLP or LangSmith as one trace.

MAF agents and [LangGraph agents](langgraph.md) can call each other both ways, under the same rules.

## Setting it up

The platform must be on (`agentctl.py platform --admins ...`) and the agents served by it (`agentctl.py add --internal`, or Create agent in the console).

**In the console:** **Agents** → the agent's **Can call** → tick the agents it may ask → **Save and restart**. The **Agent network** page shows who may call whom, and the recent calls with their outcome.

**On the command line:**

```bash
python3 agentctl.py peers claims-desk sample,legal   # claims-desk may call the sample and legal
python3 agentctl.py peers claims-desk none           # it may call no one
docker compose up -d
```

Each agent it may call becomes a tool named `ask_<name>`, described with that agent's title and description, so the model knows when to use it. Tell the agent in `instructions/system.md` when it should hand work over.

## When the other agent needs approval

1. Claims Desk calls `ask_sample` with "refund A1002 $129". The sample's refund tool needs approval, so the sample pauses and answers with an approval ticket and the exact actions it wants to take.
2. Claims Desk calls `confirm_agent_action` with that ticket and those actions. That tool always needs the person's approval, so the person sees an approval card in Claims Desk's chat: which agent, which action, which arguments.
3. The person approves. The platform checks that the ticket belongs to this person and this pair of agents, that it hasn't been used, and that what the sample has pending right now is exactly what the person approved. Only then does it pass the decision on, and the sample runs the refund.

If the person rejects, nothing is passed on. Tickets are used once, and they stop working when the conversation moves on. An agent can't approve anything by itself: decisions only pass during the request in which the person clicks Approve, and only for two minutes after it.

## How it's secured

| | |
|---|---|
| Request signing | Each agent gets its own key, derived from `PLATFORM_SECRET_KEY`. The platform signs the method, path, user, delegation, body, time and a one-time nonce. The agent refuses unsigned, altered, replayed or stale requests (`AGENTKIT_PLATFORM_KEY`). |
| Delegation | A signed token that says who the request acts for, which agent it's for, the chain of agents so far and when it expires. The router replaces any delegation header a browser sends. |
| Calls | Go only through the platform's gateway (`/agents/<name>/turn`). It checks the caller's token, the delegation, the allow-list, loops, depth and the budget, then signs the request for the called agent. |
| Approvals | The "what you approved is what runs" check above, with single-use tickets and a lock per conversation. |
| Sessions | Each agent has its own Redis user, limited to its own keys and to the few commands sessions need. It can't read another agent's conversations, list keys or change the server's configuration. |
| Containers | Agents run with `no-new-privileges`, without `NET_RAW` (no spoofed packets on the network), and aren't published on the host. |

What this doesn't cover: the agents on one server still share one host and one Docker network. Run agents that must not share a machine on separate servers, or on Azure.

## Limits

- Calls between agents are for agents on the same VPS platform. On Azure, give each agent the other's API as a connector with on-behalf-of auth ([connectors.md](connectors.md)).
- The web chat and console send approvals over `/v1/agui` or `/v1/sessions/<id>/approvals`. Those are the requests the router marks as decisions. A custom channel at another path can call other agents, but it can't approve their actions.
