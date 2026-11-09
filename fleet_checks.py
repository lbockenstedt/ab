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
               lambda: check_tenant_scope(files, repo_full_name, vault_repos)):
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
    """All rules (plus node --check) on a local worktree's uncommitted changes."""
    try:
        files = local_diff_files(repo_path)
    except Exception:  # noqa: BLE001
        return []
    out = check_fleet_rules(files, repo_full_name, None, config)
    # Syntax-check changed JS from disk.
    items = []
    for f in files:
        if f.filename.endswith(".js") and f.status != "removed":
            try:
                with open(os.path.join(repo_path, f.filename), encoding="utf-8") as fh:
                    items.append((f.filename, fh.read()))
            except OSError:
                pass
    return out + check_js_syntax(items)


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
