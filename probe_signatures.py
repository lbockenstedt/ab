"""Shared HTTPS-port probe / scanner signature classifier.

Single source of truth for "does this request path look like an automated
vulnerability scanner fingerprinting the port?" — used by the hub HTTP
middleware (``core/src/api.py``) AND by every edge component that serves its own
HTTPS surface (the reverse proxy, AppBuilder, role-hosted spoke UIs). Keeping
the signature lists in ONE importable module (rather than a copy per edge) means
a new scanner signature added here is picked up everywhere at once. The one
exception is AB, a separate repo: ab/probe_signatures.py is a byte-identical
vendored copy of lm/core/src/security/probe_signatures.py (canonical) — re-copy
it when this file changes.

The components that import this all serve a Python API + a static JS SPA over
:443 — they never serve PHP/ASP/JSP/CGI, dotfiles, DB admin panels, or
app-server consoles. A request for any of those is a scanner, not a real client
(the SPA catch-all would otherwise answer 200 index.html and hide the probe).
High-confidence signatures only: SPA deep-links and static assets
(``.js``/``.css``/``.svg``/...) never match, so a mistyped in-app route is not
mistaken for an attack.
"""

# Postpone evaluation of type hints so annotations stay plain strings at runtime.
from __future__ import annotations

# Path SUFFIXES a legitimate SPA/API never serves. Extension-based scanner
# fingerprints (PHP/ASP/JSP/CGI stacks, secret/backup files). A tuple so it is
# immutable and can be fed straight to the endswith() check below.
PROBE_SUFFIXES = (
    ".php", ".php5", ".php7", ".phtml", ".asp", ".aspx", ".jsp", ".jspx",
    ".cgi", ".env", ".sql", ".bak", ".htaccess", ".htpasswd",
)

# Path SUBSTRINGS that only appear in scanner traffic (CMS probes, VCS/secret
# dirs, DB admin panels, app-server consoles, known-CVE endpoints). Matched
# anywhere in the lower-cased path, so each token starts with "/" to anchor it
# to a path segment and avoid false hits inside ordinary words.
PROBE_TOKENS = (
    "/wp-", "/wordpress/", "/.git", "/.env", "/.aws", "/.ssh/", "/.svn",
    "/phpmyadmin", "/pma/", "/adminer", "/dbadmin", "/mysql", "/xmlrpc",
    "/actuator/", "/solr/", "/jenkins/", "/manager/html", "/vendor/",
    "/phpunit", "/eval-stdin", "/.vscode", "/.idea", "/boaform", "/hnap1",
    "/owa/", "/autodiscover/", "/cgi-bin/",
)


def looks_like_probe(path: str) -> bool:
    """True when ``path`` matches a known external-scanner signature that none
    of our HTTPS surfaces legitimately serves.

    High-confidence only — SPA deep-links and static assets never match, so a
    mistyped in-app route is not mistaken for an attack.
    """
    # Lower-case the path (treating None as empty) so "/WP-Admin" and "/wp-admin"
    # match the same lower-case signature.
    p = (path or "").lower()
    # An empty path cannot be a probe; bail out early.
    if not p:
        return False
    # Extension check first: a forbidden file type at the end is a sure scanner sign.
    if any(p.endswith(sfx) for sfx in PROBE_SUFFIXES):
        return True
    # Otherwise it is a probe only if a scanner-only substring appears anywhere.
    return any(tok in p for tok in PROBE_TOKENS)
