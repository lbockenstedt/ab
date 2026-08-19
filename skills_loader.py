"""
skills_loader.py — AppBuilder loads the repo-committed Claude skills ("agents") from
the LM repo (.claude/skills) so its fix / build / PR-review work follows the same
recipes + boundaries a human invoking the skill would.

Single source of truth: the skills live in lbockenstedt/lm/.claude/skills (see
lm/docs/agents-and-skills.md). AppBuilder reads them via the GitHub contents API,
caches with a TTL, and exposes skills_context() to inject a compact "follow these"
block into fix prompts.

Config: skills_enabled (default True), skills_repo (default "lbockenstedt/lm"),
        skills_path (default ".claude/skills"), skills_ttl_s (default 3600).

Defensive: never raises out — a skills-loader problem must not break a scan/fix.
"""
import re
import threading
import time

try:
    from main import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger("skills_loader")

_CACHE = {"skills": {}, "ts": 0.0}
_LOCK = threading.Lock()


def _parse_description(body):
    """Pull `description:` out of a SKILL.md YAML frontmatter (handles a `>-`
    block scalar spanning lines)."""
    if not body:
        return ""
    m = re.search(r"^---\s*\n(.*?)\n---", body, re.S)
    fm = m.group(1) if m else body[:1500]
    dm = re.search(r"^description:\s*(.*)$", fm, re.M)
    if not dm:
        return ""
    first = dm.group(1).strip()
    if first in (">-", ">", "|", "|-"):  # block scalar → gather indented lines
        lines = []
        started = False
        for ln in fm.splitlines():
            if re.match(r"^description:\s*[>|]", ln):
                started = True
                continue
            if started:
                if re.match(r"^\S", ln):  # next top-level key
                    break
                lines.append(ln.strip())
        return " ".join(x for x in lines if x)
    return first.strip("\"'")


def load_skills(gh, config, force=False):
    """Fetch + cache the skills. Returns {name: {name, description, instructions,
    reference}}. Keeps the last-known set on any fetch error."""
    try:
        if not config.get("skills_enabled", True):
            return {}
        ttl = int(config.get("skills_ttl_s", 3600) or 3600)
        with _LOCK:
            if not force and _CACHE["skills"] and (time.time() - _CACHE["ts"]) < ttl:
                return _CACHE["skills"]
        repo_name = config.get("skills_repo", "lbockenstedt/lm")
        path = config.get("skills_path", ".claude/skills")
        skills = {}
        repo = gh.get_repo(repo_name)
        try:
            entries = repo.get_contents(path)
        except Exception:
            entries = []  # path not present yet (e.g. skills PR not merged)
        if not isinstance(entries, list):
            entries = [entries]
        for e in entries:
            if getattr(e, "type", "") != "dir":
                continue
            name = e.name
            skill = {"name": name, "description": "", "instructions": "", "reference": ""}
            try:
                for f in repo.get_contents(e.path):
                    if f.name == "SKILL.md":
                        body = (f.decoded_content or b"").decode("utf-8", "replace")
                        skill["instructions"] = body
                        skill["description"] = _parse_description(body)
                    elif f.name == "reference.md":
                        skill["reference"] = (f.decoded_content or b"").decode("utf-8", "replace")
            except Exception:
                pass
            skills[name] = skill
        with _LOCK:
            _CACHE["skills"] = skills
            _CACHE["ts"] = time.time()
        logger.info("skills_loader: loaded %d project skill(s): %s",
                    len(skills), ", ".join(sorted(skills)) or "none")
        return skills
    except Exception as e:  # noqa: BLE001
        logger.info("skills_loader: load failed (%s) — keeping last-known %d", e, len(_CACHE["skills"]))
        return _CACHE["skills"]


def get_loaded():
    """The last-loaded skills dict (no fetch)."""
    return dict(_CACHE["skills"])


def skill_files(name):
    """The raw {"SKILL.md": ..., "reference.md": ...} for one loaded skill
    (only the two keys that were actually present are included). Empty dict
    if the skill isn't loaded — never raises, matching this module's
    defensive-by-design contract."""
    s = _CACHE["skills"].get(name)
    if not s:
        return {}
    out = {}
    if s.get("instructions"):
        out["SKILL.md"] = s["instructions"]
    if s.get("reference"):
        out["reference.md"] = s["reference"]
    return out


def skill_instructions(name, max_chars=40000):
    """SKILL.md + reference.md concatenated for one skill, for in-prompt
    injection into a build agent's context (skills_context() above only
    exposes names+descriptions, which is enough to PICK a skill but not
    enough to follow its recipe). Empty string if the skill isn't loaded."""
    files = skill_files(name)
    if not files:
        return ""
    parts = []
    if "SKILL.md" in files:
        parts.append(files["SKILL.md"])
    if "reference.md" in files:
        parts.append(f"--- reference.md ---\n{files['reference.md']}")
    return "\n\n".join(parts)[:max_chars]


def skills_context(max_chars=4000):
    """Compact 'project skills — follow these' block for injecting into a fix/build
    prompt. Names + descriptions only (keeps the prompt lean); the fixer can be
    told to honor them. Empty string when no skills are loaded."""
    skills = _CACHE["skills"]
    if not skills:
        return ""
    lines = ["## Project skills — FOLLOW these recipes + boundaries when they apply:"]
    for name, s in sorted(skills.items()):
        desc = (s.get("description") or "").strip().replace("\n", " ")
        lines.append(f"- **{name}**: {desc[:400]}")
    return "\n".join(lines)[:max_chars]
