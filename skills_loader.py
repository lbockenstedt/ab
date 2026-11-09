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
# re parses the YAML frontmatter at the top of each SKILL.md.
import json
import re
# threading.Lock guards the shared cache against concurrent loads.
import threading
# time stamps the cache so it can expire after the configured TTL.
import time

# Use the app's shared logger when running inside AppBuilder ...
try:
    from main import logger
# ... but fall back to a standalone logger when imported on its own (e.g. in tests).
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger("skills_loader")

# Module-level cache of loaded skills plus the time they were fetched.
_CACHE = {"skills": {}, "ts": 0.0}
# Lock so two threads never update the cache at the same time.
_LOCK = threading.Lock()


def _parse_description(body):
    """Pull `description:` out of a SKILL.md YAML frontmatter (handles a `>-`
    block scalar spanning lines)."""
    # Empty body: no description to find.
    if not body:
        return ""
    # Grab the YAML frontmatter between the leading "---" lines (or the first 1500 chars).
    m = re.search(r"^---\s*\n(.*?)\n---", body, re.S)
    fm = m.group(1) if m else body[:1500]
    # Find the "description:" key in the frontmatter.
    dm = re.search(r"^description:\s*(.*)$", fm, re.M)
    # No description key present.
    if not dm:
        return ""
    # The value written on the same line as "description:".
    first = dm.group(1).strip()
    if first in (">-", ">", "|", "|-"):  # block scalar → gather indented lines
        # Accumulate the indented continuation lines that make up the block value.
        lines = []
        started = False
        # Scan the frontmatter line by line.
        for ln in fm.splitlines():
            # The description key line itself starts the block; skip past it.
            if re.match(r"^description:\s*[>|]", ln):
                started = True
                continue
            # Once inside the block, collect lines until the next top-level key.
            if started:
                if re.match(r"^\S", ln):  # next top-level key
                    break
                lines.append(ln.strip())
        # Join the collected lines into one sentence, skipping blank ones.
        return " ".join(x for x in lines if x)
    # Plain single-line value: strip any surrounding quotes.
    return first.strip("\"'")


def load_skills(gh, config, force=False):
    """Fetch + cache the skills. Returns {name: {name, description, instructions,
    reference}}. Keeps the last-known set on any fetch error."""
    # Never let a loader failure escape; fall back to the cached set (see except below).
    try:
        # Skills can be disabled entirely via config.
        if not config.get("skills_enabled", True):
            return {}
        # How long (seconds) a fetched set of skills stays fresh.
        ttl = int(config.get("skills_ttl_s", 3600) or 3600)
        # Serve from cache when it is still fresh, unless a reload was forced.
        with _LOCK:
            if not force and _CACHE["skills"] and (time.time() - _CACHE["ts"]) < ttl:
                return _CACHE["skills"]
        # Which repo and directory the skills are read from.
        repo_name = config.get("skills_repo", "lbockenstedt/lm")
        path = config.get("skills_path", ".claude/skills")
        # Fresh result being built, keyed by skill name.
        skills = {}
        # Open the repo and list the skills directory.
        repo = gh.get_repo(repo_name)
        try:
            entries = repo.get_contents(path)
        except Exception:
            entries = []  # path not present yet (e.g. skills PR not merged)
        # A single file comes back as one object, not a list; normalise to a list.
        if not isinstance(entries, list):
            entries = [entries]
        # Each skill is a sub-directory; ignore plain files at the top level.
        for e in entries:
            if getattr(e, "type", "") != "dir":
                continue
            # Start every skill with empty fields so missing files just leave blanks.
            name = e.name
            skill = {"name": name, "description": "", "instructions": "", "reference": ""}
            # Read the files inside the skill directory.
            try:
                for f in repo.get_contents(e.path):
                    # SKILL.md holds the instructions plus the frontmatter description.
                    if f.name == "SKILL.md":
                        body = (f.decoded_content or b"").decode("utf-8", "replace")
                        skill["instructions"] = body
                        skill["description"] = _parse_description(body)
                    # reference.md is optional extra reference material.
                    elif f.name == "reference.md":
                        skill["reference"] = (f.decoded_content or b"").decode("utf-8", "replace")
            # A broken skill directory is skipped silently; it is still listed with empty fields.
            except Exception:
                pass
            # Record the skill under its directory name.
            skills[name] = skill
        # The fleet custom agents (.github/agents/*.agent.md) -- the same files a
        # person selects with /agent in Copilot -- load alongside the skills.
        skills.update(_load_agents(repo, config.get("agents_path", ".github/agents")))
        # Replace the cache with the freshly loaded set and note when it was loaded.
        with _LOCK:
            _CACHE["skills"] = skills
            _CACHE["ts"] = time.time()
        # Log what was loaded so operators can see which skills are active.
        logger.info("skills_loader: loaded %d project skill(s): %s",
                    len(skills), ", ".join(sorted(skills)) or "none")
        return skills
    # Any unexpected failure: log it and keep serving the last-known skills.
    except Exception as e:  # noqa: BLE001
        logger.info("skills_loader: load failed (%s) — keeping last-known %d", e, len(_CACHE["skills"]))
        return _CACHE["skills"]


def _frontmatter_value(body, key):
    """One scalar from a markdown file's YAML frontmatter (also nested one level,
    e.g. ``ab-match`` under ``metadata:``); "" when absent."""
    m = re.search(r"^---\s*\n(.*?)\n---", body or "", re.S)
    if not m:
        return ""
    vm = re.search(r"^\s*%s:\s*(.*)$" % re.escape(key), m.group(1), re.M)
    if not vm:
        return ""
    val = vm.group(1).strip()
    # YAML double-quoted escapes ("\\b") match JSON's; single quotes are literal.
    if val.startswith('"'):
        try:
            return json.loads(val)
        except ValueError:
            return val.strip('"')
    return val.strip("'")


def _load_agents(repo, path):
    """{name: entry} for every ``*.agent.md`` under ``path`` in ``repo``.

    Entries share the skill shape (so skill_instructions() works on them) plus
    kind="agent" and ``match``, the ``metadata.ab-match`` regex AB uses to pick
    the agent for an issue or PR."""
    out = {}
    try:
        entries = repo.get_contents(path)
    except Exception:  # noqa: BLE001 - no agents dir yet
        return out
    for f in entries if isinstance(entries, list) else [entries]:
        if not f.name.endswith(".agent.md"):
            continue
        try:
            body = (f.decoded_content or b"").decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        name = f.name[:-len(".agent.md")]
        out[name] = {"name": name, "kind": "agent", "instructions": body, "reference": "",
                     "description": _parse_description(body),
                     "match": _frontmatter_value(body, "ab-match")}
    return out


def matching_agents(text, max_agents=2):
    """Names of loaded agents whose ``ab-match`` regex matches ``text`` (an
    issue or PR title + body), best first (most matches), at most max_agents."""
    scored = []
    for name, s in _CACHE["skills"].items():
        pat = s.get("match") if s.get("kind") == "agent" else ""
        if not pat:
            continue
        try:
            hits = len(re.findall(pat, text or "", re.I))
        except re.error:
            continue
        if hits:
            scored.append((-hits, name))
    return [name for _, name in sorted(scored)[:max_agents]]


def agents_context(text, max_chars=20000):
    """Full instructions of the agents matching ``text``, for a fix/build/review
    prompt: the same checklist a person gets by picking that agent in Copilot.
    Empty string when nothing matches."""
    parts = []
    for name in matching_agents(text):
        parts.append("## Specialist agent \"%s\" -- follow its checklist\n%s"
                     % (name, _CACHE["skills"][name]["instructions"]))
    return "\n\n".join(parts)[:max_chars]


def get_loaded():
    """The last-loaded skills dict (no fetch)."""
    # Return a copy so callers cannot modify the shared cache.
    return dict(_CACHE["skills"])


def skill_files(name):
    """The raw {"SKILL.md": ..., "reference.md": ...} for one loaded skill
    (only the two keys that were actually present are included). Empty dict
    if the skill isn't loaded — never raises, matching this module's
    defensive-by-design contract."""
    # Look up the skill in the cache; unknown names give an empty result.
    s = _CACHE["skills"].get(name)
    if not s:
        return {}
    # Include only the files that actually had content.
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
    # Get the skill's raw files; nothing to inject if it is not loaded.
    files = skill_files(name)
    if not files:
        return ""
    # SKILL.md first, then reference.md under a clear separator.
    parts = []
    if "SKILL.md" in files:
        parts.append(files["SKILL.md"])
    if "reference.md" in files:
        parts.append(f"--- reference.md ---\n{files['reference.md']}")
    # Join the parts and truncate to keep the prompt within budget.
    return "\n\n".join(parts)[:max_chars]


def skills_context(max_chars=4000):
    """Compact 'project skills — follow these' block for injecting into a fix/build
    prompt. Names + descriptions only (keeps the prompt lean); the fixer can be
    told to honor them. Empty string when no skills are loaded."""
    # Use whatever skills are currently cached; none loaded means no block at all.
    skills = _CACHE["skills"]
    if not skills:
        return ""
    # Heading telling the model these skills must be followed when relevant.
    lines = ["## Project skills — FOLLOW these recipes + boundaries when they apply:"]
    # One bullet per skill (alphabetical), with its description flattened to one line.
    for name, s in sorted(skills.items()):
        desc = (s.get("description") or "").strip().replace("\n", " ")
        lines.append(f"- **{name}**: {desc[:400]}")
    # Join into one block, capped at max_chars to keep the prompt lean.
    return "\n".join(lines)[:max_chars]
