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
    match = re.search(r'<input type="password" name="GITHUB_TOKEN"[^>]*>', src)
    ok &= _check("token input found", match is not None)
    if not match:
        return False

    # Pull the surrounding block so the sibling hint paragraph renders too.
    block_start = src.rindex("<label", 0, match.start())
    block_end = src.index("</div>", match.end())
    fragment = src[block_start:block_end]

    env = jinja2.Environment(autoescape=True)
    tmpl = env.from_string(fragment)

    secret = "ghp_SUPERSECRETVALUE1234567890"
    with_token = tmpl.render(settings={"GITHUB_TOKEN": secret, "GITHUB_TOKEN_SET": True})
    without = tmpl.render(settings={"GITHUB_TOKEN": "", "GITHUB_TOKEN_SET": False})

    ok &= _check("secret absent from rendered HTML", secret not in with_token)
    ok &= _check("renders empty value", 'value=""' in with_token)
    ok &= _check(
        "signals a token is stored", "Saved" in with_token or "stored" in with_token
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
        "    _submitted_token = str(data.get(",
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
    return ok


# --------------------------------------------------------------------------- #
# 2c. Render context must not carry the secret
# --------------------------------------------------------------------------- #
def case_render_context_excludes_token():
    print("CASE: settings_page puts no PAT in the render context")
    src = _read(ROUTES)
    ok = _check(
        "old leaking assignment gone",
        'settings["GITHUB_TOKEN"] = config.get("GITHUB_TOKEN")' not in src,
    )
    ok &= _check('blanked in context', 'settings["GITHUB_TOKEN"] = ""' in src)
    ok &= _check(
        "boolean flag exposed instead", 'settings["GITHUB_TOKEN_SET"]' in src
    )
    return ok


def main():
    ok = True
    for case in (
        case_repo_suggest_binds_helper,
        case_template_hides_token,
        case_blank_token_preserved,
        case_render_context_excludes_token,
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
