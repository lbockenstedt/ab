#!/usr/bin/env python3
"""Self-test for two settings-path defects in routes.py.

Run:  python3 ab/test_settings_token_and_repo_suggest.py

1. module_repo_map_suggest() used get_monitored_repos without ever binding it
   (the only import lived inside _promotable_repos), so the endpoint raised
   NameError on every request. Asserted with symtable, which reports exactly
   how the compiler resolved the name.

2. The settings form rendered the real GitHub PAT into the HTML
   (value="{{ settings.GITHUB_TOKEN }}"). type="password" masks the field on
   screen but the secret still shipped in the page source. The field is now
   write-only, which means a blank submission must preserve the stored token
   instead of wiping it.

Static/extraction based: no app import, no network, no config writes.
"""
import re
import symtable
import textwrap
import sys

import jinja2

ROUTES = "routes.py"
TEMPLATE = "templates/index.html"


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _find_function(table, name):
    """Depth-first lookup of a nested function symtable by name."""
    for child in table.get_children():
        if child.get_type() == "function" and child.get_name() == name:
            return child
        found = _find_function(child, name)
        if found is not None:
            return found
    return None


def _extract_block(src, start_anchor, end_anchor):
    """Return the source between two unique anchors, asserting uniqueness so
    the test fails loudly if the code moves rather than silently passing."""
    if src.count(start_anchor) != 1 or src.count(end_anchor) != 1:
        return None
    start = src.index(start_anchor)
    end = src.index(end_anchor)
    if end <= start:
        return None
    return textwrap.dedent(src[start:end])


# --------------------------------------------------------------------------- #
# 1. NameError in module_repo_map_suggest
# --------------------------------------------------------------------------- #
def case_repo_suggest_binds_helper():
    print("CASE: module_repo_map_suggest binds get_monitored_repos")
    src = _read(ROUTES)
    top = symtable.symtable(src, ROUTES, "exec")

    fn = _find_function(top, "module_repo_map_suggest")
    ok = _check("module_repo_map_suggest exists", fn is not None)
    if not fn:
        return False

    names = {s.get_name() for s in fn.get_symbols()}
    ok &= _check("references get_monitored_repos", "get_monitored_repos" in names)
    if "get_monitored_repos" not in names:
        return False

    sym = fn.lookup("get_monitored_repos")
    # A local import binds the name in the function; the bug was an unbound
    # global with no module-level counterpart.
    bound_locally = sym.is_assigned() or sym.is_local()
    module_names = {s.get_name() for s in top.get_symbols()}
    bound_at_module = "get_monitored_repos" in module_names

    ok &= _check(
        "name is resolvable (local import or module-level import)",
        bound_locally or bound_at_module,
    )
    ok &= _check(
        "not an unbound global",
        not (sym.is_global() and not bound_at_module and not bound_locally),
    )
    return ok


# --------------------------------------------------------------------------- #
# 2a. Template must not emit the token
# --------------------------------------------------------------------------- #
def case_template_hides_token():
    print("CASE: settings template never renders the PAT")
    src = _read(TEMPLATE)

    ok = _check(
        "no value=\"{{ settings.GITHUB_TOKEN }}\" in template",
        'value="{{ settings.GITHUB_TOKEN }}"' not in src,
    )

    # Render the real field fragment both ways and assert the secret is absent.
    match = re.search(r'<input[^>]*name="GITHUB_TOKEN"[^>]*>', src)
    ok &= _check("token input found", match is not None)
    if not match:
        return False

    # Pull the surrounding block so the sibling hint paragraph renders too.
    block_start = src.rindex("<label", 0, match.start())
    block_end = src.index("</div>", match.end())
    fragment = src[block_start:block_end]

    env = jinja2.Environment(autoescape=True)
    tmpl = env.from_string(fragment)

    # Assembled at runtime so secret scanners (AppBuilder's own Tier-1
    # check included) do not flag this fixture as a real credential.
    secret = "ghp_" + "N" * 26
    stored = tmpl.render(settings={"GITHUB_TOKEN": secret,
                                   "GITHUB_TOKEN_configured": True,
                                   "GITHUB_TOKEN_from_env": False})
    env_only = tmpl.render(settings={"GITHUB_TOKEN": secret,
                                     "GITHUB_TOKEN_configured": True,
                                     "GITHUB_TOKEN_from_env": True})
    without = tmpl.render(settings={"GITHUB_TOKEN": "",
                                    "GITHUB_TOKEN_configured": False,
                                    "GITHUB_TOKEN_from_env": False})

    for label, html in (("stored", stored), ("env-only", env_only), ("absent", without)):
        ok &= _check(f"secret absent from rendered HTML ({label})", secret not in html)
    ok &= _check("renders empty value", 'value=""' in stored)
    ok &= _check("has a title= tooltip", "title=" in stored)
    ok &= _check("signals a token is stored", "stored" in stored)
    ok &= _check("offers an explicit clear when stored", "abGithubTokenClear" in stored)
    ok &= _check(
        "env-only says so and does not claim it is stored",
        "environment variable" in env_only,
    )
    ok &= _check(
        "env-only offers no misleading clear link",
        "abGithubTokenClear" not in env_only,
    )
    ok &= _check(
        "signals when no token is stored",
        "Not configured" in without or "No token" in without,
    )
    return ok


# --------------------------------------------------------------------------- #
# 2b. Blank submission must not wipe the stored token
# --------------------------------------------------------------------------- #
def case_blank_token_preserved():
    print("CASE: blank GITHUB_TOKEN submission keeps the stored token")
    src = _read(ROUTES)

    ok = _check(
        "GITHUB_TOKEN removed from the blind updates map",
        '"GITHUB_TOKEN": lambda v: v,' not in src,
    )

    block = _extract_block(
        src,
        '    if "GITHUB_TOKEN" in data:',
        '    config_data["direct_push_enabled"]',
    )
    ok &= _check("token save block located exactly once", block is not None)
    if block is None:
        return False

    def run(form, stored):
        scope = {"data": form, "config_data": {"GITHUB_TOKEN": stored}}
        exec(compile(block, "<token-save>", "exec"), scope, scope)
        return scope["config_data"].get("GITHUB_TOKEN")

    stored = "ghp_existing_token"
    ok &= _check("absent field keeps token", run({}, stored) == stored)
    ok &= _check("empty string keeps token", run({"GITHUB_TOKEN": ""}, stored) == stored)
    ok &= _check(
        "whitespace-only keeps token", run({"GITHUB_TOKEN": "   "}, stored) == stored
    )
    ok &= _check("None keeps token", run({"GITHUB_TOKEN": None}, stored) == stored)
    ok &= _check(
        "new token replaces stored one",
        run({"GITHUB_TOKEN": "ghp_new_token"}, stored) == "ghp_new_token",
    )
    ok &= _check(
        "new token is stripped",
        run({"GITHUB_TOKEN": "  ghp_new_token  "}, stored) == "ghp_new_token",
    )
    ok &= _check("works with no prior token", run({"GITHUB_TOKEN": "ghp_a"}, "") == "ghp_a")
    # "unchanged" and "revoke" must stay distinct states, same as the sibling
    # llm_proxy_api_key / OIDC client-secret fields.
    ok &= _check(
        "__CLEAR__ sentinel revokes the token",
        run({"GITHUB_TOKEN": "__CLEAR__"}, stored) == "",
    )
    ok &= _check(
        "__CLEAR__ is whitespace tolerant",
        run({"GITHUB_TOKEN": "  __CLEAR__  "}, stored) == "",
    )
    return ok


# --------------------------------------------------------------------------- #
# 2c. Render context must not carry the secret
# --------------------------------------------------------------------------- #
def case_render_context_excludes_token():
    print("CASE: the merged render context carries no PAT")
    src = _read(ROUTES)

    ok = _check(
        "old leaking assignment gone",
        'settings["GITHUB_TOKEN"] = config.get("GITHUB_TOKEN")' not in src,
    )

    # Grepping the source is NOT sufficient here: settings_page blanks
    # settings["GITHUB_TOKEN"], but the template context is built as
    # {**settings, **config, ...} and config is merged AFTERWARDS, so
    # config["GITHUB_TOKEN"] silently reinstates the raw PAT. Extract the real
    # dict expression and evaluate it to prove what the template actually sees.
    anchor = '"settings": {**settings, **config,'
    ok &= _check("context dict located exactly once", src.count(anchor) == 1)
    if src.count(anchor) != 1:
        return False

    start = src.index("{", src.index(anchor) + len('"settings": ') - 1)
    depth, end = 0, None
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    ok &= _check("context dict brace-matched", end is not None)
    if end is None:
        return False

    # Assembled at runtime so secret scanners (AppBuilder's own Tier-1
    # check included) do not flag this fixture as a real credential.
    secret = "ghp_" + "N" * 26
    scope = {
        "settings": {
            "GITHUB_TOKEN": "",
            "monitored_labels_str": "",
            "GITHUB_TOKEN_configured": True,
            "GITHUB_TOKEN_from_env": False,
        },
        # A real config always carries the token; that is the whole point.
        "config": {"GITHUB_TOKEN": secret, "llm_proxy_api_key": "sk-secret"},
        "repo_tests_str": "",
        "_safe_llm_credentials": [],
        "_safe_llm_entries": [],
    }
    ctx = eval(src[start:end], scope, scope)  # noqa: S307 - repo's own source

    ok &= _check("merged context blanks GITHUB_TOKEN", ctx.get("GITHUB_TOKEN") == "")
    ok &= _check(
        "secret appears nowhere in the merged context",
        secret not in repr(ctx),
    )
    ok &= _check(
        "presence flag survives the merge", ctx.get("GITHUB_TOKEN_configured") is True
    )
    ok &= _check(
        "sibling secret still blanked (regression guard)",
        ctx.get("llm_proxy_api_key") == "",
    )
    return ok


def case_token_state_flags():
    print("CASE: config-set / env-only / absent are distinct states")
    src = _read(ROUTES)
    ok = _check("configured flag present", 'settings["GITHUB_TOKEN_configured"]' in src)
    ok &= _check("env-only flag present", 'settings["GITHUB_TOKEN_from_env"]' in src)

    block = _extract_block(
        src,
        '    settings["GITHUB_TOKEN_configured"]',
        "\n    settings[\"LLM_TIMEOUT\"]",
    )
    ok &= _check("flag block located exactly once", block is not None)
    if block is None:
        return False

    def run(cfg_token, env_token):
        scope = {"settings": {}, "config": {"GITHUB_TOKEN": cfg_token},
                 "os": __import__("os")}
        env = scope["os"].environ
        prior = env.get("GITHUB_TOKEN")
        if env_token:
            env["GITHUB_TOKEN"] = env_token
        else:
            env.pop("GITHUB_TOKEN", None)
        try:
            exec(compile(block, "<flags>", "exec"), scope, scope)
        finally:
            if prior is None:
                env.pop("GITHUB_TOKEN", None)
            else:
                env["GITHUB_TOKEN"] = prior
        s = scope["settings"]
        return s["GITHUB_TOKEN_configured"], s["GITHUB_TOKEN_from_env"]

    ok &= _check("stored in config -> configured, not env", run("ghp_cfg", "") == (True, False))
    ok &= _check("env only -> configured AND env", run("", "ghp_env") == (True, True))
    ok &= _check("neither -> not configured", run("", "") == (False, False))
    ok &= _check("config wins over env", run("ghp_cfg", "ghp_env") == (True, False))
    return ok


def main():
    ok = True
    for case in (
        case_repo_suggest_binds_helper,
        case_template_hides_token,
        case_blank_token_preserved,
        case_render_context_excludes_token,
        case_token_state_flags,
    ):
        ok &= case()
        print()

    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
