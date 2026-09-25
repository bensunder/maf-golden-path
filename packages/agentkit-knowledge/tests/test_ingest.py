"""Ingestion: access rules, extraction, chunking, incremental sync, fail-closed pruning."""

import httpx
import pytest

from agentkit.knowledge import KnowledgeBase, KnowledgeSettings, StaticGroups
from agentkit.knowledge.ingest import (
    AclRules,
    DocumentIntelligence,
    SourceDocument,
    chunk_markdown,
    index_definition,
    ingest,
    local_documents,
    main,
)
from agentkit.knowledge.testing import FakeIndexClient, FakeSearchIndex, fake_embedder, fake_knowledge, index_folder

LEADS = "00000000-0000-0000-0000-00000000a11d"
ACL = f"""rules:
  - match: "leads/**"
    groups: ["{LEADS}"]
  - match: "*.md"
    groups: ["all-users"]
  - match: "*.html"
    groups: ["all-users"]
"""


@pytest.fixture
def corpus(tmp_path):
    (tmp_path / "acl.yaml").write_text(ACL)
    (tmp_path / "refund-policy.md").write_text(
        "---\ntitle: Refund policy\nurl: https://intranet/refunds\n---\n# Refund policy\n\n## Approvals\n\n"
        "Refunds over $50 need approval from a refunds lead.\n\n## Timing\n\nRefunds post within 5 days.\n")
    (tmp_path / "leads").mkdir()
    (tmp_path / "leads" / "exceptions.md").write_text("# Exceptions\n\nLeads may approve up to $500 for loyal customers.\n")
    (tmp_path / "shipping.html").write_text("<html><head><title>Shipping FAQ</title><style>x{}</style></head>"
                                            "<body><h2>Carriers</h2><p>We ship with UPS.</p></body></html>")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "salaries.txt").write_text("salary bands")  # no rule matches
    return tmp_path


async def embed_many(texts):
    embed = fake_embedder()
    return [await embed(t) for t in texts]


def settings(**kw):
    return KnowledgeSettings(_env_file=None, **kw)


async def run(path, index, **kw):
    return await ingest(local_documents(path), search_client=index, embed_many=embed_many, knowledge=settings(), **kw)


async def test_access_rules_and_fail_closed_default(corpus):
    index = FakeSearchIndex()
    report = await run(corpus, index)
    assert sorted(report.added) == ["leads-exceptions", "refund-policy", "shipping"]
    assert report.skipped == [("secret/salaries.txt", "no acl.yaml rule matches: not indexed")]
    groups = {d["doc_id"]: d["groups"] for d in index.docs.values()}
    assert groups["leads-exceptions"] == [LEADS] and groups["refund-policy"] == ["all-users"]
    assert all(len(d["content_vector"]) == 8 for d in index.docs.values())


async def test_extraction_titles_urls_and_sections(corpus):
    index = FakeSearchIndex()
    await run(corpus, index)
    policy = sorted((d for d in index.docs.values() if d["doc_id"] == "refund-policy"), key=lambda d: d["chunk"])
    assert [d["title"] for d in policy] == ["Refund policy › Approvals", "Refund policy › Timing"]
    assert policy[0]["url"] == "https://intranet/refunds" and "title: Refund policy" not in policy[0]["content"]
    shipping = next(d for d in index.docs.values() if d["doc_id"] == "shipping")
    assert shipping["title"].startswith("Shipping FAQ") and "UPS" in shipping["content"] and "x{}" not in shipping["content"]


async def test_incremental_sync(corpus):
    index = FakeSearchIndex()
    await run(corpus, index)
    again = await run(corpus, index)
    assert again.added == again.updated == again.removed == [] and len(again.unchanged) == 3

    (corpus / "refund-policy.md").write_text("# Refund policy\n\nRefunds over $75 need approval.\n")
    (corpus / "shipping.html").unlink()
    changed = await run(corpus, index)
    assert changed.updated == ["refund-policy"] and changed.removed == ["shipping"]
    policy = [d for d in index.docs.values() if d["doc_id"] == "refund-policy"]
    assert len(policy) == 1 and "$75" in policy[0]["content"]  # stale chunks of the old version are gone


async def test_access_change_reindexes_and_removed_rule_removes(corpus):
    index = FakeSearchIndex()
    await run(corpus, index)
    (corpus / "acl.yaml").write_text(ACL.replace('"*.md"', '"refund-*.md"').replace(f'"{LEADS}"', '"g-new-leads"'))
    report = await run(corpus, index)
    assert report.updated == ["leads-exceptions"]  # same text, new access: re-indexed
    assert {d["groups"][0] for d in index.docs.values() if d["doc_id"] == "leads-exceptions"} == {"g-new-leads"}


async def test_rule_removed_takes_the_document_out(corpus):
    index = FakeSearchIndex()
    await run(corpus, index)
    (corpus / "acl.yaml").write_text(ACL.replace('  - match: "leads/**"\n    groups: ["' + LEADS + '"]\n', ""))
    report = await run(corpus, index)
    assert "leads-exceptions" in report.removed
    assert not [d for d in index.docs.values() if d["doc_id"] == "leads-exceptions"]


async def test_invalid_groups_are_refused(tmp_path):
    doc = SourceDocument("a.md", b"# A\n\ntext", ["ok-group", "bad group'"])
    report = await ingest([doc], search_client=FakeSearchIndex(), embed_many=None, knowledge=settings())
    assert "invalid or empty access groups" in report.skipped[0][1]


async def test_dry_run_writes_nothing(corpus):
    index = FakeSearchIndex()
    report = await run(corpus, index, dry_run=True)
    assert len(report.added) == 3 and index.docs == {}


async def test_index_is_created_with_the_expected_schema(corpus):
    indexes = FakeIndexClient()
    await ingest(local_documents(corpus), search_client=FakeSearchIndex(), embed_many=embed_many,
                 knowledge=settings(index="kb"), index_client=indexes)
    fields = {f.name: f for f in indexes.indexes["kb"].fields}
    assert fields["id"].key and fields["groups"].filterable
    assert fields["content_vector"].vector_search_dimensions == 1536
    assert indexes.indexes["kb"].semantic_search.configurations[0].name == "default"


def test_index_definition_without_semantic():
    assert index_definition("x", semantic_configuration=None).semantic_search is None


async def test_pdf_needs_document_intelligence_and_uses_it(tmp_path):
    (tmp_path / "acl.yaml").write_text('rules:\n  - match: "**"\n    groups: ["all-users"]\n')
    (tmp_path / "handbook.pdf").write_bytes(b"%PDF-1.7 fake")
    skipped = await run(tmp_path, FakeSearchIndex())
    assert "needs Document Intelligence" in skipped.skipped[0][1]

    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(202, headers={"operation-location": "https://di.example/ops/1"})
        return httpx.Response(200, json={"status": "succeeded",
                                         "analyzeResult": {"content": "# Handbook\n\nRefunds post in 5 days."}})

    class NoAuth:
        async def headers(self):
            return {"Authorization": "Bearer t"}

    di = DocumentIntelligence("https://di.example", auth=NoAuth(), poll_seconds=0,
                              http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    index = FakeSearchIndex()
    report = await run(tmp_path, index, docintel=di)
    assert report.added == ["handbook"] and "Refunds post" in next(iter(index.docs.values()))["content"]
    assert seen[0].headers["content-type"] == "application/octet-stream"
    assert "outputContentFormat=markdown" in str(seen[0].url)


def test_chunking_respects_size_headings_and_overlap():
    text = "# Guide\n\n## Part A\n\n" + " ".join(f"Sentence {i} is here." for i in range(200)) + "\n\n## Part B\n\nShort."
    chunks = chunk_markdown(text, max_chars=400, overlap=80)
    assert all(len(body) <= 400 for _, body in chunks)
    assert chunks[0][0] == "Guide › Part A" and chunks[-1] == ("Guide › Part B", "Short.")
    a1, a2 = chunks[0][1], chunks[1][1]
    assert a2.split("\n\n")[0] in a1  # the next chunk starts with the tail of the previous one


def test_acl_globs_are_path_aware():
    rules = AclRules.from_yaml('rules:\n  - match: "*.md"\n    groups: [all-users]\n  - match: "docs/**/*.pdf"\n    groups: [g]\n')
    assert rules.groups_for("faq.md") == ["all-users"]
    assert rules.groups_for("leads/secret.md") is None  # '*' doesn't cross folders
    assert rules.groups_for("docs/a/b/x.pdf") == ["g"] and rules.groups_for("docs/x.pdf") == ["g"]


def test_acl_first_match_wins():
    rules = AclRules.from_yaml('rules:\n  - match: "hr/**"\n    groups: [g-hr]\n  - match: "**"\n    groups: [all-users]\n')
    assert rules.groups_for("hr/pay/bands.md") == ["g-hr"] and rules.groups_for("faq.md") == ["all-users"]


async def test_folder_fixture_end_to_end_with_trimming(corpus):
    (corpus / "secret" / "salaries.txt").unlink()
    index = index_folder(corpus)
    kb = KnowledgeBase(settings())
    with fake_knowledge(kb, index, groups=StaticGroups({"sam": [], "riley": [LEADS]})):
        sam = {p.doc_id for p in await kb.search("approve refunds loyal customers", user_id="sam")}
        riley = {p.doc_id for p in await kb.search("approve refunds loyal customers", user_id="riley")}
    assert "leads-exceptions" not in sam and "leads-exceptions" in riley


def test_cli_needs_an_endpoint(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTKIT_KNOWLEDGE_SEARCH_ENDPOINT", raising=False)
    assert main(["--source", str(tmp_path)]) == 2
    assert "AGENTKIT_KNOWLEDGE_SEARCH_ENDPOINT" in capsys.readouterr().err
