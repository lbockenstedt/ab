"""Round-trip the branch-cleanup policy through the real settings form.

`branch_policy` is only as good as the values it is handed, and those values
come from the settings page. This file drives the *real* ``save_settings`` from
``routes.py`` (extracted with ``ast`` — importing routes boots the whole app)
with the *real* form-field names used in ``templates/index.html``, then feeds
the saved config straight back into ``may_delete``.

That end-to-end shape is deliberate: a typo in the template's ``name=`` or a
missing entry in the ``updates`` dict would leave the policy silently reading
defaults, which is exactly the class of half-wired change that let `dev` and
`qa` get deleted in the first place.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from branch_policy import may_delete, may_force_push  # noqa: E402


def _save(**overrides):
    """Submit the settings form and return the config that was persisted."""
    from test_feature_settings_roundtrip import _load_ns, _base_pairs, _FakeRequest
    import asyncio

    cwd = os.getcwd()
    os.chdir(HERE)
    try:
        ns = _load_ns()
    finally:
        os.chdir(cwd)

    pairs = _base_pairs()
    # A checkbox that is off is *absent* from the submission, so honour None as
    # "don't send this field at all" rather than sending an empty value.
    for key, value in overrides.items():
        pairs = [p for p in pairs if p[0] != key]
        if value is not None:
            pairs.append((key, value))

    asyncio.new_event_loop().run_until_complete(
        ns["save_settings"](_FakeRequest(pairs))
    )
    return ns["_config_holder"]["config"]


def test_protected_branches_persist_as_a_list():
    cfg = _save(protected_branches="main, dev, qa, release-2.0")
    assert cfg["protected_branches"] == ["main", "dev", "qa", "release-2.0"]


def test_auto_prefixes_persist_as_a_list():
    cfg = _save(auto_branch_prefixes="bug/, ai-feature/")
    assert cfg["auto_branch_prefixes"] == ["bug/", "ai-feature/"]


def test_checked_box_enables_cleanup():
    assert _save(delete_merged_branches="on")["delete_merged_branches"] is True


def test_unchecked_box_disables_cleanup():
    """An unchecked checkbox is omitted by the browser, not sent as "off"."""
    assert _save(delete_merged_branches=None)["delete_merged_branches"] is False


def test_saved_config_actually_governs_deletion():
    """The whole point: what the form saves is what the policy enforces."""
    cfg = _save(
        delete_merged_branches="on",
        protected_branches="main, dev, qa",
        auto_branch_prefixes="bug/",
    )

    assert may_delete("bug/42-thing", cfg)[0] is True

    for shared in ("dev", "qa", "main"):
        ok, reason = may_delete(shared, cfg)
        assert ok is False, f"{shared} must never be deletable"
        assert reason, "a refusal must explain itself so it can be logged"


def test_disabling_cleanup_protects_even_auto_branches():
    cfg = _save(delete_merged_branches=None, auto_branch_prefixes="bug/")
    assert may_delete("bug/42-thing", cfg)[0] is False


def test_saved_config_governs_force_push():
    """The same config must also stop fix_engine force-pushing a shared branch."""
    cfg = _save(protected_branches="main, dev, qa")
    assert may_force_push("dev", cfg)[0] is False
    assert may_force_push("bug/42-thing", cfg)[0] is True


def test_blank_fields_fall_back_to_safe_defaults():
    """A user who clears the boxes must not thereby unprotect dev/qa."""
    cfg = _save(protected_branches="", auto_branch_prefixes="")
    assert cfg["protected_branches"] == []
    for shared in ("dev", "qa", "main"):
        assert may_delete(shared, cfg)[0] is False
