"""Policy documents: every file has an access rule, and each person only finds what they may read."""

from pathlib import Path

from agentkit.knowledge.ingest import local_documents

from order_status_agent.tools import KNOWLEDGE

ROOT = Path(__file__).resolve().parents[1]
SAM, RILEY = "sam@contoso.example", "riley@contoso.example"  # support agent, refunds lead


def test_every_document_has_an_access_rule():
    skipped = [item for item in local_documents(ROOT / "knowledge") if isinstance(item, tuple)]
    assert skipped == [], f"add rules to knowledge/acl.yaml for: {skipped}"


async def test_support_agents_never_see_the_leads_playbook():
    query = "goodwill refund above order total exceptions"
    sam = {p.doc_id for p in await KNOWLEDGE.search(query, user_id=SAM)}
    riley = {p.doc_id for p in await KNOWLEDGE.search(query, user_id=RILEY)}
    assert "refund-leads-exceptions-playbook" not in sam and "refund-policy" in sam
    assert "refund-leads-exceptions-playbook" in riley


async def test_citations_link_to_the_intranet():
    (passage, *_) = await KNOWLEDGE.search("who can refund over $50", user_id=SAM)
    assert passage.doc_id == "refund-policy"
    assert passage.url == "https://intranet.contoso.example/policies/refunds"
    assert passage.title.startswith("Refund policy")
