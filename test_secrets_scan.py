"""Self-test for ab/secrets_scan.py.

Run:  pytest -q ab/test_secrets_scan.py

Standalone: imports only secrets_scan (stdlib-only, no app init, no network).

Motivating case: the LM hub's at-rest encryption key (``LM_FERNET_KEY``) lives
in a ``.env`` line. Auditing whether it had ever reached a public diff exposed
the fact that this scanner would NOT have caught it if it had — and that the
same blind spot swallowed ``LM_HUB_SECRET`` and ``DB_PASSWORD`` too. Two
independent causes, both pinned below:

  1. the original generic rule required the value to be QUOTED, which a .env
     line never is; and
  2. it anchored each credential word with ``\\b``, which cannot match after an
     underscore — so any vendor/product prefix hid the name entirely.

The false-positive tests matter as much as the detection tests: this check is
always-on for every PR AppBuilder reviews, so a noisy rule would train
reviewers to ignore it.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from secrets_scan import _scan_line, check_secrets  # noqa: E402

# A real, now-rotated-shape Fernet key: 43 urlsafe-base64 chars + '='.
FERNET = "ThsabeURWix83kh6gKFa6ee60UO6k5yWOknwjsezh3Q="


class _F:
    """Minimal stand-in for a PyGithub File (only .filename/.patch are read)."""

    def __init__(self, filename, patch):
        self.filename = filename
        self.patch = patch


# --- the regression: unquoted, prefixed credential names ---------------------

@pytest.mark.parametrize("line", [
    "LM_FERNET_KEY=" + FERNET,
    "ENCRYPTION_KEY=" + FERNET,
    "LM_HUB_SECRET=s3cr3tvalue0123456789abcdef",
    "DB_PASSWORD=sup3rs3cretpassw0rd123",
    "MY_API_TOKEN=abcdef0123456789abcdef",
    # systemd unit / docker-compose shapes
    "Environment=LM_FERNET_KEY=" + FERNET,
    "  - LM_FERNET_KEY=" + FERNET,
])
def test_unquoted_prefixed_credentials_are_caught(line):
    assert _scan_line(line), "should have been flagged: %r" % line


def test_fernet_key_is_named_as_an_encryption_key():
    """The finding must say it's an encryption key: leaking one means every
    encrypted state file has to be re-encrypted, not just the key swapped."""
    names = [n for n, _ in _scan_line("LM_FERNET_KEY=" + FERNET)]
    assert any("Encryption key" in n for n in names), names


def test_one_secret_line_yields_exactly_one_finding():
    """Three rules can match a single .env line; a reviewer must not see the
    same secret reported three times."""
    assert len(_scan_line("LM_FERNET_KEY=" + FERNET)) == 1


# --- the pre-existing behaviour must not regress -----------------------------

@pytest.mark.parametrize("line", [
    "aws_key = 'AKIAIOSFODNN7EXAMPLE'",
    "token = 'ghp_" + "a" * 36 + "'",
    "-----BEGIN RSA PRIVATE KEY-----",
    "url = 'postgresql://admin:hunter2@db.internal:5432/app'",
])
def test_existing_signatures_still_fire(line):
    assert _scan_line(line), "regressed: %r" % line


# --- false positives: this runs on every PR, so noise is a real cost ---------

@pytest.mark.parametrize("line", [
    # the correct patterns we never want to discourage
    'key = os.environ["LM_FERNET_KEY"]',
    'password = os.getenv("DB_PASSWORD")',
    'api_key = config.get("api_key")',
    "self.secret_key = derive_key(seed)",
    # placeholders / templating
    "LM_FERNET_KEY=${FERNET}",
    "LM_FERNET_KEY=changeme",
    "LM_FERNET_KEY=<your-key-here>",
    "LM_FERNET_KEY=",
    # structurally excluded by the value charset
    "TOKEN_URL=https://login.example.com/oauth/token",
    "SECRET_PATH=/etc/lm/secret.json",
    "SESSION_TOKEN_TTL_SECONDS=3600",
])
def test_ordinary_config_lines_stay_quiet(line):
    assert not _scan_line(line), "false positive on %r" % line


# --- end-to-end through check_secrets ---------------------------------------

def test_check_secrets_flags_an_added_env_line():
    patch = "@@ -0,0 +1,2 @@\n+# hub config\n+LM_FERNET_KEY=%s\n" % FERNET
    findings = check_secrets([_F(".env", patch)])
    assert len(findings) == 1
    assert findings[0]["level"] == "error"
    assert ".env" in findings[0]["title"]


def test_removed_lines_are_not_flagged():
    """Deleting a secret is the fix, not the offence."""
    patch = "@@ -1,2 +1,1 @@\n-LM_FERNET_KEY=%s\n+LM_FERNET_KEY=${FERNET}\n" % FERNET
    assert check_secrets([_F(".env", patch)]) == []


def test_the_secret_value_is_never_echoed_in_full():
    """Findings are posted as PUBLIC PR comments — re-publishing the secret
    there would widen the exposure this check exists to catch."""
    patch = "@@ -0,0 +1 @@\n+LM_FERNET_KEY=%s\n" % FERNET
    blob = repr(check_secrets([_F(".env", patch)]))
    assert FERNET not in blob
    assert "redacted" in blob


def test_binary_or_patchless_files_are_skipped_not_raised():
    assert check_secrets([_F("logo.png", None)]) == []
