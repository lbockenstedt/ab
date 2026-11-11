"""fleet_checks — the session code rules, applied deterministically to a diff."""
from types import SimpleNamespace as F

import fleet_checks as fc


def _p(*added, ctx=()):
    """A patch whose new side is ctx lines then added lines."""
    body = ["@@ -1,1 +1,%d @@" % (len(ctx) + len(added))]
    body += [" " + c for c in ctx] + ["+" + a for a in added]
    return "\n".join(body)


def _file(name, *added, status="modified"):
    return F(filename=name, patch=_p(*added), status=status)


# --- vault -----------------------------------------------------------------
def test_new_inline_api_key_without_vault_is_an_error():
    """The TrueNAS case: an appliance API key saved straight into global_config."""
    files = [_file("core/src/routes/truenas.py",
                   "class Appliance(BaseModel):",
                   "    api_key: str = ''",
                   "    rec['api_key'] = body.get('api_key')")]
    out = fc.check_vault_secrets(files, "o/lm")
    assert [f["title"] for f in out] == ["Secret stored without the Credential Vault"]
    assert "api_key" in out[0]["detail"] and "MODULE_REQUIREMENTS" in out[0]["detail"]


def test_vault_handled_backend_but_no_ui_picker_is_an_error():
    """The second TrueNAS case: backend fixed, the form had no vault picker."""
    files = [_file("core/src/routes/truenas.py",
                   "    api_key: str = ''",
                   "    await instance_vault.validate_ref(hub, rec, sess, is_admin=a, storage_key=k)"),
             _file("WebUI/js/module-management.js",
                   "  { key: 'api_key', label: 'API key', type: 'password' },")]
    out = fc.check_vault_secrets(files, "o/lm")
    assert [f["title"] for f in out] == ["Secret field in the WebUI has no vault picker"]


def test_vault_aware_change_passes():
    files = [_file("core/src/instance_vault.py", '    "truenas": {"api_key": ("api_key",)},',
                   "SECRET_FIELDS['x'] = {}"),
             _file("core/src/routes/truenas.py", "    api_key: str = ''"),
             _file("WebUI/js/module-management.js",
                   "  { key: 'api_key', type: 'password', vaultPicker: true, vaultSecret: true },")]
    assert fc.check_vault_secrets(files, "o/lm") == []


def test_non_secret_names_and_masking_do_not_trip():
    files = [_file("core/src/routes/central.py",
                   "    token_url: str = ''",
                   "    api_key_set: bool = False",
                   "    rec['password'] = ''",
                   "    out['api_key'] = '***'")]
    assert fc.check_vault_secrets(files, "o/lm") == []


def test_vault_rule_only_applies_to_hub_repos_and_not_tests():
    files = [_file("src/handlers.py", "    password: str")]
    assert fc.check_vault_secrets(files, "o/cs") == []
    assert fc.check_vault_secrets([_file("core/tests/test_x.py", "    password: str")], "o/lm") == []


# --- other rules -----------------------------------------------------------
def test_storage_dependencies_npm_hardcode_version():
    files = [_file("core/src/a.py", "import sqlite3", "import csv", "HOST = 'hub.orange-tme.com'"),
             _file("requirements.txt", "# comment", "leftpad==1.0"),
             _file("WebUI/package.json", "{}", status="added"),
             _file("core/VERSION", "1.2.3")]
    titles = [f["title"] for f in fc.check_fleet_rules(files, "o/lm")]
    assert "Database storage in `core/src/a.py`" in titles
    assert "Non-JSON format `csv` in `core/src/a.py`" in titles
    assert "New dependency in `requirements.txt`" in titles
    assert "npm file `WebUI/package.json`" in titles
    assert "Hard-coded lab value in `core/src/a.py`" in titles
    assert "VERSION edited by hand" in titles
    # Promotion branches own VERSION.
    assert fc.check_version_file(files, "promote/dev-to-qa") == []


def test_docstrings_on_new_functions():
    patch = "\n".join(["@@ -1 +1,9 @@",
                       "+def documented(a,",
                       "+               b):",
                       '+    """Says what it does."""',
                       "+    return a",
                       "+",
                       "+async def bare(x):",
                       "+    return x",
                       " def untouched():",
                       "     return 1"])
    out = fc.check_docstrings([F(filename="core/src/m.py", patch=patch, status="modified")])
    assert len(out) == 1 and "`bare`" in out[0]["detail"] and "documented" not in out[0]["detail"]


def test_ssrf_and_tenant_scope():
    files = [_file("core/src/routes/x.py",
                   "@router.post('/api/x/save')",
                   "async def save(request: Request):",
                   "    url = body.get('webhook_url')")]
    titles = [f["title"] for f in fc.check_fleet_rules(files, "o/lm")]
    assert "Outbound URL not validated in `core/src/routes/x.py`" in titles
    assert "Route without tenant scope in `core/src/routes/x.py`" in titles
    ok = [_file("core/src/routes/x.py",
                "@router.post('/api/x/save')",
                "async def save(request: Request, tenant=Depends(get_tenant_id)):",
                '    """Save."""',
                "    url = body.get('webhook_url')",
                "    if not safe_external_url(url): raise HTTPException(400)"),
          _file("docs/x.md", "## Save")]
    assert fc.check_fleet_rules(ok, "o/lm") == []


def test_js_syntax(monkeypatch):
    import shutil
    if not shutil.which("node"):
        return
    out = fc.check_js_syntax([("WebUI/a.js", "function ok() { return 1; }"),
                              ("WebUI/b.js", "function bad( { ")])
    assert [f["title"] for f in out] == ["JavaScript syntax error in `WebUI/b.js`"]


def test_worktree_gate(tmp_path):
    """AB's pre-push gate sees new and modified files in a local checkout."""
    import subprocess
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                                    capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tmp_path / "a.py").write_text('def f():\n    """F."""\n    return 1\n')
    run("add", "-A")
    run("commit", "-qm", "init")
    (tmp_path / "b.py").write_text("import sqlite3\n")
    out = fc.check_worktree(str(tmp_path), "o/lm")
    assert any(f["title"] == "Database storage in `b.py`" for f in out)
    assert fc.blocking(out) and "Database storage" in fc.as_feedback(out)



# --- rework rules (mined from a month of fix-up commits) --------------------
def _diff(name, added=(), removed=()):
    body = ["@@ -1,%d +1,%d @@" % (len(removed), len(added))]
    body += ["-" + r for r in removed] + ["+" + a for a in added]
    return F(filename=name, patch="\n".join(body), status="modified")


def test_webui_ratchet_counts_net_change_across_files():
    """Moving an escapeHtml call is fine; adding one, an inline handler or a window global is not."""
    moved = [_diff("WebUI/js/a.js", removed=["x = escapeHtml(a)"]),
             _diff("WebUI/js/b.js", added=["y = escapeHtml(a)"])]
    assert fc.check_webui_ratchet(moved, "o/lm") == []
    added = [_diff("WebUI/js/a.js", added=["h = `<b onclick=\"go()\">${escapeHtml(n)}</b>`",
                                            "window.goThere = go"])]
    titles = sorted(f["title"] for f in fc.check_webui_ratchet(added, "o/lm"))
    assert titles == ["WebUI ratchet: 1 new escapeHtml() calls", "WebUI ratchet: 1 new inline event handlers",
                      "WebUI ratchet: 1 new window globals"]
    assert fc.check_webui_ratchet(added, "o/netbox") == []


def test_ui_uuid_flags_text_but_not_attributes_or_names():
    shown = [_file("WebUI/js/a.js", "row += `<td>${esc(s.spoke_id)}</td>`")]
    assert [f["level"] for f in fc.check_ui_uuids(shown)] == ["warning"]
    fine = [_file("WebUI/js/a.js", "row += `<tr data-id=\"${s.spoke_id}\"><td>${esc(s.name)}</td>`",
                  "fetch(`/api/spokes/${s.spoke_id}`)",
                  "row += `<td>${esc(spokeName(s.spoke_id))}</td>`")]
    assert fc.check_ui_uuids(fine) == []


def test_first_pick_warns_on_first_site_and_notes_first_spoke():
    out = fc.check_first_pick([_file("core/src/routes/x.py", "site = sites[0]",
                                     "sid = hub.get_spoke_by_type('ipam')")])
    assert sorted(f["level"] for f in out) == ["advisory", "warning"]
    assert fc.check_first_pick([_file("core/src/access.py", "site = sites[0]")]) == []


def test_secret_logging_is_an_error_but_set_flags_are_fine():
    bad = [_file("src/x.py", "logger.info(f'login with {password}')", "print('key', api_key)")]
    out = fc.check_secret_logging(bad)
    assert out[0]["level"] == "error" and "password" in out[0]["detail"] and "api_key" in out[0]["detail"]
    ok = [_file("src/x.py", "logger.info('password set: %s', password_set)",
                "logger.info('token %s', mask(token))", "logger.info('password is required')")]
    assert fc.check_secret_logging(ok) == []


def test_async_blocking_only_flags_added_calls_in_async_bodies():
    text = ("import time, asyncio\n"
            "async def poll():\n"
            "    time.sleep(1)\n"
            "    await asyncio.to_thread(lambda: time.sleep(1))\n"
            "def sync():\n"
            "    time.sleep(1)\n")
    out = fc.check_async_blocking([("src/x.py", text, {3, 4, 6})])
    assert len(out) == 1 and "time.sleep() in poll (line 3)" in out[0]["detail"]
    assert fc.check_async_blocking([("src/x.py", text, {6})]) == []


def test_copies_and_dead_code_in_a_worktree(tmp_path):
    import subprocess
    body = "    a = 1\n    b = 2\n    c = a + b\n    return c\n"
    (tmp_path / "old.py").write_text("def helper():\n" + body + "\nhelper()\n")
    (tmp_path / "new.py").write_text("def copied():\n" + body + "\ncopied()\n\ndef unused():\n    return 1\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    files = [_file("new.py", "def copied():", "def unused():")]
    titles = {f["title"]: f["detail"] for f in fc.check_copies_and_dead_code(str(tmp_path), files)}
    assert "`unused` (new.py)" in titles["New function(s) nothing calls"]
    assert "copied" not in titles["New function(s) nothing calls"]
    assert "copies `helper` (old.py)" in titles["Copied function(s) instead of a shared helper"]


# --- comments and docs ------------------------------------------------------
def test_js_function_needs_a_comment_above():
    rows = F(filename="WebUI/js/a.js", status="modified", patch="\n".join([
        "@@ -1,1 +1,6 @@", " const x = 1;", "+function bare() {}", "+", "+// Opens the rack view.",
        "+function documented() {}", "+/** JSDoc */", "+async function alsoFine() {}"]))
    out = fc.check_js_comments([rows])
    assert len(out) == 1 and "`bare`" in out[0]["detail"] and "documented" not in out[0]["detail"]


def test_long_python_function_needs_step_comments():
    sig = ["def long_one(a,", "             b):", '    """Doc."""']
    body = ["    x%d = %d" % (n, n) for n in range(16)]
    assert len(fc.check_step_comments([_file("src/x.py", *sig, *body)])) == 1
    commented = sig + ["    # Build the values."] + body
    assert fc.check_step_comments([_file("src/x.py", *commented)]) == []
    assert fc.check_step_comments([_file("src/x.py", *sig, *body[:15])]) == []


def test_new_route_or_field_needs_a_doc_update():
    code = [_file("core/src/routes/x.py", '@app.get("/api/x")'),
            _file("WebUI/js/s.js", '<input type="number" name="window_h">')]
    out = fc.check_docs_updated(code)
    assert len(out) == 1 and "route `/api/x`" in out[0]["detail"] and "field `window_h`" in out[0]["detail"]
    assert fc.check_docs_updated(code + [_file("docs/x.md", "## X")]) == []
    assert fc.check_docs_updated([_file("core/src/x.py", "y = 1")]) == []


def test_cli_diffs_since_base_and_fails_only_on_errors(tmp_path, capsys):
    """CI entry point: rules run on base...HEAD; errors exit 1, a bad base skips."""
    import subprocess
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                                    capture_output=True, text=True).stdout.strip()
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tmp_path / "a.py").write_text('def f():\n    """F."""\n    return 1\n')
    run("add", "-A")
    run("commit", "-qm", "init")
    base = run("rev-parse", "HEAD")
    # A committed change (not a worktree edit) that logs a secret: an error.
    (tmp_path / "b.py").write_text('def g(password):\n    """G."""\n    print("pw", password)\n')
    run("add", "-A")
    run("commit", "-qm", "leak")
    assert fc.main(["--base", base, "--path", str(tmp_path), "--repo", "o/x"]) == 1
    out = capsys.readouterr().out
    assert "::error title=" in out and "1 error(s)" in out
    # New branch push (all-zero before) and an unknown sha are skipped, not failed.
    assert fc.main(["--base", "0" * 40, "--path", str(tmp_path)]) == 0
    assert fc.main(["--base", "f" * 40, "--path", str(tmp_path)]) == 0
    assert "skipped" in capsys.readouterr().out


def test_cli_allows_version_brought_forward_from_main(tmp_path):
    """A dev push that fast-forwards over promoted commits carries main's
    VERSION bump; only a VERSION that matches neither qa nor main is an error."""
    import subprocess
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                                    capture_output=True, text=True).stdout.strip()
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    base = run("rev-parse", "HEAD")
    (tmp_path / "VERSION").write_text("1.0.1\n")
    run("commit", "-qam", "promote: qa -> main")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    assert fc.main(["--base", base, "--path", str(tmp_path), "--repo", "o/x", "--head-ref", ""]) == 0
    (tmp_path / "VERSION").write_text("9.9.9\n")
    run("commit", "-qam", "hand edit")
    assert fc.main(["--base", base, "--path", str(tmp_path), "--repo", "o/x", "--head-ref", ""]) == 1
