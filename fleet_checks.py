"""fleet_checks.py — deterministic fleet code rules, applied to a diff.

The rules here were enforced by hand in Copilot sessions; AppBuilder now runs
them on every PR it reviews and on every fix/feature it builds (before push),
so the model gets the same feedback a session would give it.

Same {level, title, detail} finding shape as secrets_scan / check_tooltips.
Levels: "error" and "warning" block auto-merge (and fail AB's own pre-push
gate); "advisory" is a note only.

Input is a list of objects with ``filename``, ``patch`` (unified diff) and
``status`` — PyGithub File objects, or ``local_diff_files()`` for a worktree.
No app imports, so it can also run from CI.
"""
import os
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

# Cap the work one review may do.
_MAX_FILES = 60
_MAX_FINDINGS_PER_RULE = 10

# Paths that are tests or docs: most code rules do not apply there.
_TEST_RE = re.compile(r"(?i)(^|/)(tests?|qa)(/|$)|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$|(^|/)conftest\.py$")
_DOC_RE = re.compile(r"(?i)\.(md|rst|txt)$|(^|/)docs?/")

# --- Vault rule ------------------------------------------------------------
# A field whose NAME ends in one of these is a secret.
_SECRET_WORD = (r"(?:api_?key|api_?secret|api_?token|client_?secret|password|passwd|"
                r"passphrase|secret_?key|shared_?secret|auth_?token|access_?token|"
                r"bearer_?token|private_?key|community|psk|secret|token)")
_SECRET_NAME = r"\w*%s" % _SECRET_WORD
# Python: a model/Form field, a dict key filled from a request, or a stored key.
_PY_SECRET_DECL = [
    re.compile(r"^\s+(%s)\s*:\s*(?:Optional\[)?(?:str|SecretStr)\b" % _SECRET_NAME, re.I),
    re.compile(r"\b(%s)\s*:\s*str\s*=\s*Form\(" % _SECRET_NAME, re.I),
    re.compile(r"""\[\s*["'](%s)["']\s*\]\s*=\s*(?!\s*(?:""|''|None\b|False\b))(?!\s*["']\*+)""" % _SECRET_NAME, re.I),
    re.compile(r"""["'](%s)["']\s*:\s*(?:body|data|payload|req|request|item|form|params)\b""" % _SECRET_NAME, re.I),
]
# WebUI: a password input, or a form field keyed by a secret name.
_JS_SECRET_DECL = [
    re.compile(r"""type\s*[:=]\s*\\?["']password\\?["']""", re.I),
    re.compile(r"""\b(?:key|name|field)\s*[:=]\s*\\?["'](%s)\\?["']""" % _SECRET_NAME, re.I),
]
# Added text that shows the change already goes through the vault.
_PY_VAULT_TOKENS = ("instance_vault", "cred_vault", "vault_credential", "SECRET_FIELDS",
                    "validate_ref", "strip_inline_secrets", "overlay_many", "automation_get",
                    "put_secret", "credential_store", "_vault_on", "_vault_enabled",
                    "_vault_available", "VAULT_REQUIRED")
_JS_VAULT_TOKENS = ("vaultPicker", "vault_credential", "vaultSecret", "vaultRequired",
                    "credVault", "vault-picker", "vaultSelect")

# --- Other rules -----------------------------------------------------------
_STORAGE_HARD = re.compile(r"^\s*(?:import|from)\s+(sqlite3|shelve|dbm|sqlalchemy|peewee|tinydb)\b")
_STORAGE_SOFT = re.compile(r"^\s*(?:import|from)\s+(yaml|ruamel|csv|lxml|xml)\b")
_HARDCODE = re.compile(r"orange-tme\.com|\b172\.21\.\d{1,3}\.\d{1,3}\b", re.I)
_CODE_EXT = (".py", ".js", ".html", ".sh", ".ps1", ".json", ".j2")
_NPM_FILES = re.compile(r"(^|/)(package\.json|package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$")
_REQS = re.compile(r"(^|/)requirements[\w.-]*\.txt$")
_URL_FROM_REQUEST = re.compile(
    r"""(?:body|data|payload|req|cfg|item|form)\s*(?:\.get\(|\[)\s*["'](\w+_url)["']""")
_ROUTE_DEF = re.compile(r"^\s*@\w+\.(get|post|put|delete|patch)\(")
_TENANT_GUARD = re.compile(r"(?i)tenant|authz|_guard\(|require_|is_admin|admin_only|"
                           r"_check_admin|Depends\(|session_user|_session\(|sess\b")
# Routes that are public by design.
_PUBLIC_ROUTE = re.compile(r"""\(\s*["']/(?:auth|login|logout|health|static|favicon|status)\b""")


def _added(patch):
    """Added lines of a unified diff, without the '+' marker."""
    # Skip the '+++ b/file' header; keep '+' lines.
    out = []
    for line in (patch or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return out


def _new_side(patch):
    """The new-file view of each hunk: (is_added, text) for '+' and ' ' lines."""
    # Removed lines and headers are not part of the new file.
    out = []
    for line in (patch or "").splitlines():
        if line.startswith("@@"):
            out.append((False, None))  # hunk break
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((True, line[1:]))
        elif line.startswith(" "):
            out.append((False, line[1:]))
    return out


def _f(level, title, detail):
    """Build one finding."""
    return {"level": level, "title": title, "detail": detail}


def _short(repo_full_name):
    """'owner/lm' -> 'lm'."""
    return (repo_full_name or "").rsplit("/", 1)[-1].lower()


def _live(files):
    """Changed, non-removed files (capped)."""
    return [f for f in list(files)[:_MAX_FILES]
            if getattr(f, "status", "") != "removed" and getattr(f, "filename", "")]


def check_vault_secrets(files, repo_full_name="", vault_repos=("lm",)):
    """A new secret field must go through the Credential Vault (hub repos).

    Fails when the change adds a secret field (password, API key, token, ...)
    on the Python side without any vault handling in the Python it adds, or a
    secret input in the WebUI without a vault picker in the JS it adds. The
    TrueNAS appliance shipped like this twice: once storing the key inline,
    then with the backend fixed but no picker in the form."""
    # Only hub-style repos store connection secrets for the fleet.
    if vault_repos and _short(repo_full_name) not in {r.lower() for r in vault_repos}:
        return []
    # Collect secret declarations and vault evidence, per side.
    py_hits, js_hits, py_vault, js_vault = [], [], False, False
    for f in _live(files):
        name = f.filename
        if _TEST_RE.search(name) or _DOC_RE.search(name):
            continue
        lines = _added(getattr(f, "patch", None))
        text = "\n".join(lines)
        if name.endswith(".py"):
            py_vault = py_vault or any(t in text for t in _PY_VAULT_TOKENS)
            for ln in lines:
                for rx in _PY_SECRET_DECL:
                    m = rx.search(ln)
                    if m:
                        py_hits.append((name, m.group(1)))
                        break
        elif name.endswith((".js", ".html")):
            js_vault = js_vault or any(t in text for t in _JS_VAULT_TOKENS)
            for ln in lines:
                for rx in _JS_SECRET_DECL:
                    m = rx.search(ln)
                    if m:
                        js_hits.append((name, m.group(1) if m.groups() else "password input"))
                        break
    # Report each side that adds secrets with no vault handling.
    out = []
    how = ("When a vault is configured (any provider) every credential must be stored "
           "in the Credential Vault, never inline in global_config; with no vault it is "
           "Fernet-encrypted on disk. See lm/MODULE_REQUIREMENTS.md 'Credentials must "
           "live in the Credential Vault': add the fields to instance_vault.SECRET_FIELDS "
           "(and VAULT_REQUIRED if needed), call validate_ref + strip_inline_secrets on "
           "save, overlay/overlay_many at push time, return only an *_set flag from list "
           "routes, and give the WebUI field vaultPicker/vaultRequired/vaultSecret.")
    if py_hits and not py_vault:
        where = ", ".join(sorted({"`%s` in %s" % (n, p) for p, n in py_hits})[:8])
        out.append(_f("error", "Secret stored without the Credential Vault",
                      "This change adds secret field(s) %s but none of the added Python "
                      "uses the vault (instance_vault / cred_vault). %s" % (where, how)))
    if js_hits and not js_vault:
        where = ", ".join(sorted({"`%s` in %s" % (n, p) for p, n in js_hits})[:8])
        out.append(_f("error", "Secret field in the WebUI has no vault picker",
                      "This change adds secret input(s) %s with no vault picker, so a "
                      "vault-enabled hub cannot select the credential from the vault. %s"
                      % (where, how)))
    return out


def check_storage_format(files):
    """JSON (or variants) only: no SQLite/ORM; YAML/CSV/XML need a reason."""
    out = []
    for f in _live(files):
        if not f.filename.endswith(".py") or _TEST_RE.search(f.filename):
            continue
        for ln in _added(getattr(f, "patch", None)):
            m = _STORAGE_HARD.match(ln)
            if m:
                out.append(_f("error", "Database storage in `%s`" % f.filename,
                              "`%s` imports %s. Fleet storage is plain JSON files only — "
                              "no SQLite or ORM." % (f.filename, m.group(1))))
                continue
            m = _STORAGE_SOFT.match(ln)
            if m:
                out.append(_f("warning", "Non-JSON format `%s` in `%s`" % (m.group(1), f.filename),
                              "Storage must be JSON. If %s is only used to parse or export "
                              "a vendor format (not to store state), say so in the PR "
                              "Intent." % m.group(1)))
    return out[:_MAX_FINDINGS_PER_RULE]


def check_dependencies(files):
    """Fewest dependencies, and no npm anywhere."""
    out = []
    for f in _live(files):
        if _NPM_FILES.search(f.filename):
            out.append(_f("error", "npm file `%s`" % f.filename,
                          "The fleet has no npm build. WebUI code is plain JavaScript "
                          "served as-is."))
        elif _REQS.search(f.filename):
            new = [ln.strip() for ln in _added(getattr(f, "patch", None))
                   if ln.strip() and not ln.strip().startswith("#")]
            if new:
                out.append(_f("warning", "New dependency in `%s`" % f.filename,
                              "Adds %s. Keep dependencies to a minimum and prefer plain "
                              "Python; justify the package in the PR Intent."
                              % ", ".join("`%s`" % n for n in new[:6])))
    return out


def check_hardcoded(files):
    """No lab hostnames or lab IPs in source."""
    out = []
    for f in _live(files):
        name = f.filename
        if (not name.endswith(_CODE_EXT) or _TEST_RE.search(name) or _DOC_RE.search(name)):
            continue
        hits = sorted({m.group(0) for ln in _added(getattr(f, "patch", None))
                       if not ln.lstrip().startswith(("#", "//", "*"))
                       for m in _HARDCODE.finditer(ln)})
        if hits:
            out.append(_f("warning", "Hard-coded lab value in `%s`" % name,
                          "Adds %s. Nothing lab-specific is hard-coded: read it from "
                          "config or discover it." % ", ".join("`%s`" % h for h in hits[:5])))
    return out[:_MAX_FINDINGS_PER_RULE]


def check_version_file(files, head_ref=None):
    """VERSION is owned by the promotion bot."""
    if (head_ref or "").startswith(("promote/", "backmerge/")):
        return []
    bad = [f.filename for f in list(files)[:_MAX_FILES]
           if os.path.basename(getattr(f, "filename", "")) == "VERSION"]
    if not bad:
        return []
    return [_f("error", "VERSION edited by hand",
               "%s changed outside a promote/backmerge branch. The promotion workflow "
               "bumps VERSION; leave it alone." % ", ".join(bad))]


def check_docstrings(files):
    """Every new Python function gets a docstring."""
    out = []
    for f in _live(files):
        if not f.filename.endswith(".py") or _TEST_RE.search(f.filename):
            continue
        rows = _new_side(getattr(f, "patch", None))
        missing = []
        i = 0
        while i < len(rows):
            added, text = rows[i]
            m = re.match(r"\s*(?:async\s+)?def\s+(\w+)\s*\(", text or "") if added else None
            if not m:
                i += 1
                continue
            # Find the end of the signature (balanced parens, ends with ':').
            depth, j = 0, i
            while j < len(rows) and rows[j][1] is not None:
                seg = rows[j][1]
                depth += seg.count("(") - seg.count(")")
                if depth <= 0 and seg.rstrip().endswith(":"):
                    break
                j += 1
            # The first non-blank body line must be a string literal.
            k = j + 1
            while k < len(rows) and rows[k][1] is not None and not rows[k][1].strip():
                k += 1
            if k < len(rows) and rows[k][1] is not None:
                body = rows[k][1].lstrip()
                if not re.match(r"[rRbBuU]?[\"']", body):
                    missing.append(m.group(1))
            i = j + 1
        if missing:
            out.append(_f("warning", "Function(s) without a docstring in `%s`" % f.filename,
                          "%s. Every function needs a docstring and short step comments."
                          % ", ".join("`%s`" % n for n in missing[:8])))
    return out[:_MAX_FINDINGS_PER_RULE]


def check_ssrf(files):
    """Outbound URLs taken from a request must pass safe_external_url."""
    out = []
    for f in _live(files):
        if not f.filename.endswith(".py") or _TEST_RE.search(f.filename):
            continue
        lines = _added(getattr(f, "patch", None))
        keys = sorted({m.group(1) for ln in lines for m in _URL_FROM_REQUEST.finditer(ln)})
        if keys and not any("safe_external_url" in ln for ln in lines):
            out.append(_f("warning", "Outbound URL not validated in `%s`" % f.filename,
                          "Reads %s from the request without `safe_external_url` in the "
                          "added code. Validate outbound URL settings on save (SSRF)."
                          % ", ".join("`%s`" % k for k in keys)))
    return out


def check_tenant_scope(files, repo_full_name="", scoped_repos=("lm",)):
    """New hub routes must be tenant-scoped or admin-guarded."""
    if scoped_repos and _short(repo_full_name) not in {r.lower() for r in scoped_repos}:
        return []
    out = []
    for f in _live(files):
        if not f.filename.endswith(".py") or _TEST_RE.search(f.filename):
            continue
        rows = _new_side(getattr(f, "patch", None))
        for i, (added, text) in enumerate(rows):
            if not added or not _ROUTE_DEF.match(text or "") or _PUBLIC_ROUTE.search(text):
                continue
            # Look at the decorator and the next lines of the handler.
            window = " ".join((r[1] or "") for r in rows[i:i + 14])
            if not _TENANT_GUARD.search(window):
                path = re.search(r"""\(\s*["']([^"']+)""", text)
                out.append(_f("warning", "Route without tenant scope in `%s`" % f.filename,
                              "`%s` has no get_tenant_id / admin guard in its first lines. "
                              "Every page and API route is scoped to the caller's tenant."
                              % (path.group(1) if path else text.strip())))
    return out[:_MAX_FINDINGS_PER_RULE]


def check_js_syntax(items):
    """``node --check`` each (path, text) pair; skipped when node is missing."""
    node = shutil.which("node")
    if not node:
        return []
    out = []
    for path, text in items:
        if not path.endswith(".js") or text is None:
            continue
        # node only checks files on disk; use a temp copy.
        fd, tmp = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            proc = subprocess.run([node, "--check", tmp], capture_output=True,
                                  text=True, timeout=15)
            if proc.returncode != 0:
                err = (proc.stderr or "").replace(tmp, path).strip().splitlines()
                out.append(_f("error", "JavaScript syntax error in `%s`" % path,
                              "node --check: %s" % " | ".join(err[:4])))
        except Exception:  # noqa: BLE001 -- tooling trouble is not a finding
            pass
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return out


def _removed(patch):
    """Removed lines of a unified diff, without the '-' marker."""
    return [ln[1:] for ln in (patch or "").splitlines()
            if ln.startswith("-") and not ln.startswith("---")]


def _added_linenos(patch):
    """New-file line numbers of the added lines in a unified diff."""
    # Walk each hunk from its '+start' and count new-side lines.
    out, n = set(), 0
    for line in (patch or "").splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
        if m:
            n = int(m.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            out.add(n)
            n += 1
        elif line.startswith(" "):
            n += 1
    return out


# lm WebUI ratchets (core/tests/test_webui_ratchets.py): counts may only go down.
_RATCHETS = (
    ("escapeHtml() calls", re.compile(r"\bescapeHtml\("), "build markup with safeHtml"),
    ("inline event handlers", re.compile(r"\son[a-z]+=[\"']"), "wire events with data-action / delegate()"),
    ("window globals", re.compile(r"^\s*window\.\w+\s*=[^=]"), "export from the module or register the view"),
)


def check_webui_ratchet(files, repo_full_name=""):
    """lm WebUI: no net new escapeHtml calls, inline handlers or window globals."""
    if _short(repo_full_name) != "lm":
        return []
    webui = [f for f in files if (f.filename.startswith("WebUI/") and "/assets/" not in f.filename
                                  and (f.filename.endswith(".js") or f.filename == "WebUI/index.html"))]
    out = []
    for label, rx, fix in _RATCHETS:
        net, where = 0, set()
        for f in webui:
            added = sum(len(rx.findall(ln)) for ln in _added(getattr(f, "patch", None)))
            net += added - sum(len(rx.findall(ln)) for ln in _removed(getattr(f, "patch", None)))
            if added:
                where.add(f.filename)
        if net > 0:
            out.append(_f("error", "WebUI ratchet: %d new %s" % (net, label),
                          "%s add %s; the lm ratchet test fails on any net increase. Instead, %s "
                          "(docs/webui.md)." % (", ".join("`%s`" % w for w in sorted(where)), label, fix)))
    return out


_UI_FILE = re.compile(r"(^|/)(WebUI|static|templates)/.*\.(js|html)$")
_UI_ID = re.compile(r"\$\{([^}]*\b(?:spoke_id|agent_id|spokeId|agentId|uuid|guid|registration_id|"
                    r"instance_id)\b[^}]*)\}")


def _in_attribute(prefix):
    """True when the text before a match leaves an attribute value open (x="..., x='...)."""
    for q in ('"', "'"):
        i = prefix.rfind("=" + q)
        if i >= 0 and q not in prefix[i + 2:]:
            return True
    return False


def check_ui_uuids(files):
    """WebUI text shows human names, never UUIDs/ids (attributes and URLs are fine)."""
    out = []
    for f in _live(files):
        if not _UI_FILE.search(f.filename) or _TEST_RE.search(f.filename):
            continue
        hits = []
        for ln in _added(getattr(f, "patch", None)):
            if re.search(r"fetch\(|api\w*\(|encodeURIComponent|console\.|/api/", ln):
                continue
            for m in _UI_ID.finditer(ln):
                # Ids inside attribute values (data-id="${...}", onclick args) are not shown.
                if ("`" in m.group(1) or "<" in m.group(1) or _in_attribute(ln[:m.start()])
                        or re.search(r"(?i)name|label|title", m.group(1))):
                    continue
                hits.append(m.group(1).strip())
        if hits:
            out.append(_f("warning", "UUID/id shown in the UI in `%s`" % f.filename,
                          "Renders %s as text. Show the human name (spoke/agent name lookup); "
                          "UUIDs belong only on diagnostic pages."
                          % ", ".join("`${%s}`" % h for h in sorted(set(hits))[:5])))
    return out[:_MAX_FINDINGS_PER_RULE]


_FIRST_SPOKE = re.compile(r"\bget_spoke_by_type\(")
_FIRST_ROW = re.compile(r"\b(?:sites|tenants|spokes|tenant_list|site_list|site_rows|tenant_rows)\[0\]|"
                        r"next\(iter\((?:sites|tenants|spokes)\b")


def check_first_pick(files):
    """No 'first site / first tenant' fallbacks (tenant leaks); first-spoke picks get a note."""
    out = []
    for f in _live(files):
        if not f.filename.endswith(".py") or _TEST_RE.search(f.filename):
            continue
        if f.filename.endswith(("access.py", "hub_spoke_registry.py")):
            continue
        added = [ln.strip() for ln in _added(getattr(f, "patch", None)) if not ln.lstrip().startswith("#")]
        rows = [ln for ln in added if _FIRST_ROW.search(ln)]
        if rows:
            out.append(_f("warning", "First-match fallback in `%s`" % f.filename,
                          "`%s` takes the first site/tenant/spoke. A tenant must never land in "
                          "another tenant's site: match by the record's own tenant, or fail "
                          "instead of guessing." % rows[0][:120]))
        spokes = [ln for ln in added if _FIRST_SPOKE.search(ln)]
        if spokes:
            out.append(_f("advisory", "First connected spoke picked in `%s`" % f.filename,
                          "`%s` returns the first spoke of the type. Tenant-bound spokes own their "
                          "data: for tenant-facing work use `access.scoped_spoke_of_type(hub, tenant, "
                          "type)`, and loop over every spoke for fleet-wide work." % spokes[0][:120]))
    return out[:_MAX_FINDINGS_PER_RULE]


_LOG_CALL = re.compile(r"\b(?:logger|logging|log|_log)\.(?:debug|info|warning|warn|error|exception|critical)\(|\bprint\(")
_FSTR = re.compile(r"""\b[fF][rR]?("([^"\\]|\\.)*"|'([^'\\]|\\.)*')""")
_STR_LIT = re.compile(r"""(?:(?<!\w)[rbuRBU]{1,2})?("([^"\\]|\\.)*"|'([^'\\]|\\.)*')""")
# Secret-valued identifiers; counts/flags/references named after a secret are fine.
_SECRET_ARG = re.compile(r"(?i)\b\w*(?:api_?key|api_?secret|client_?secret|password|passwd|passphrase|"
                         r"secret_?key|shared_?secret|auth_?token|access_?token|bearer_?token|"
                         r"private_?key|psk|secret|token)\b")
_SECRET_SAFE = re.compile(r"(?i)(_(set|ref|id|name|names|field|fields|len|count|present|configured|"
                          r"masked|hint|url|path|file|key_id|bucket|type|kind|source|expires?|age|ttl)$|"
                          r"^(has|is|mask|max|num|n|len)_|tokens$|_tokens_)")


def check_secret_logging(files):
    """Log calls must not print secret values."""
    out = []
    for f in _live(files):
        if not f.filename.endswith(".py") or _TEST_RE.search(f.filename):
            continue
        hits = []
        for ln in _added(getattr(f, "patch", None)):
            call = _LOG_CALL.search(ln)
            if not call or "mask" in ln.lower() or "redact" in ln.lower():
                continue
            # Keep f-string interpolations, drop other string literals, then look at arguments.
            code = _FSTR.sub(lambda m: " " + " ".join(re.findall(r"\{([^{}]*)\}", m.group(0))) + " ",
                             ln[call.end():])
            code = _STR_LIT.sub("''", code)
            for m in _SECRET_ARG.finditer(code):
                if not _SECRET_SAFE.search(m.group(0)):
                    hits.append(m.group(0))
        if hits:
            out.append(_f("error", "Secret written to a log in `%s`" % f.filename,
                          "Logs %s. Never log secrets; log whether it is set, or mask it "
                          "(including secrets nested in command payloads)."
                          % ", ".join("`%s`" % h for h in sorted(set(hits))[:5])))
    return out[:_MAX_FINDINGS_PER_RULE]


_BLOCKING = {("time", "sleep"), ("subprocess", "run"), ("subprocess", "call"),
             ("subprocess", "check_output"), ("subprocess", "check_call"),
             ("requests", "get"), ("requests", "post"), ("requests", "put"),
             ("requests", "delete"), ("requests", "patch"), ("requests", "request"),
             ("json", "dump"), ("shutil", "copytree"), ("shutil", "rmtree")}
_BLOCKING_NAMES = {"urlopen"}


def check_async_blocking(items):
    """No blocking calls added directly inside ``async def`` (items: path, text, added line numbers)."""
    import ast
    out = []
    for path, text, added in items:
        if not path.endswith(".py") or text is None or _TEST_RE.search(path) or not added:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        hits = []

        def walk(node, fn_name):
            # Calls in this async body; nested defs/lambdas run elsewhere (to_thread etc.).
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                    continue
                if isinstance(child, ast.Call) and child.lineno in added:
                    fn = child.func
                    name = (fn.value.id, fn.attr) if (isinstance(fn, ast.Attribute)
                                                      and isinstance(fn.value, ast.Name)) else None
                    if name in _BLOCKING or (isinstance(fn, ast.Name) and fn.id in _BLOCKING_NAMES) \
                            or (isinstance(fn, ast.Attribute) and fn.attr == "urlopen"):
                        hits.append("%s() in %s (line %d)" % (".".join(name) if name else
                                                              getattr(fn, "attr", getattr(fn, "id", "?")),
                                                              fn_name, child.lineno))
                walk(child, fn_name)

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                walk(node, node.name)
        if hits:
            out.append(_f("warning", "Blocking call on the event loop in `%s`" % path,
                          "%s. Run it with `asyncio.to_thread` (or an async client) so one slow "
                          "call does not stall every spoke and page." % "; ".join(hits[:5])))
    return out[:_MAX_FINDINGS_PER_RULE]


def check_full_files(files, fetch):
    """Rules that need whole files at the new revision: node --check and
    blocking calls in async code. ``fetch(path)`` returns the text or None."""
    items = []
    for f in _live(files)[:25]:
        if f.filename.endswith((".js", ".py")):
            items.append((f.filename, fetch(f.filename), _added_linenos(getattr(f, "patch", None))))
    out = check_js_syntax([(p, t) for p, t, _ in items])
    return out + check_async_blocking(items)


def _repo_defs(repo_path):
    """{normalised body dump: [(path, name)]} for every function in the repo (>= 4 statements)."""
    import ast
    names = subprocess.run(["git", "-C", repo_path, "ls-files", "*.py"], capture_output=True,
                           text=True, timeout=30).stdout.splitlines()
    index = {}
    for rel in names[:4000]:
        if _TEST_RE.search(rel):
            continue
        try:
            with open(os.path.join(repo_path, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                                         and isinstance(getattr(node.body[0], "value", None), ast.Constant)
                                         and isinstance(node.body[0].value.value, str)) else node.body
                if len(body) >= 4:
                    key = ast.dump(ast.Module(body=body, type_ignores=[]))
                    index.setdefault(key, []).append((rel, node.name))
    return index


def check_copies_and_dead_code(repo_path, files):
    """Worktree only: new functions that copy an existing one, or that nothing calls."""
    out = []
    new_defs = []
    for f in _live(files):
        if _TEST_RE.search(f.filename) or not f.filename.endswith((".py", ".js")):
            continue
        rows = _new_side(getattr(f, "patch", None))
        for i, (added, text) in enumerate(rows):
            m = added and (re.match(r"\s*(?:async\s+)?def\s+(\w+)\s*\(", text or "")
                           or re.match(r"\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(", text or ""))
            if not m or m.group(1).startswith(("_test", "test_", "__")):
                continue
            prev = rows[i - 1][1] if i else ""
            if (prev or "").lstrip().startswith("@"):
                continue  # decorated: registered as a route/handler
            new_defs.append((f.filename, m.group(1)))
    # Dead code: the name appears nowhere else in the repo.
    dead = []
    for path, name in new_defs[:40]:
        hits = subprocess.run(["git", "-C", repo_path, "grep", "-c", "-w", "--", name],
                              capture_output=True, text=True, timeout=30).stdout
        total = sum(int(r.rsplit(":", 1)[-1]) for r in hits.splitlines() if ":" in r)
        if total <= 1:
            dead.append("`%s` (%s)" % (name, path))
    if dead:
        out.append(_f("warning", "New function(s) nothing calls",
                      "%s. Remove dead code, or wire it in." % ", ".join(dead[:8])))
    # Copies: a new Python function whose body equals an existing one.
    py_new = {(p, n) for p, n in new_defs if p.endswith(".py")}
    if py_new:
        copies = []
        for key, where in _repo_defs(repo_path).items():
            mine = [w for w in where if w in py_new]
            other = [w for w in where if w not in py_new]
            if mine and other:
                copies.append("`%s` (%s) copies `%s` (%s)" % (mine[0][1], mine[0][0], other[0][1], other[0][0]))
        if copies:
            out.append(_f("warning", "Copied function(s) instead of a shared helper",
                          "%s. Call or move the existing helper instead of copying it." % "; ".join(copies[:5])))
    return out


_JS_FUNC = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(")
_JS_COMMENT = re.compile(r"^\s*(?://|/\*|\*)")


def check_js_comments(files):
    """Every new WebUI/JS function has a comment (// or JSDoc) just above it."""
    out = []
    for f in _live(files):
        if not f.filename.endswith(".js") or _TEST_RE.search(f.filename) or "/assets/" in f.filename:
            continue
        rows = _new_side(getattr(f, "patch", None))
        missing = []
        for i, (added, text) in enumerate(rows):
            m = added and _JS_FUNC.match(text or "")
            if not m:
                continue
            # Walk up past blank lines to the line above the function.
            k = i - 1
            while k >= 0 and rows[k][1] is not None and not rows[k][1].strip():
                k -= 1
            above = rows[k][1] if k >= 0 else None
            # A hunk break above means the line is not visible: give it the benefit of the doubt.
            if above is not None and not _JS_COMMENT.match(above):
                missing.append(m.group(1))
        if missing:
            out.append(_f("warning", "Function(s) without a comment in `%s`" % f.filename,
                          "%s. Put a one-line `//` comment (or JSDoc) above each function saying "
                          "what it does." % ", ".join("`%s`" % n for n in missing[:8])))
    return out[:_MAX_FINDINGS_PER_RULE]


_STEP_MIN_LINES = 15
_TRIPLE = re.compile(r"\"\"\"|'''")


def check_step_comments(files):
    """New Python functions longer than ~15 code lines have at least one step comment."""
    out = []
    for f in _live(files):
        if not f.filename.endswith(".py") or _TEST_RE.search(f.filename):
            continue
        rows = _new_side(getattr(f, "patch", None))
        bare = []
        for i, (added, text) in enumerate(rows):
            m = added and re.match(r"(\s*)(?:async\s+)?def\s+(\w+)\s*\(", text or "")
            if not m:
                continue
            indent, code, commented, in_doc = len(m.group(1)), 0, False, False
            # Skip the rest of a multi-line signature (up to the line ending with ':').
            start, depth = i, 0
            while start < len(rows) and rows[start][1] is not None:
                depth += rows[start][1].count("(") - rows[start][1].count(")")
                if depth <= 0 and rows[start][1].rstrip().endswith(":"):
                    break
                start += 1
            # Body: the added lines after the def that are indented deeper than it.
            for added2, body in rows[start + 1:]:
                if body is None or not added2:
                    break
                if not body.strip():
                    continue
                if len(body) - len(body.lstrip()) <= indent:
                    break
                # Docstrings (single- or multi-line) are not code.
                quotes = len(_TRIPLE.findall(body))
                if in_doc or quotes:
                    if quotes % 2:
                        in_doc = not in_doc
                    continue
                if body.strip().startswith("#") or " #" in body:
                    commented = True
                    break
                code += 1
            if not commented and code > _STEP_MIN_LINES:
                bare.append("%s (%d lines)" % (m.group(2), code))
        if bare:
            out.append(_f("warning", "Long function(s) without step comments in `%s`" % f.filename,
                          "%s. Add a short comment before each step so the next reader can follow it."
                          % ", ".join("`%s`" % b for b in bare[:6])))
    return out[:_MAX_FINDINGS_PER_RULE]


_DOC_FILE = re.compile(r"\.md$")
_FORM_FIELD = re.compile(r"""<(?:input|select|textarea)\b[^>]*\bname=["'](\w+)["']""")


def check_docs_updated(files):
    """User-visible additions (new routes, new WebUI pages, new form fields) come with a doc update."""
    files = list(files or [])
    if any(_DOC_FILE.search(f.filename) for f in files if getattr(f, "status", "") != "removed"):
        return []
    why = []
    for f in _live(files):
        if _TEST_RE.search(f.filename):
            continue
        added = _added(getattr(f, "patch", None))
        # New API routes.
        if f.filename.endswith(".py"):
            routes = [re.search(r"\(\s*[\"']([^\"']+)", ln) for ln in added if _ROUTE_DEF.match(ln)]
            why += ["route `%s`" % r.group(1) for r in routes if r]
        # New WebUI pages and form fields.
        if f.filename.endswith((".js", ".html")):
            if getattr(f, "status", "") == "added" and _UI_FILE.search(f.filename):
                why.append("new page `%s`" % f.filename)
            why += ["field `%s`" % m.group(1) for ln in added for m in _FORM_FIELD.finditer(ln)]
    if not why:
        return []
    return [_f("warning", "User-visible change without a documentation update",
               "Adds %s but no docs/*.md, README or MODULE_REQUIREMENTS.md changed. Document what it "
               "does and how to use it." % ", ".join(sorted(set(why))[:6]))]


def check_fleet_rules(files, repo_full_name="", head_ref=None, config=None):
    """Run every diff rule; returns findings (never raises)."""
    cfg = config or {}
    files = list(files or [])
    vault_repos = cfg.get("fleet_vault_repos") or ("lm",)
    out = []
    for fn in (lambda: check_vault_secrets(files, repo_full_name, vault_repos),
               lambda: check_storage_format(files),
               lambda: check_dependencies(files),
               lambda: check_hardcoded(files),
               lambda: check_version_file(files, head_ref),
               lambda: check_docstrings(files),
               lambda: check_ssrf(files),
               lambda: check_tenant_scope(files, repo_full_name, vault_repos),
               lambda: check_webui_ratchet(files, repo_full_name),
               lambda: check_ui_uuids(files),
               lambda: check_first_pick(files),
               lambda: check_secret_logging(files),
               lambda: check_js_comments(files),
               lambda: check_step_comments(files),
               lambda: check_docs_updated(files)):
        try:
            out += fn()
        except Exception:  # noqa: BLE001 -- one bad rule must not hide the others
            pass
    return out


def local_diff_files(repo_path):
    """File-like objects for a worktree's uncommitted changes (incl. new files)."""
    # Make new files visible to `git diff` without staging their content.
    subprocess.run(["git", "-C", repo_path, "add", "-A", "-N"], capture_output=True, timeout=30)
    names = subprocess.run(["git", "-C", repo_path, "diff", "HEAD", "--name-status"],
                           capture_output=True, text=True, timeout=30).stdout
    out = []
    for row in names.splitlines():
        parts = row.split("\t")
        if len(parts) < 2:
            continue
        status, name = parts[0], parts[-1]
        patch = subprocess.run(["git", "-C", repo_path, "diff", "HEAD", "--", name],
                               capture_output=True, text=True, timeout=30).stdout
        out.append(SimpleNamespace(filename=name, patch=patch,
                                   status="removed" if status.startswith("D") else "modified"))
    return out


def check_worktree(repo_path, repo_full_name="", config=None):
    """All rules on a local worktree's uncommitted changes, including the
    whole-file rules and the copied/dead-code scan."""
    try:
        files = local_diff_files(repo_path)
    except Exception:  # noqa: BLE001
        return []
    out = check_fleet_rules(files, repo_full_name, None, config)

    def _read(rel):
        # The changed file as it is on disk now.
        try:
            with open(os.path.join(repo_path, rel), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None
    try:
        out += check_full_files(files, _read)
        out += check_copies_and_dead_code(repo_path, files)
    except Exception:  # noqa: BLE001
        pass
    return out


def gate(repo_path, repo_full_name, config):
    """Correction feedback for a worktree's blocking rule breaches.

    Returns "" when clean, when fleet_checks_enabled is off, or when the
    tooling itself fails (tooling trouble never blocks a change)."""
    # Off switch, then run the rules on the uncommitted change.
    if not (config or {}).get("fleet_checks_enabled", True):
        return ""
    try:
        return as_feedback(blocking(check_worktree(repo_path, repo_full_name, config)))
    except Exception:  # noqa: BLE001
        return ""


def rules_text():
    """The fleet code rules (fleet_rules.md) for prompts; "" if missing."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "fleet_rules.md"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def blocking(findings):
    """Findings that must be fixed before AB pushes (errors and warnings)."""
    return [f for f in findings or [] if f.get("level") in ("error", "warning")]


def as_feedback(findings):
    """Render findings as correction feedback for the model."""
    return "\n".join("- [%s] %s: %s" % (f["level"].upper(), f["title"], f["detail"])
                     for f in findings)
