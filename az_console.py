"""az_console.py — bridge from AppBuilder's chat agent to the LM Azure resource
group, so the assistant can DIAGNOSE (and, when the operator opts in, ACT on)
the live LM servers it manages. This is the consumer of the Phase-1 secretless
identity: it shells out through the login helper (default
/usr/local/bin/lm-az-login) which proves the host is LM-AB via its managed
identity, pulls the scoped service-principal cert from LM-VAULT into tmpfs,
and logs `az` in as that SP (Virtual Machine Contributor on the LM RG). No
secret is ever handled here or persisted to disk.

Two capabilities, each with a pure, unit-testable vetting function separated
from the impure subprocess call so the security logic can be tested with no az
present:

    vet_az_args(argv, allow_mutation)          -> (ok, reason)
    run_az(argv, config)                       -> result dict     (az control-plane)

    vet_shell_command(cmd, allow_mutation)     -> (ok, reason)
    run_server_shell(vm, cmd, config)          -> result dict     (shell ON a VM
                                                  via `az vm run-command invoke`)

DEFAULT-DENY MUTATION
---------------------
Unless `allow_mutation` is True, ONLY read-only shapes are permitted and every
other request is refused BEFORE any process starts. This mirrors the operator's
whole-project stance: diagnosis is safe and unattended; change requires an
explicit opt-in (config CHAT_AZURE_ALLOW_MUTATION). When mutation IS allowed,
the ONLY boundary is the SP's RBAC scope — deliberately, per the operator's
"guard rails come once it works, and the RBAC scope IS the first guard rail".

Both capabilities are gated upstream by CHAT_AZURE_ENABLED (default False), so
on a host without the helper (e.g. a dev laptop) the tools simply report that
Azure access is disabled rather than erroring.
"""
import os
import re
import shlex
import subprocess

# ── az control-plane read-only allowlist ────────────────────────────────────
# Matched against the first one/two tokens of the az argv. Deliberately small:
# these are the shapes a diagnosis needs and none of them mutate. run-command
# is intentionally absent here — running a shell ON a VM goes through the
# separately-vetted run_server_shell path, never through raw `az`.
_AZ_READ_PREFIXES = {
    ("account", "show"), ("account", "list-locations"),
    ("group", "show"), ("group", "list"),
    ("resource", "list"), ("resource", "show"),
    ("vm", "list"), ("vm", "show"), ("vm", "get-instance-view"),
    ("vm", "list-ip-addresses"), ("vm", "list-sizes"),
    ("network", "nic"), ("network", "vnet"), ("network", "nsg"),
    ("monitor", "metrics"), ("monitor", "activity-log"), ("monitor", "log-analytics"),
    ("disk", "list"), ("disk", "show"),
}
# Verbs that mutate — refused in read-only mode no matter where they appear.
_AZ_MUTATION_TOKENS = {
    "create", "delete", "update", "set", "start", "stop", "restart", "deallocate",
    "run-command", "invoke", "reset", "redeploy", "reimage", "capture", "generalize",
    "add", "remove", "attach", "detach", "assign", "purge", "rotate", "regenerate",
}


def vet_az_args(argv, allow_mutation=False):
    """Vet an `az` argv (WITHOUT the leading 'az'). Returns (ok, reason).
    In read-only mode the first two non-flag tokens must be an allowlisted
    read shape and no mutation verb may appear anywhere."""
    if not argv or not isinstance(argv, (list, tuple)):
        return False, "no az command provided"
    if any(not isinstance(a, str) for a in argv):
        return False, "az arguments must all be strings (argv form, not a shell string)"
    if allow_mutation:
        return True, "mutation allowed (bounded by the service principal's RBAC scope)"
    positional = [a for a in argv if not a.startswith("-")]
    lowered = [a.lower() for a in positional]
    for tok in lowered:
        if tok in _AZ_MUTATION_TOKENS:
            return False, (f"'{tok}' is a mutating operation; enable "
                           f"CHAT_AZURE_ALLOW_MUTATION to permit it")
    head = tuple(lowered[:2])
    if head in _AZ_READ_PREFIXES or (len(lowered) >= 1 and (lowered[0], "") in _AZ_READ_PREFIXES):
        return True, "read-only az command"
    if lowered and (lowered[0],) in {(p[0],) for p in _AZ_READ_PREFIXES} and len(lowered) == 1:
        return True, "read-only az command"
    return False, ("not an allowlisted read-only az command "
                   "(e.g. `vm list`, `vm get-instance-view`, `resource list`); "
                   "enable CHAT_AZURE_ALLOW_MUTATION for anything else")


# ── server-shell read-only allowlist ────────────────────────────────────────
# First token of each pipeline segment must be one of these read-only binaries.
_SHELL_READ_BINARIES = {
    "systemctl", "journalctl", "service", "cat", "tail", "head", "grep", "egrep",
    "ls", "stat", "ps", "top", "df", "du", "free", "uptime", "uname", "hostname",
    "date", "whoami", "id", "env", "printenv", "ss", "netstat", "ip", "curl",
    "wget", "ping", "dig", "nslookup", "docker", "podman", "echo", "which",
    "find", "wc", "sort", "uniq", "awk", "sed", "true", "test", "pgrep",
}
# systemctl/service subcommands that are read-only (status/show only).
_SYSTEMCTL_READ_SUBCMDS = {"status", "is-active", "is-enabled", "is-failed", "show",
                           "list-units", "list-unit-files", "cat", "get-default",
                           "is-system-running", "list-dependencies", "list-timers",
                           "list-sockets", "show-environment"}
# Hard mutation markers refused anywhere in read-only mode.
_SHELL_MUTATION_MARKERS = [
    ">", ">>", "|&", "rm ", "mv ", "cp ", "tee ", "dd ", "mkfs", "chmod", "chown",
    "kill", "reboot", "shutdown", "poweroff", "halt", "apt", "yum", "dnf", "pip ",
    "npm ", "git ", "truncate", "ln ", "mount", "umount", "sysctl -w", "iptables",
    "useradd", "userdel", "passwd", "crontab", "at ",
]
_SED_INPLACE_RE = re.compile(r"\bsed\b[^|;]*\s-\w*i")  # sed -i (in-place edit)


def vet_shell_command(cmd, allow_mutation=False):
    """Vet a shell command to run ON an LM VM. Returns (ok, reason). In
    read-only mode: no mutation markers anywhere, and every pipeline/`;`/`&&`
    segment must START with a read-only binary (systemctl/service restricted
    to status-like subcommands). Conservative — anything unrecognised fails."""
    if not cmd or not isinstance(cmd, str) or not cmd.strip():
        return False, "no shell command provided"
    if allow_mutation:
        return True, "mutation allowed (bounded by the service principal's RBAC scope)"
    low = cmd.lower()
    for marker in _SHELL_MUTATION_MARKERS:
        if marker in low:
            return False, (f"'{marker.strip()}' indicates a state change; enable "
                           f"CHAT_AZURE_ALLOW_MUTATION to permit it")
    if _SED_INPLACE_RE.search(low):
        return False, "in-place sed edit (sed -i) is a mutation; enable CHAT_AZURE_ALLOW_MUTATION"
    # Split into segments on ; | && || and vet each segment's leading binary.
    segments = re.split(r"\|\||&&|[;|]", cmd)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            return False, "could not parse shell command safely"
        if not toks:
            continue
        binary = os.path.basename(toks[0])
        if binary not in _SHELL_READ_BINARIES:
            return False, (f"'{binary}' is not an allowlisted read-only command; "
                           f"enable CHAT_AZURE_ALLOW_MUTATION for arbitrary commands")
        if binary in ("systemctl", "service"):
            sub = next((t for t in toks[1:] if not t.startswith("-")), "")
            # `service <name> status` puts the subcommand last; accept either order.
            subs = {t for t in toks[1:] if not t.startswith("-")}
            if not (sub in _SYSTEMCTL_READ_SUBCMDS or subs & _SYSTEMCTL_READ_SUBCMDS):
                return False, (f"systemctl/service '{sub}' is not a read-only subcommand "
                               f"(allowed: status/is-active/show/…)")
    return True, "read-only shell command"


def azure_enabled(config):
    return bool((config or {}).get("CHAT_AZURE_ENABLED", False))


def _login_helper(config):
    return (config or {}).get("AZURE_LOGIN_HELPER") or "/usr/local/bin/lm-az-login"


def _resource_group(config):
    return (config or {}).get("AZURE_LM_RESOURCE_GROUP") or "LM"


def _ensure_login(config, timeout=60):
    """Run the secretless login helper so the subsequent az call has a valid SP
    context. Returns (ok, message). Missing helper (dev host) -> (False, note)."""
    helper = _login_helper(config)
    if not os.path.exists(helper):
        return False, (f"Azure login helper not found at {helper} — Azure access is only "
                       f"available from LM-AB, not this host.")
    try:
        p = subprocess.run([helper], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "az login helper timed out"
    except Exception as e:  # noqa: BLE001
        return False, f"az login helper failed to start: {type(e).__name__}: {e}"
    if p.returncode != 0:
        return False, f"az login helper failed: {(p.stderr or p.stdout or '').strip()[:400]}"
    return True, "authenticated"


def _cap(text, limit=12000):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n…[output truncated]"


def run_az(argv, config, timeout=120):
    """Run a vetted `az` control-plane command. `argv` is the arg list AFTER
    'az'. Returns a result dict (never raises)."""
    if not azure_enabled(config):
        return {"error": "Azure access is disabled (set CHAT_AZURE_ENABLED to enable)."}
    allow = bool((config or {}).get("CHAT_AZURE_ALLOW_MUTATION", False))
    ok, reason = vet_az_args(argv, allow_mutation=allow)
    if not ok:
        return {"error": f"refused: {reason}", "argv": list(argv)}
    ok, msg = _ensure_login(config)
    if not ok:
        return {"error": msg}
    az = (config or {}).get("AZURE_AZ_BIN") or "az"
    try:
        p = subprocess.run([az, *argv], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"az command timed out after {timeout}s", "argv": list(argv)}
    except FileNotFoundError:
        return {"error": f"az binary not found ({az})"}
    return {"argv": list(argv), "exit_code": p.returncode,
            "stdout": _cap(p.stdout), "stderr": _cap((p.stderr or "").strip(), 2000)}


def run_server_shell(vm, cmd, config, timeout=180):
    """Run a vetted shell command ON one LM VM via `az vm run-command invoke`.
    Returns a result dict (never raises)."""
    if not azure_enabled(config):
        return {"error": "Azure access is disabled (set CHAT_AZURE_ENABLED to enable)."}
    vm = (vm or "").strip()
    if not vm:
        return {"error": "vm name is required"}
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", vm):
        return {"error": f"invalid vm name: {vm!r}"}
    allow = bool((config or {}).get("CHAT_AZURE_ALLOW_MUTATION", False))
    ok, reason = vet_shell_command(cmd, allow_mutation=allow)
    if not ok:
        return {"error": f"refused: {reason}", "vm": vm}
    ok, msg = _ensure_login(config)
    if not ok:
        return {"error": msg}
    az = (config or {}).get("AZURE_AZ_BIN") or "az"
    rg = _resource_group(config)
    argv = [az, "vm", "run-command", "invoke", "-g", rg, "-n", vm,
            "--command-id", "RunShellScript", "--scripts", cmd,
            "--query", "value[0].message", "-o", "tsv"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"run-command timed out after {timeout}s", "vm": vm}
    except FileNotFoundError:
        return {"error": f"az binary not found ({az})"}
    return {"vm": vm, "resource_group": rg, "command": cmd, "exit_code": p.returncode,
            "output": _cap(p.stdout), "stderr": _cap((p.stderr or "").strip(), 2000)}
