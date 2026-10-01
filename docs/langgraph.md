# LangGraph agents

Some teams already think in graphs: triage, then retrieve, then draft, then review. agentkit runs a LangGraph graph as a Microsoft Agent Framework agent, so it gets everything a MAF agent gets from the kit, with no extra wiring:

- the same model access (the AI gateway, or the VPS model settings), with PII redacted before anything reaches the model;
- the same tools, written once with MAF's `@tool`; every call runs the tool policy and the output scan, and risky tools pause for a person's approval;
- prompt-injection checks, the token budget, sessions, the HTTP API, web chat, Teams, the console and the quality gate;
- telemetry: an `invoke_agent` span with model and tool spans under it, to Application Insights, OTLP or [LangSmith](telemetry.md#langsmith);
- calls to and from other agents on the VPS platform ([multi-agent.md](multi-agent.md)).

## Create one

In the console: **Create agent** → **Built with: LangGraph**. Or with copier:

```bash
copier copy --trust --vcs-ref v0.10.2 --data framework=langgraph --data project_name='Claims Desk' gh:bensunder/maf-golden-path claims-desk
```

The project is the same as a MAF one, plus `graph.py`:

```python
def build_graph(model, tools, instructions: str) -> StateGraph:
    bound = model.bind_tools(tools) if tools else model

    async def agent(state: MessagesState) -> dict:
        return {"messages": [await bound.ainvoke([SystemMessage(instructions), *state["messages"]])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    return graph
```

Change its shape freely. The kit hands it:

- `model`, a LangChain chat model over the kit's client. It calls one tool at a time, so each call gets its own approval.
- `tools`, the tools in `tools.py`, governed as described above. Call them from `ToolNode` or from your own nodes, one after another or at the same time (`asyncio.gather`).
- `instructions`, from `instructions/system.md`.

Return the uncompiled `StateGraph`. The kit compiles it with a checkpointer.

## Approvals

A tool marked `approval_mode="always_require"` pauses the graph, unless one of your `APPROVAL_RULES` approves the call. The person sees the approval in the web chat, console or Teams, and the graph resumes with their decision on whichever replica gets it. The paused graph is kept in the session store (Redis or Cosmos DB), not in the process.

Each call is decided on its own, including several risky calls made by one node. A call that already ran is never run again when the node resumes. A call the person rejected doesn't run, and the model is told so.

## State you add

The checkpoint is read back with LangGraph's strict allow-list: messages, LangGraph's own types, and nothing else. Stored data never gets to name a class to construct. If your state holds your own classes, list them:

```python
build_graph_agent(..., graph=build_graph, checkpoint_types=[ClaimState])
```

## Testing

The generated tests use `ScriptedChatClient`, exactly as for MAF agents. The script's tool calls go through your graph, your tools and the kit's middleware, and evals in `evals/cases.yaml` run the same way. `make test` in the kit renders a LangGraph project and runs its tests.

## When to choose which

| | MAF | LangGraph |
|---|---|---|
| Shape | A model with tools, looping until it answers | Any graph you draw: branches, loops, parallel steps, several model calls |
| Code you write | Tools and instructions | Tools, instructions and `graph.py` |
| Everything else | The kit | The kit, the same way |

Start with MAF. Switch to LangGraph when the steps need to be explicit.
