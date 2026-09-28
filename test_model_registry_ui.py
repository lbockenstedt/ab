#!/usr/bin/env python3
"""Self-test for the Model Registry settings tab.

Run:  python3 ab/test_model_registry_ui.py
      (also collected by pytest via the test_* functions below)

Step 3 of the LLM tab used to be a bare JSON textarea. The rule schema is wide
-- 3 identity fields, 4 ranking fields drawn from 3 closed vocabularies, and 6
capability booleans -- so editing it meant knowing both the field names and the
legal values from memory, with a typo silently reclassifying a model. It is now
its own tab rendering one labelled control per field.

What these pin, in order of how badly each would bite:

1. THE POSTED FIELD IS STILL SINGULAR AND STILL IN THE FORM. The editor is
   cosmetic; `model_registry_json` remains the only thing save_settings reads.
   Moving the textarea to another tab must not drop it out of #settings-form
   (hidden sections still post), and a second element with that name would make
   the POST order-dependent.

2. THE DROPDOWN VOCABULARIES MATCH model_registry.py. This is the real drift
   risk: add a cost tier in Python and a stale <select> keeps offering the old
   four, so the operator cannot pick the new one and the UI quietly lies about
   what is legal. The vocabularies are asserted EQUAL, not merely a subset, so
   a removed tier fails just as loudly as an added one.

3. EVERY CAPABILITY FIELD IS REACHABLE. model_registry._CAP_FIELDS is what
   resolve() actually consumes. A field present there but absent from the
   editor is invisible to the operator while still driving routing -- exactly
   the "you have to guess" failure this tab exists to remove.

Nothing here imports routes.py or main.py (app-init side effects); the template
is read as text, which is also what makes assertion 2 meaningful -- it compares
the SHIPPED markup against the SHIPPED constants.
"""
import ast
import os
import re
import sys

import model_registry

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE = os.path.join(_HERE, "templates", "index.html")

with open(_TEMPLATE, encoding="utf-8") as fh:
    HTML = fh.read()

SECTION_ID = "sec-model-registry"


def _section():
    """The markup of the Model Registry <section>, start tag to </section>.

    Sections are not nested in this template, so the first </section> after the
    opening tag is the right one."""
    start = HTML.index('<section id="%s"' % SECTION_ID)
    end = HTML.index("</section>", start)
    return HTML[start:end]


def _js_pairs(const_name):
    """Extract the ['value', 'label'] pairs from a JS const array literal.

    Parsed with ast.literal_eval after swapping JS brackets for Python ones --
    a regex for the values alone would also match the human-readable labels."""
    m = re.search(r"const %s = (\[.*?\]);" % const_name, HTML, re.S)
    assert m, "%s not found in the template" % const_name
    return [pair[0] for pair in ast.literal_eval(m.group(1))]


def _js_string_list(const_name):
    m = re.search(r"const %s = (\[.*?\]);" % const_name, HTML, re.S)
    assert m, "%s not found in the template" % const_name
    return list(ast.literal_eval(m.group(1)))


def _flag_keys():
    m = re.search(r"const AB_REG_FLAGS = (\[.*?\]);", HTML, re.S)
    assert m, "AB_REG_FLAGS not found in the template"
    return [entry[0] for entry in ast.literal_eval(m.group(1))]


# ---------------------------------------------------------------- 1. plumbing

def test_tab_and_section_are_paired():
    """A nav button with no section (or the reverse) renders a dead tab."""
    assert 'data-tab="%s"' % SECTION_ID in HTML, "no nav button for the Model Registry tab"
    assert '<section id="%s" data-settings-section' % SECTION_ID in HTML, (
        "the Model Registry section must carry data-settings-section or settingsTab() "
        "will not hide/show it")


def test_registry_field_is_singular_and_inside_the_settings_form():
    """The posted field must be unique and still submit.

    settingsTab() only toggles visibility -- hidden sections keep their inputs
    in #settings-form -- so moving the textarea to a new tab is safe ONLY while
    it stays inside the form element."""
    occurrences = [m.start() for m in re.finditer(r'name="model_registry_json"', HTML)
                   if not HTML[:m.start()].rstrip().endswith("--")]
    # Filter out the explanatory HTML comment that also names the field.
    real = [pos for pos in occurrences if "<textarea" in HTML[max(0, pos - 200):pos]]
    assert len(real) == 1, (
        "expected exactly one posted model_registry_json control, found %d -- a "
        "duplicate makes the POST order-dependent" % len(real))

    form_start = HTML.index('<form id="settings-form"')
    form_end = HTML.index("</form>", form_start)
    assert form_start < real[0] < form_end, (
        "the registry textarea fell outside #settings-form, so Save would no "
        "longer persist it")


def test_json_textarea_still_renders_the_server_value():
    """The cards are built by parsing this value on load; if the template stops
    interpolating it the editor silently starts from empty and a Save would
    wipe the registry."""
    assert "{{ registry_rules_json }}</textarea>" in HTML


def test_old_json_only_editor_left_no_dangling_references():
    """The replaced visual editor's ids/functions must be gone TOGETHER -- a
    leftover onclick pointing at a deleted function is a console error on a
    control that looks live."""
    for stale in ("toggleRegistryEditor", "registryUpsertRule", "_registryUpsert",
                  "registry-form-provider", "registry-form-cost",
                  "registry-form-complexity", "registry-form-match"):
        assert stale not in HTML, "%s survived the Model Registry rewrite" % stale


# ------------------------------------------------------------ 2. vocabularies

def test_cost_tier_dropdown_matches_model_registry():
    assert sorted(_js_pairs("AB_REG_COST")) == sorted(model_registry.COST_TIER_RANK), (
        "the cost-tier dropdown has drifted from model_registry.COST_TIER_RANK")


def test_max_complexity_dropdown_matches_model_registry():
    assert sorted(_js_pairs("AB_REG_COMPLEXITY")) == sorted(model_registry.COMPLEXITY_RANK), (
        "the max-complexity dropdown has drifted from model_registry.COMPLEXITY_RANK")


def test_speed_tier_dropdown_matches_model_registry():
    assert sorted(_js_pairs("AB_REG_SPEED")) == sorted(model_registry.SPEED_RANK), (
        "the speed-class dropdown has drifted from model_registry.SPEED_RANK")


def test_speed_tier_offers_an_auto_option():
    """speed_tier() falls back to a capability_rank-derived prior when a rule
    carries no explicit value. Without an explicit "auto" choice the editor
    cannot express that state and every rule it touches gets pinned."""
    assert "'auto (derive from rank)'" in HTML


def test_provider_suggestions_cover_every_shipped_provider():
    """A provider present in DEFAULT_MODEL_RULES but missing from the
    suggestions is one the operator has to know by heart."""
    shipped = {r.get("provider") for r in model_registry.DEFAULT_MODEL_RULES if r.get("provider")}
    suggested = set(_js_string_list("AB_REG_PROVIDERS"))
    assert shipped <= suggested, "providers missing from the dropdown: %s" % sorted(shipped - suggested)

    datalist = HTML[HTML.index('<datalist id="ab-reg-providers">'):]
    datalist = datalist[:datalist.index("</datalist>")]
    assert set(re.findall(r'<option value="([^"]+)"', datalist)) == suggested, (
        "the <datalist> and AB_REG_PROVIDERS have drifted apart")


def test_provider_is_free_text_not_a_closed_select():
    """Providers are operator-configured keys, so the control suggests without
    constraining -- a <select> would make a custom provider untypeable."""
    assert "setAttribute('list', 'ab-reg-providers')" in HTML


# -------------------------------------------------------------- 3. coverage

def test_every_capability_field_is_editable():
    """model_registry._CAP_FIELDS is what resolve() consumes. Each must be
    reachable: booleans as checkboxes in AB_REG_FLAGS, scalars as a set(...)
    or explicit delete in the card body."""
    flags = set(_flag_keys())
    for field in model_registry._CAP_FIELDS:
        if field in flags:
            continue
        assert ("set('%s'" % field) in HTML or ("abRegRules[idx].%s" % field) in HTML, (
            "%s is consumed by model_registry.resolve() but cannot be edited in "
            "the Model Registry tab" % field)


def test_identity_fields_are_editable():
    """Without these a rule cannot be created at all, only re-tuned."""
    for field in ("provider", "match", "id", "label", "enabled", "notes"):
        assert ("set('%s'" % field) in HTML, "%s is not editable in the registry tab" % field


def test_capability_rank_is_clamped_to_the_documented_range():
    """capability_rank() clamps to 0-100 server-side; the input must not offer
    values that will be silently rewritten."""
    assert "Math.max(0, Math.min(100, n))" in HTML
    assert "rank.min = 0; rank.max = 100;" in HTML


def test_blank_optional_numerics_delete_rather_than_zero():
    """capability_rank and speed_tier both have meaningful fallbacks. Writing 0
    (or "") instead of removing the key would pin the weakest possible value
    and quietly demote the model."""
    assert "delete abRegRules[idx].capability_rank" in HTML
    assert "delete abRegRules[idx].speed_tier" in HTML


def test_unrecognised_saved_value_is_preserved_in_a_select():
    """Opening the tab must never rewrite a value it does not recognise -- a
    <select> with no matching <option> otherwise reports the FIRST option, so
    merely visiting the page would silently reclassify the rule on the next
    Save."""
    assert "(unrecognised)" in HTML


# ---------------------------------------------------------------- 4. tooltips

def test_every_control_in_the_section_has_a_tooltip():
    """check_tooltips.py raises an advisory finding for any added
    <button>/<input>/<select> with no title=; this section is dense enough that
    an untitled control is a real usability gap, so it is asserted outright."""
    section = _section()
    # A tag named inside an HTML comment is prose, not a control.
    section = re.sub(r"<!--.*?-->", "", section, flags=re.S)
    missing = []
    for m in re.finditer(r"<(button|input|select|textarea)\b([^>]*)>", section):
        attrs = m.group(2)
        if re.search(r'type\s*=\s*["\']hidden["\']', attrs):
            continue
        if "title=" not in attrs:
            missing.append(m.group(0)[:90])
    assert not missing, "controls without a tooltip:\n" + "\n".join(missing)


def test_javascript_built_controls_carry_tooltips():
    """The cards are built in JS, so check_tooltips' markup scan cannot see
    them. Each control factory must therefore take and apply a tip."""
    for factory in ("_abRegInput", "_abRegSelect", "_abRegCheckbox"):
        body = HTML[HTML.index("function %s(" % factory):]
        body = body[:body.index("\n        }")]
        assert re.search(r"\.title = tip", body), (
            "%s builds a control without applying its tooltip" % factory)


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %s" % (fn.__name__, exc))
    print("\n%d/%d passed" % (len(fns) - failed, len(fns)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
