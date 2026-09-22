"""
twin_sync.py — Twin repository parity detection and synchronization.

Handles twin parity between agent/src/ and src/ in repositories such as pxmx:
- detect_twin_drift: detects when twin files are modified asymmetrically or diffs mismatch.
- mirror_twin_content: generates mirrored content update payload for the twin file.
"""
from typing import Any, Dict, List, Optional


def detect_twin_drift(repo_name: str, files: List[Any]) -> List[Dict[str, str]]:
    """Check if a file in agent/src/ was changed without src/ (or vice versa),

    or if their patch contents differ.
    """
    repo_lower = (repo_name or "").lower()
    file_map = {}
    for f in (files or []):
        fn = getattr(f, "filename", "") or ""
        if fn:
            file_map[fn] = f

    is_pxmx = "pxmx" in repo_lower
    has_agent_files = any(fn.startswith("agent/src/") for fn in file_map)

    # Only enforce drift if pxmx repo or if agent/src/ files are present in the change set
    if not is_pxmx and not has_agent_files:
        return []

    drifts: List[Dict[str, str]] = []
    checked_pairs = set()

    for fn, f in file_map.items():
        if fn.startswith("agent/src/"):
            rel = fn[len("agent/src/"):]
            twin_fn = f"src/{rel}"
            pair_key = (fn, twin_fn)
            if pair_key in checked_pairs:
                continue
            checked_pairs.add(pair_key)

            if twin_fn not in file_map:
                drifts.append({
                    "source": fn,
                    "twin": twin_fn,
                    "reason": "missing_twin",
                    "repo": repo_name,
                })
            else:
                twin_f = file_map[twin_fn]
                f_patch = getattr(f, "patch", "") or ""
                t_patch = getattr(twin_f, "patch", "") or ""
                if f_patch != t_patch:
                    drifts.append({
                        "source": fn,
                        "twin": twin_fn,
                        "reason": "patch_mismatch",
                        "repo": repo_name,
                    })

        elif fn.startswith("src/") and is_pxmx:
            rel = fn[len("src/"):]
            twin_fn = f"agent/src/{rel}"
            pair_key = (twin_fn, fn)
            if pair_key in checked_pairs:
                continue
            checked_pairs.add(pair_key)

            if twin_fn not in file_map:
                drifts.append({
                    "source": fn,
                    "twin": twin_fn,
                    "reason": "missing_twin",
                    "repo": repo_name,
                })
            else:
                twin_f = file_map[twin_fn]
                f_patch = getattr(f, "patch", "") or ""
                t_patch = getattr(twin_f, "patch", "") or ""
                if f_patch != t_patch:
                    drifts.append({
                        "source": fn,
                        "twin": twin_fn,
                        "reason": "patch_mismatch",
                        "repo": repo_name,
                    })

    return drifts


def mirror_twin_content(source_path: str, target_path: str, source_content: str) -> Dict[str, str]:
    """Generate matching file update payload for the twin file."""
    return {
        "source_path": source_path,
        "target_path": target_path,
        "path": target_path,
        "content": source_content,
        "status": "mirrored",
    }
