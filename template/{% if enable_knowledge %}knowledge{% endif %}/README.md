# Knowledge

Documents the agent can search. Add Markdown, text, HTML, or PDF/Office files (those need Document
Intelligence, which `azd up` creates). Every file needs a rule in `acl.yaml`, or it isn't indexed.

Every deploy syncs this folder to Azure AI Search (unchanged files are skipped; removed files leave the index). To run it yourself:

    agentkit-ingest --source knowledge            # add --dry-run to preview

Tests and offline evals search this folder directly (see `tests/conftest.py`).
