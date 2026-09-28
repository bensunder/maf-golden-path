# Agent template libraries

**Create agent** in the console can start a new agent from a template: a persona and working procedure written by a specialist. The platform generates the agent from the kit's template as usual, then:

- puts the persona into `src/<package>/instructions/system.md`, below the kit's own rules (tool use, boundaries);
- adds the library's `rules` after the persona, marked as taking precedence over it;
- fills the charter's scope from the template's `services`;
- appends the library's eval cases to `evals/cases.yaml`;
- copies the library's license next to the instructions.

The agent is then yours to edit: templates are a starting point, not a dependency.

## Libraries here

| Folder | What | Source | License |
|---|---|---|---|
| `legal-agents/` | 30 legal specialists: practice areas, legal functions, jurisdictions | [judicialmind/legal-agents](https://github.com/judicialmind/legal-agents) at `20587b4` | MIT |

## Adding a library

A library is a folder here with markdown files, each starting with YAML front matter:

```markdown
---
name: Contract Lifecycle Manager          # required
emoji: 📝
vibe: One line on what it does.
services:
  - Contract review and risk analysis
---

# Contract Lifecycle Manager
You are ...
```

Files without a `name` in their front matter (READMEs) are skipped. Subfolders become groups. An optional `library.yaml` gives the library a title, source, license, group headings, `rules` and `evals` (see `legal-agents/library.yaml`).

On a VPS, `python3 agentctl.py templates add <name> <git-url> [--ref <tag or commit>]` clones a library here. Read the files before you use them: a template becomes an agent's instructions, so it deserves the same review as code. `agentctl.py templates list` shows what's installed. Libraries you add this way are ignored by git.
