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
                "    if not safe_external_url(url): raise HTTPException(400)")]
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

