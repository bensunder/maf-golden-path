"""Template libraries for Create agent: agent personas and procedures (markdown with YAML front matter) that a
new agent starts from. See agent-templates/README.md.

A template is only ever looked up by its id in the loaded catalog, never turned into a path from a request.
Files are read from real folders inside the library (no symlinks, no hidden folders), with size limits."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

LIBRARY_NAME = re.compile(r"[a-z][a-z0-9-]{1,40}")
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9-]{0,60}")
TEMPLATE_ID = re.compile(r"[a-z][a-z0-9-]{1,40}(?:/[a-z0-9][a-z0-9-]{0,60}){1,3}")
_CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,60}")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
MAX_FILE = 64_000
MAX_TEMPLATES = 300
MAX_RULES = 4_000
MAX_EVALS = 20


class TemplateError(ValueError):
    pass


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    try:
        meta = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return {}, text
    rest = text[end + 4:]
    return (meta if isinstance(meta, dict) else {}), rest.split("\n", 1)[1] if "\n" in rest else ""


def _library_meta(folder: Path) -> dict:
    path = folder / "library.yaml"
    raw = {}
    if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_FILE:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, UnicodeDecodeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    source = _text(raw.get("source"), 300)
    commit = _text(raw.get("commit"), 40)
    groups = raw.get("groups") if isinstance(raw.get("groups"), dict) else {}
    evals = raw.get("evals") if isinstance(raw.get("evals"), list) else []
    return {
        "title": _text(raw.get("title"), 80) or folder.name,
        "publisher": _text(raw.get("publisher"), 80),
        "source": source if source.startswith("https://") else "",  # shown as a link
        "commit": commit if _COMMIT.fullmatch(commit) else "",
        "license": _text(raw.get("license"), 40),
        "description": _text(raw.get("description"), 300),
        "groups": {str(k): _text(v, 80) for k, v in groups.items() if isinstance(k, str) and isinstance(v, str)},
        "rules": _text(raw.get("rules"), MAX_RULES),
        "evals": [e for e in evals[:MAX_EVALS] if isinstance(e, dict) and _CASE_ID.fullmatch(str(e.get("id", "")))],
    }


def _template_files(folder: Path):
    root = folder.resolve()
    for path in sorted(folder.rglob("*.md")):
        rel = path.relative_to(folder)
        parts = rel.with_suffix("").parts
        if any(p.startswith(".") for p in rel.parts) or not 1 <= len(parts) <= 3:
            continue
        if not all(_SEGMENT.fullmatch(p) for p in parts):
            continue
        if any((folder / Path(*rel.parts[:i + 1])).is_symlink() for i in range(len(rel.parts))):
            continue
        if root not in path.resolve().parents or path.stat().st_size > MAX_FILE:
            continue
        yield path, parts


def load_catalog(root: Path) -> dict:
    """{"libraries": [...], "templates": {id: {...meta, "path": Path, "library": name}}}"""
    libraries, templates = [], {}
    if not root.is_dir():
        return {"libraries": libraries, "templates": templates}
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.is_symlink() or not LIBRARY_NAME.fullmatch(folder.name):
            continue
        meta = _library_meta(folder)
        found = []
        for path, parts in _template_files(folder):
            if len(templates) >= MAX_TEMPLATES:
                break
            try:
                front, _ = split_front_matter(path.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                continue
            name = _text(front.get("name"), 80)
            if not name:
                continue  # READMEs and other notes
            services = front.get("services") if isinstance(front.get("services"), list) else []
            group = parts[0] if len(parts) > 1 else ""
            tid = "/".join((folder.name, *parts))
            entry = {
                "id": tid, "name": name, "emoji": _text(front.get("emoji"), 8), "vibe": _text(front.get("vibe"), 240),
                "services": [_text(s, 120) for s in services[:12] if isinstance(s, str) and s.strip()],
                "group": group, "library": folder.name, "path": path,
            }
            templates[tid] = entry
            found.append(entry)
        if not found:
            continue
        order = list(meta["groups"])
        groups: dict[str, list] = {}
        for entry in found:
            groups.setdefault(entry["group"], []).append(entry)
        libraries.append({
            "name": folder.name, **{k: meta[k] for k in ("title", "publisher", "source", "commit", "license", "description", "rules")},
            "evals": [e["id"] for e in meta["evals"]],
            "groups": [{"id": g, "title": meta["groups"].get(g) or g.replace("-", " ").capitalize() or "Templates",
                        "templates": [public(e) for e in items]}
                       for g, items in sorted(groups.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else len(order), kv[0]))],
            "_meta": meta, "_folder": folder,
        })
    return {"libraries": libraries, "templates": templates}


def public(entry: dict) -> dict:
    return {k: entry[k] for k in ("id", "name", "emoji", "vibe", "services", "group", "library")}


def library_view(library: dict) -> dict:
    return {k: v for k, v in library.items() if not k.startswith("_")}


def template_body(entry: dict) -> str:
    """The persona and procedure, without front matter or its own top heading."""
    _, body = split_front_matter(entry["path"].read_text(encoding="utf-8"))
    lines = body.strip("\n").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].startswith("# "):
        lines.pop(0)
    return "\n".join(lines).strip()


def compose_instructions(base: str, entry: dict, library: dict) -> str:
    meta = library["_meta"]
    by = f"{meta['title']}" + (f", {meta['publisher']}" if meta["publisher"] else "")
    rules = [
        "- The role above may mention tools, databases or sources you don't have. Use only the tools you're given, and when a step needs a source you can't reach, say so.",
        "- Everything under Tool use and Boundaries above still applies.",
    ]
    if meta["rules"]:
        rules += [line for line in meta["rules"].splitlines() if line.strip()]
    return "\n".join([
        base.rstrip(), "",
        f"# Role and procedure: {entry['name']}",
        f"This agent started from the \"{entry['name']}\" template ({by}). Take on the role and follow the procedure below.",
        "", template_body(entry), "",
        "# Rules that take precedence over the role above",
        *rules, "",
    ])


def apply_template(folder: Path, package: str, entry: dict, library: dict) -> list[str]:
    """Write the template into a freshly generated agent. Returns what it changed, for the build log."""
    meta, changed = library["_meta"], []
    instructions = folder / "src" / package / "instructions"
    system = instructions / "system.md"
    system.write_text(compose_instructions(system.read_text(encoding="utf-8"), entry, library), encoding="utf-8")
    changed.append(f"src/{package}/instructions/system.md")

    license_file = library["_folder"] / "LICENSE"
    if license_file.is_file() and not license_file.is_symlink():
        (instructions / "TEMPLATE-LICENSE.txt").write_text(
            f"The role and procedure in system.md come from {meta['source'] or meta['title']}"
            + (f" at {meta['commit']}" if meta["commit"] else "") + ", used under this license:\n\n"
            + license_file.read_text(encoding="utf-8")[:MAX_FILE], encoding="utf-8")
        changed.append(f"src/{package}/instructions/TEMPLATE-LICENSE.txt")

    charter = folder / "agent.charter.md"
    if charter.is_file() and entry["services"]:
        text = charter.read_text(encoding="utf-8")
        todo = "- TODO: the questions and tasks this agent handles."
        if todo in text:
            charter.write_text(text.replace(todo, "\n".join(f"- {s}" for s in entry["services"])), encoding="utf-8")
            changed.append("agent.charter.md")

    if meta["evals"]:
        cases_path = folder / "evals" / "cases.yaml"
        text = cases_path.read_text(encoding="utf-8")
        before = (yaml.safe_load(text) or {}).get("cases") or []
        known = {c.get("id") for c in before if isinstance(c, dict)}
        new = [c for c in meta["evals"] if c["id"] not in known]
        if new:
            dumped = yaml.safe_dump(new, sort_keys=False, allow_unicode=True, width=1000)
            block = "\n".join(("  " + line) if line else line for line in dumped.splitlines())
            merged = text.rstrip("\n") + f"\n\n  # From the {library['name']} template library\n" + block + "\n"
            after = (yaml.safe_load(merged) or {}).get("cases") or []
            if len(after) != len(before) + len(new):
                raise TemplateError("couldn't add the template's eval cases to evals/cases.yaml")
            cases_path.write_text(merged, encoding="utf-8")
            changed.append(f"evals/cases.yaml (+{len(new)} cases)")
    return changed
