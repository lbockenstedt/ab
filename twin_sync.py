"""
twin_sync.py — Twin repository parity detection and synchronization.

Handles twin parity between agent/src/ and src/ in repositories such as pxmx:
- detect_twin_drift: detects when twin files are modified asymmetrically or diffs mismatch.
- mirror_twin_content: generates mirrored content update payload for the twin file.
"""
# Type hints only; they document the shapes of the values passed around.
from typing import Any, Dict, List, Optional


def detect_twin_drift(repo_name: str, files: List[Any]) -> List[Dict[str, str]]:
    """Check if a file in agent/src/ was changed without src/ (or vice versa),

    or if their patch contents differ.
    """
    # Lower-case the repo name once so the "pxmx" check below is case-insensitive.
    repo_lower = (repo_name or "").lower()
    # Index the changed files by path so a file's twin can be looked up directly.
    file_map = {}
    # Walk every changed file object (tolerating a None list) and record it by filename.
    for f in (files or []):
        # Some file objects may lack a filename; fall back to "" so they are skipped.
        fn = getattr(f, "filename", "") or ""
        # Only remember files that actually have a path.
        if fn:
            file_map[fn] = f

    # pxmx is the repo known to keep agent/src/ and src/ as mirrored twins.
    is_pxmx = "pxmx" in repo_lower
    # Detect whether this change touches the agent/src/ tree at all.
    has_agent_files = any(fn.startswith("agent/src/") for fn in file_map)

    # Only enforce drift if pxmx repo or if agent/src/ files are present in the change set
    if not is_pxmx and not has_agent_files:
        return []

    # Collected drift findings, one dict per out-of-sync twin pair.
    drifts: List[Dict[str, str]] = []
    # Remember which (agent, src) pairs were already compared so none is reported twice.
    checked_pairs = set()

    # Compare each changed file against its twin on the other side of the mirror.
    for fn, f in file_map.items():
        # Case 1: an agent/src/ file changed; its twin lives at the same path under src/.
        if fn.startswith("agent/src/"):
            # Strip the agent/src/ prefix to get the shared relative path, then build the twin path.
            rel = fn[len("agent/src/"):]
            twin_fn = f"src/{rel}"
            # Key pairs as (agent path, src path) so both directions map to the same key.
            pair_key = (fn, twin_fn)
            # This pair was already checked from the other side; skip it.
            if pair_key in checked_pairs:
                continue
            # Mark the pair as checked before comparing it.
            checked_pairs.add(pair_key)

            # The twin was not changed at all in this PR: one side was edited without the other.
            if twin_fn not in file_map:
                drifts.append({
                    "source": fn,
                    "twin": twin_fn,
                    "reason": "missing_twin",
                    "repo": repo_name,
                })
            # Both sides changed; compare their diffs to make sure they were edited identically.
            else:
                # Look up the twin's file object and read both patches
                # (a missing patch becomes an empty string).
                twin_f = file_map[twin_fn]
                f_patch = getattr(f, "patch", "") or ""
                t_patch = getattr(twin_f, "patch", "") or ""
                # Different diffs mean the two copies are drifting apart; record it.
                if f_patch != t_patch:
                    drifts.append({
                        "source": fn,
                        "twin": twin_fn,
                        "reason": "patch_mismatch",
                        "repo": repo_name,
                    })

        # Case 2: a src/ file changed in pxmx; its twin lives under agent/src/.
        elif fn.startswith("src/") and is_pxmx:
            # Strip the src/ prefix and build the matching agent/src/ path.
            rel = fn[len("src/"):]
            twin_fn = f"agent/src/{rel}"
            # Use the same (agent path, src path) key order as case 1 to de-duplicate.
            pair_key = (twin_fn, fn)
            # Already compared when its agent/src/ twin was visited; skip it.
            if pair_key in checked_pairs:
                continue
            # Mark the pair as checked before comparing it.
            checked_pairs.add(pair_key)

            # The agent/src/ twin was not changed: the mirror was only updated on one side.
            if twin_fn not in file_map:
                drifts.append({
                    "source": fn,
                    "twin": twin_fn,
                    "reason": "missing_twin",
                    "repo": repo_name,
                })
            # Both sides changed; check that their diffs match exactly.
            else:
                # Look up the twin's file object and read both patches
                # (a missing patch becomes an empty string).
                twin_f = file_map[twin_fn]
                f_patch = getattr(f, "patch", "") or ""
                t_patch = getattr(twin_f, "patch", "") or ""
                # Different diffs mean the twins no longer match; record it.
                if f_patch != t_patch:
                    drifts.append({
                        "source": fn,
                        "twin": twin_fn,
                        "reason": "patch_mismatch",
                        "repo": repo_name,
                    })

    # Return every drift found (empty list means the twins are in sync).
    return drifts


def mirror_twin_content(source_path: str, target_path: str, source_content: str) -> Dict[str, str]:
    """Generate matching file update payload for the twin file."""
    # Package the source content as an update aimed at the twin path ("path" mirrors
    # "target_path" for callers that expect a generic file-update payload).
    return {
        "source_path": source_path,
        "target_path": target_path,
        "path": target_path,
        "content": source_content,
        "status": "mirrored",
    }
