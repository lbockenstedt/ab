"""
contract_guard.py — Wire Contract & Protocol Compatibility Auditor.

Audits changes to WebSocket message types, handlers, and spoke payloads:
- Flags removal or renaming of message type constants (_TYPE_*, PXMX_*, NETBOX_*).
- Flags removal of expected keys from JSON dictionary payloads without fallback .get(key, default).
"""
import re
from typing import Any, Dict, List

CONST_PATTERN = re.compile(r"\b(_TYPE_[A-Z0-9_]+|PXMX_[A-Z0-9_]+|NETBOX_[A-Z0-9_]+)\b")
PAYLOAD_INDEX_PATTERN = re.compile(r"\b(payload|data|msg|message|body)\[['\"]([a-zA-Z0-9_]+)['\"]\]")
REMOVED_KEY_PATTERN = re.compile(r"^\s*-\s*['\"]([a-zA-Z0-9_]+)['\"]\s*:")


def audit_wire_contract(files: List[Any]) -> List[Dict[str, Any]]:
    """Audit changes to wire contracts, message constants, and payload dictionary schemas."""
    findings: List[Dict[str, Any]] = []

    for f in (files or []):
        filename = getattr(f, "filename", "") or ""
        raw_patch = getattr(f, "patch", None) or ""
        if not raw_patch:
            continue

        lines = raw_patch.splitlines()
        removed_lines = [l[1:] for l in lines if l.startswith("-") and not l.startswith("---")]
        added_lines = [l[1:] for l in lines if l.startswith("+") and not l.startswith("+++")]

        removed_text = "\n".join(removed_lines)
        added_text = "\n".join(added_lines)

        # 1. Message type constants removed or renamed
        removed_constants = set(CONST_PATTERN.findall(removed_text))
        added_constants = set(CONST_PATTERN.findall(added_text))

        for const in sorted(removed_constants):
            if const not in added_constants:
                findings.append({
                    "type": "wire_contract_constant_removed",
                    "file": filename,
                    "constant": const,
                    "title": "Wire contract constant removed or renamed",
                    "detail": f"Removal or renaming of message type constant `{const}` violates backward compatibility.",
                    "level": "warning",
                })

        # 2. Key removal from dictionary payloads
        removed_keys = set()
        for line in lines:
            if line.startswith("-") and not line.startswith("---"):
                m = REMOVED_KEY_PATTERN.search(line)
                if m:
                    removed_keys.add(m.group(1))

        added_keys = set()
        for line in added_lines:
            m = re.search(r"['\"]([a-zA-Z0-9_]+)['\"]\s*:", line)
            if m:
                added_keys.add(m.group(1))

        for k in sorted(removed_keys):
            if k not in added_keys:
                findings.append({
                    "type": "wire_contract_payload_key_removed",
                    "file": filename,
                    "key": k,
                    "title": "Payload key removed from wire contract",
                    "detail": f"Removal of expected payload key `{k}` from JSON payload without backward fallback.",
                    "level": "warning",
                })

        # 3. Direct dictionary indexing without .get(key, default)
        for line in added_lines:
            for match in PAYLOAD_INDEX_PATTERN.finditer(line):
                var_name, key_name = match.group(1), match.group(2)
                # Check if .get is also on this line or nearby
                if f".get(" not in line:
                    findings.append({
                        "type": "wire_contract_unsafe_key_access",
                        "file": filename,
                        "key": key_name,
                        "title": "Unsafe direct payload dictionary access",
                        "detail": f"Direct indexing `{var_name}['{key_name}']` detected. Use `.get('{key_name}', default)` for safe schema evolution.",
                        "level": "warning",
                    })

    return findings
