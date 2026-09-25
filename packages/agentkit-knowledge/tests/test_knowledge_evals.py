"""The eval checks for knowledge: cites, must_not_retrieve, and running a case as a user."""

from agentkit.hosting import AgentKitSettings, build_agent, format_source
from agentkit.knowledge import KnowledgeBase, KnowledgeSettings, StaticGroups, knowledge_tool
from agentkit.knowledge.testing import FakeSearchIndex, fake_embedder
from agentkit.testing.evals import _SOURCE_LINE, EvalCase, run_case

from test_knowledge import DOCS

GROUPS = StaticGroups({"sam": ["g-support"], "riley": ["g-support", "g-leads"]})


def factory(client):
    kb = KnowledgeBase(KnowledgeSettings(_env_file=None), search_client=FakeSearchIndex(DOCS), embed=fake_embedder(),
                       groups=GROUPS)
    settings = AgentKitSettings(environment="test", guardrail_mode="heuristic", _env_file=None)
    return build_agent(name="kb", instructions="x", tools=[knowledge_tool(kb)], settings=settings, client=client)


def case(user, answer, **expect):
    return EvalCase(id="c", input="refund exceptions?", user=user, expect=expect, script=[
        {"tool": "search_knowledge", "args": {"query": "refund approval exceptions loyal"}}, {"reply": answer}])


async def test_cites_and_trimming_pass_for_the_right_user():
    result = await run_case(factory, case("sam", "Over $50 needs a lead [1].", cites=["refund-policy"],
                                          must_not_retrieve=["leads-playbook", "hr-salaries"]))
    assert result.passed, result.failures


async def test_must_not_retrieve_catches_a_leak():
    result = await run_case(factory, case("riley", "Leads may go to $500 [1].", must_not_retrieve=["leads-playbook"]))
    assert not result.passed and "leads-playbook" in result.failures[0]


async def test_made_up_citation_numbers_fail():
    result = await run_case(factory, case("sam", "Over $50 needs a lead [1][3].", cites=["refund-policy"]))
    assert result.failures == ["cites ['[3]'] but no such source was retrieved"]


async def test_uncited_answer_fails_cites():
    result = await run_case(factory, case("sam", "Over $50 needs a lead.", cites=["refund-policy"]))
    assert result.failures == ["answer doesn't cite 'refund-policy' (cited: nothing)"]


async def test_without_a_user_the_search_finds_nothing():
    result = await run_case(factory, case(None, "No idea.", must_not_retrieve=["refund-policy"]))
    assert result.passed and "no signed-in user" in result.tool_calls[0].result


def test_eval_parser_matches_the_shared_format():
    line = format_source(7, "doc id", "A · B", "https://x/y z", "text").splitlines()[0]
    match = _SOURCE_LINE.match(line)
    assert match and match.groups() == ("7", "doc_id")
