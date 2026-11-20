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
# os checks the helper path and binary names; re does pattern checks; shlex splits shell
# commands the way a shell would; subprocess runs az and the login helper.
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
# run-command executes as root, so in read-only mode a command must neither
# change state nor print a secret. The first token of each pipeline segment
# must be one of these binaries; _BINARY_VETTERS then checks its options,
# because several "read-only" tools run commands, write files or print other
# files when given the right flag (`env -S`, `sort -o`, `date -f`, `ip netns exec`).
_SHELL_READ_BINARIES = {
    "systemctl", "journalctl", "service", "cat", "tail", "head", "grep", "egrep",
    "ls", "stat", "ps", "top", "df", "du", "free", "uptime", "uname", "hostname",
    "date", "whoami", "id", "env", "printenv", "ss", "netstat", "ip", "curl",
    "ping", "dig", "nslookup", "docker", "echo", "which",
    "find", "wc", "sort", "uniq", "true", "test", "pgrep",
}
# Binaries that are general-purpose interpreters: being on the "read-only" list
# is meaningless for them because their *script argument* can spawn anything
# (`awk 'BEGIN{system("id")}'`, `sed '1e id'`). Never allowlist these.
_SHELL_INTERPRETERS = {
    "awk", "gawk", "mawk", "nawk", "sed", "perl", "python", "python3", "ruby",
    "sh", "bash", "zsh", "dash", "ksh", "lua", "node", "php", "tclsh", "expect",
    "xargs", "eval", "exec", "nc", "ncat", "netcat", "socat", "ssh", "telnet",
}
# `find` actions that execute or write, and -files0-from, whose error messages
# echo the named file's contents. `find` itself only reads names.
_FIND_ACTION_FLAGS = {
    "-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls", "-fprint",
    "-fprint0", "-fprintf", "-files0-from",
}
# Raw substrings that redirect output (a write), refused anywhere in the command.
# Hard mutation markers refused anywhere in read-only mode. Split by how they
# must be matched: shell operators are raw substrings, but command names must be
# matched as whole tokens — a substring test wrongly rejects `cat` (contains
# "at "), `ss -tlnp | grep tcp` (contains "cp ") and `add`/`dd`.
_SHELL_MUTATION_OPERATORS = [">", ">>", "|&"]
# Command names that change system state; matched as whole tokens, not substrings.
_SHELL_MUTATION_COMMANDS = {
    "rm", "mv", "cp", "tee", "dd", "mkfs", "chmod", "chown", "kill", "pkill",
    "killall", "reboot", "shutdown", "poweroff", "halt", "apt", "apt-get", "yum",
    "dnf", "pip", "pip3", "npm", "git", "truncate", "ln", "mount", "umount",
    "sysctl", "iptables", "nft", "useradd", "userdel", "usermod", "groupadd",
    "passwd", "crontab", "at", "insmod", "modprobe", "swapoff", "fdisk",
    "parted", "mkswap", "setenforce", "sudo", "su", "install", "patch", "tar",
    "unzip", "gunzip", "openssl", "curlftpfs",
}
# Command substitution / process substitution smuggle a second command inside an
# otherwise-innocent one (`cat $(curl evil)`), so they are refused outright.
_SHELL_SUBSTITUTION = ["`", "$(", "${", "<(", ">("]
# Any other '$' expands a variable or an ANSI-C string ($'restart') that shlex
# reads differently from bash, and '<' feeds a file into a command. Either one
# means the vetter would not see what bash actually runs or reads.
_SHELL_EXPANSION_CHARS = ["$", "<"]
# Unquoted, bash brace-expands '{a,b}' after this check has seen the token
# (`journalctl --{vacuum-size=1K,}` becomes `--vacuum-size=1K --`), and '#'
# starts a comment that hides the rest of the line from bash but not from shlex.
_SHELL_UNQUOTED_CHARS = "{}#"
# A program given by path must be a system binary, not a same-named file
# elsewhere (`./cat`, `/tmp/x/cat`).
_SYSTEM_BIN_DIRS = {"/bin", "/usr/bin", "/sbin", "/usr/sbin"}
# Detects "sed ... -i" (in-place file edit) anywhere in a segment.
_SED_INPLACE_RE = re.compile(r"\bsed\b[^|;]*\s-\w*i")  # sed -i (in-place edit)

# ── file reads (as root) ────────────────────────────────────────────────────
# Binaries that print file contents. Every operand naming a path is checked by
# _read_path_ok, because root can read /etc/shadow, /etc/ab/.env, private keys
# and /proc/<pid>/environ, and the output goes back into the chat.
_CONTENT_READERS = {"cat", "tail", "head", "grep", "egrep", "sort", "uniq"}
# Readable in full, with globs and recursive grep: logs and unit files hold no
# secrets.
_OPEN_READ_ROOTS = ("/var/log/", "/usr/lib/systemd/", "/lib/systemd/")
# Readable file by file, minus secret-looking names. Globs and recursion are
# refused here, since a pattern would hide the name from the check.
_NAMED_READ_ROOTS = ("/etc/nginx/", "/etc/systemd/", "/etc/apt/sources.list.d/")
# Application trees under /opt: only versions, logs and source, never the .env,
# data or config files that sit beside them.
_OPT_READ_NAME_RE = re.compile(
    r"VERSION|.+\.(log|py|js|html|css|md|txt|sh|service)|.+\.log\.\d+")
# Individual host-information files that hold no secrets. /proc is listed file
# by file: /proc/<pid>/environ holds secrets and /proc/<pid>/root is the whole
# root filesystem.
_READ_FILES = {
    "/etc/hostname", "/etc/hosts", "/etc/os-release", "/etc/lsb-release",
    "/etc/issue", "/etc/timezone", "/etc/resolv.conf",
    "/etc/nsswitch.conf", "/etc/machine-id", "/etc/logrotate.conf",
    "/etc/sysctl.conf", "/etc/docker/daemon.json", "/etc/ssh/sshd_config",
    "/etc/waagent.conf", "/etc/apt/sources.list",
    "/proc/loadavg", "/proc/meminfo", "/proc/cpuinfo", "/proc/uptime",
    "/proc/version", "/proc/mounts", "/proc/stat", "/proc/vmstat",
    "/proc/diskstats", "/proc/swaps", "/proc/partitions", "/proc/pressure/cpu",
    "/proc/pressure/memory", "/proc/pressure/io", "/proc/net/dev",
    "/proc/net/tcp", "/proc/net/tcp6", "/proc/net/udp", "/proc/net/route",
    "/proc/net/sockstat",
}
# Path components that mark a secret: credential dirs (.git/config can hold a
# token in the remote URL), env files, keys and anything named like a secret.
_SECRET_COMPONENTS = {".git", ".ssh", ".gnupg", ".docker", ".azure", ".aws", ".kube",
                      ".netrc", ".pgpass", ".git-credentials"}
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".env")
_SECRET_PREFIXES = (".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
_SECRET_WORDS = ("secret", "token", "passw", "credential", "private", "htpasswd", "shadow")


def _unquoted_chars(cmd):
    """The characters of cmd that bash sees outside single or double quotes
    (a backslash escapes the next character, except inside single quotes)."""
    out, quote, i = [], "", 0
    while i < len(cmd):
        c = cmd[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _glob_words(cmd):
    """The shell words of cmd (raw text) that hold an unquoted glob character
    (* ? [), which bash expands into file names after this check has run."""
    words, cur, glob, quote, i = [], [], False, "", 0
    while i < len(cmd):
        c = cmd[i]
        if c == "\\" and quote != "'":
            cur.append(cmd[i:i + 2])
            i += 2
            continue
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c.isspace() or c in ";|&":
            if glob:
                words.append("".join(cur))
            cur, glob = [], False
            i += 1
            continue
        elif c in "*?[":
            glob = True
        cur.append(c)
        i += 1
    if glob:
        words.append("".join(cur))
    return words


# A glob word may only be a plain path under an open root: it then expands to
# log and unit file names, never to an option (`-*` matching a file named
# `-delete`) or to a file outside the root.
_GLOB_WORD_RE = re.compile(r"[A-Za-z0-9_./*?\[\]-]+")


def _glob_word_ok(word):
    """True if an unquoted glob word is a quote-free path under an open root.
    A glob component starting with '.' is refused: before bash 5.2
    (globskipdots) `.?`, `.*` and `.[.]` all match `..` and climb out."""
    parts = word.split("/")
    return (bool(_GLOB_WORD_RE.fullmatch(word)) and word.startswith(_OPEN_READ_ROOTS)
            and ".." not in parts
            and not any(p.startswith(".") and any(c in p for c in "*?[") for p in parts))


def _secret_component(part):
    """True if one path component looks like it names a secret."""
    p = part.lower()
    return (p in _SECRET_COMPONENTS or p.endswith(_SECRET_SUFFIXES)
            or p.startswith(_SECRET_PREFIXES) or any(w in p for w in _SECRET_WORDS))


def _read_path_ok(path, open_only=False):
    """(ok, reason) for a path a content reader would print as root.
    open_only limits it to the open roots (for recursive grep)."""
    # Only absolute paths: a relative one depends on run-command's cwd, and
    # ~ and {a,b} are expanded by bash after this check.
    if not path.startswith("/"):
        return False, f"'{path}': read-only mode reads absolute paths only"
    if any(c in path for c in "{}~"):
        return False, f"'{path}': brace and tilde expansion are not allowed"
    # Canonicalise without touching the disk: drop '' and '.', refuse '..'.
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        return False, f"'{path}': '..' is not allowed"
    canon = "/" + "/".join(parts)
    # Logs and unit files: anything goes, including globs.
    if (canon + "/").startswith(_OPEN_READ_ROOTS):
        return True, ""
    refusal = (f"'{path}' is outside the read-only file allowlist (logs under /var/log, "
               f"unit files, app versions/logs/source under /opt, host info) or may hold "
               f"a secret; enable CHAT_AZURE_ALLOW_MUTATION to read it")
    # Everywhere else the exact name is checked, so a glob would bypass it.
    if open_only or any(c in canon for c in "*?["):
        return False, refusal
    if canon in _READ_FILES:
        return True, ""
    if any(_secret_component(p) for p in parts):
        return False, refusal
    if canon.startswith(_NAMED_READ_ROOTS):
        return True, ""
    if canon.startswith("/opt/") and _OPT_READ_NAME_RE.fullmatch(parts[-1]):
        return True, ""
    return False, refusal


def _vet_read_operands(binary, args, open_only=False):
    """Check every path a content reader is given, as an operand or as an
    option value (`--file=/x`, `-f/x`). Tokens without '/' name files in
    run-command's own working directory."""
    for tok in args:
        if "~" in tok:
            return False, f"{binary}: '~' expands to a home directory; use an absolute path"
        if "/" not in tok:
            continue
        # Option value: after '=' (--file=/x) or from the first '/' (-f/x).
        if tok.startswith("-"):
            path = tok.split("=", 1)[1] if "=" in tok else tok[tok.index("/"):]
        else:
            path = tok
        ok, why = _read_path_ok(path, open_only)
        if not ok:
            return False, f"{binary}: {why} (a grep pattern may not contain '/')"
    return True, ""


# ── option helpers ──────────────────────────────────────────────────────────
def _long_opt_hits(tok, names):
    """True if tok is a GNU long option equal to, or abbreviating, one of names.
    getopt_long accepts any unambiguous prefix (`--out` for `--output`), so an
    exact-name check alone would be bypassable."""
    if not tok.startswith("--") or tok == "--":
        return False
    name = tok[2:].split("=", 1)[0].lower()
    return bool(name) and any(n.startswith(name) for n in names)


def _short_cluster(tok):
    """Letters of a single-dash option cluster (`-tlnp` -> 'tlnp'), else ''."""
    if tok.startswith("-") and not tok.startswith("--"):
        return tok[1:]
    return ""


def _refuse_opt(binary, tok, what):
    """Standard refusal for an option that is not allowed in read-only mode."""
    return False, (f"{binary} '{tok}' {what}; enable CHAT_AZURE_ALLOW_MUTATION "
                   f"to permit it")


_NUM_RE = re.compile(r"\d+(\.\d+)?")

# ── per-binary vetters: each takes (binary, args) and returns (ok, reason) ──
# Loopback only, in a form curl parses the same way this regex does: no
# userinfo, fragment or backslash that could move the real host elsewhere.
_FETCH_URL_RE = re.compile(
    r"(?:https?://)?(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)(?::\d{1,5})?"
    r"(?:/[A-Za-z0-9._~/?=&%:+,-]*)?", re.IGNORECASE)


def _fetch_target_ok(tok):
    """True if a curl URL argument points at loopback. Anything else can
    exfiltrate to an attacker or reach the instance-metadata endpoint."""
    return bool(_FETCH_URL_RE.fullmatch(tok))


def _vet_fetch_targets(binary, targets):
    """Every URL must be loopback, and there must be at least one."""
    if not targets:
        return False, f"{binary} needs an explicit loopback URL in read-only mode"
    for t in targets:
        if not _fetch_target_ok(t):
            return False, (f"{binary} may only fetch loopback URLs in read-only mode "
                           f"(got '{t}'); enable CHAT_AZURE_ALLOW_MUTATION to reach "
                           f"other hosts")
    return True, ""


# curl options are an allowlist: its option set is large (proxies, --connect-to,
# --resolve, headers, -w %output{file}) and only plain GETs are needed here.
_CURL_FLAGS = {"-s", "--silent", "-S", "--show-error", "-k", "--insecure", "-I",
               "--head", "-f", "--fail", "-v", "--verbose", "-i", "--include",
               "--compressed", "--no-progress-meter"}
_CURL_NUM_OPTS = {"-m", "--max-time", "--connect-timeout"}
_CURL_CLUSTER_RE = re.compile(r"-[sSkIfvi]*(m(\d+(\.\d+)?)?)?")


def _vet_curl(binary, args):
    """curl: allowlisted flags only, loopback URLs only."""
    targets, i = [], 0
    while i < len(args):
        t = args[i]
        m = _CURL_CLUSTER_RE.fullmatch(t)
        if t in _CURL_FLAGS:
            pass
        elif t in _CURL_NUM_OPTS or (m and t != "-" and m.group(1) == "m"):
            # A timeout whose number is the next token.
            if i + 1 >= len(args) or not _NUM_RE.fullmatch(args[i + 1]):
                return False, f"{binary} '{t}' needs a number"
            i += 1
        elif m and t != "-":
            pass
        elif t.startswith("-"):
            return _refuse_opt(binary, t, "is not allowed in read-only mode (allowed: "
                               "-s -S -k -I -f -v -i, -m/--max-time N, --connect-timeout N)")
        else:
            targets.append(t)
        i += 1
    return _vet_fetch_targets(binary, targets)


# docker subcommands that only inspect state. `inspect` is absent: it
# prints each container's environment, which holds its secrets. `top` passes
# extra arguments to ps (`docker top c e` prints the environment), so it is
# allowed with a container name only.
_CONTAINER_READ_SUBCMDS = {
    "ps", "images", "logs", "stats", "version", "info", "port", "diff",
    "history", "events",
}
_CONTAINER_NESTED_READ = {
    "image": {"ls", "list", "history"},
    "container": {"ls", "list", "ps", "logs", "port", "diff", "stats"},
}


def _vet_container(binary, args):
    """docker: a read-only subcommand first, no global options (-H points the
    client at another daemon). docker reads its global options only before
    the subcommand. podman is not allowed: it also takes them after it, and
    some (--out, the hidden --cpu-profile) create files as root."""
    if not args or args[0].startswith("-"):
        return False, (f"{binary} needs a read-only subcommand first, with no global "
                       f"options (allowed: ps/images/logs/stats/…)")
    sub = args[0].lower()
    if sub == "top":
        ok = len(args) == 2 and not args[1].startswith("-")
    elif sub in _CONTAINER_NESTED_READ:
        ok = len(args) > 1 and args[1].lower() in _CONTAINER_NESTED_READ[sub]
    else:
        ok = sub in _CONTAINER_READ_SUBCMDS
    if not ok:
        return False, (f"{binary} '{' '.join(args[:2])}' is not a read-only subcommand "
                       f"(allowed: ps/images/logs/stats/version/info/port/diff/history/"
                       f"events, image ls, container ls/logs, top <name>)")
    return True, ""


def _vet_env(binary, args):
    """env: only a bare print of the environment. Any other option can run a
    command (`env -S"bash -c id"`, `env -i cmd`)."""
    if all(a in ("-0", "--null") for a in args):
        return True, ""
    return False, ("env may only print the environment in read-only mode (no options, "
                   "VAR=value assignments or command)")


# systemctl/service subcommands that are read-only (status/show only).
_SYSTEMCTL_READ_SUBCMDS = {"status", "is-active", "is-enabled", "is-failed", "show",
                           "list-units", "list-unit-files", "cat", "get-default",
                           "is-system-running", "list-dependencies", "list-timers",
                           "list-sockets", "show-environment"}
# systemctl options are an allowlist, and options that take a value consume
# it, so `-p status restart ab` is read as the verb `restart`, as systemctl does.
_SYSTEMCTL_FLAGS = {"--no-pager", "--no-legend", "-a", "--all", "-l", "--full",
                    "--plain", "--failed", "-q", "--quiet", "--value", "-r",
                    "--recursive", "--no-ask-password", "--system"}
_SYSTEMCTL_VALUE_OPTS = {"-n", "--lines", "-t", "--type", "--state", "-p",
                         "--property", "-P", "-o", "--output"}


def _vet_systemctl(binary, args):
    """systemctl: allowlisted options, and the verb (first positional) must
    be a read-only one. Bare `systemctl` lists units."""
    positional, i = [], 0
    while i < len(args):
        t = args[i]
        if t == "--":
            positional += args[i + 1:]
            break
        if not t.startswith("-"):
            positional.append(t)
        elif (t in _SYSTEMCTL_FLAGS or re.fullmatch(r"-n\d+", t)
              or (t.startswith("--") and "=" in t
                  and t.split("=", 1)[0] in _SYSTEMCTL_VALUE_OPTS)):
            pass
        elif t in _SYSTEMCTL_VALUE_OPTS:
            if i + 1 >= len(args):
                return False, f"systemctl '{t}' needs a value"
            i += 1
        else:
            return _refuse_opt(binary, t, "is not allowed in read-only mode")
        i += 1
    verb = positional[0].lower() if positional else ""
    if verb and verb not in _SYSTEMCTL_READ_SUBCMDS:
        return False, (f"systemctl '{verb}' is not a read-only subcommand "
                       f"(allowed: status/is-active/show/…)")
    return True, ""


_UNIT_NAME_RE = re.compile(r"[A-Za-z0-9@._:-]+")


def _vet_service(binary, args):
    """service: exactly `service <name> status` or `service --status-all`.
    The name may not contain '/', since service runs /etc/init.d/<name>."""
    if args == ["--status-all"]:
        return True, ""
    if (len(args) == 2 and not args[0].startswith("-")
            and _UNIT_NAME_RE.fullmatch(args[0]) and args[1].lower() == "status"):
        return True, ""
    return False, ("service is limited to `service <name> status` and "
                   "`service --status-all` in read-only mode")


# journalctl options that delete, rotate or write journal data or files.
_JOURNALCTL_WRITE_OPTS = ("vacuum-size", "vacuum-time", "vacuum-files", "rotate",
                          "flush", "sync", "relinquish-var", "smart-relinquish-var",
                          "setup-keys", "update-catalog", "cursor-file")


def _vet_journalctl(binary, args):
    """journalctl: refuse the options that change the journal or write a file."""
    for t in args:
        if _long_opt_hits(t, _JOURNALCTL_WRITE_OPTS):
            return _refuse_opt(binary, t, "changes the journal or writes a file")
    return True, ""


def _vet_sort(binary, args):
    """sort: refuse -o (write), -T (temp files), --compress-program (exec) and
    the options that read other files."""
    for t in args:
        if (any(c in _short_cluster(t) for c in "oT")
                or _long_opt_hits(t, ("output", "compress-program", "files0-from",
                                      "temporary-directory", "random-source"))):
            return _refuse_opt(binary, t, "writes a file, runs a program or reads another file")
    return True, ""


def _vet_uniq(binary, args):
    """uniq: a second file operand is an OUTPUT file, so allow at most one.
    Values of -f/-s/-w are not operands. `--` is refused because every token
    after it is an operand, even one starting with '-', and a glob because bash
    may expand one word into two files."""
    operands, skip = 0, False
    if any(c in t for t in args for c in "*?["):
        return False, ("uniq writes its second file operand, and a glob can expand "
                       "to two files; name one file in read-only mode")
    for t in args:
        if skip:
            skip = False
        elif t == "--":
            return _refuse_opt(binary, t, "makes the next tokens file operands, and "
                               "the second one is written")
        elif t == "-" or not t.startswith("-"):
            operands += 1
        elif t.startswith("--"):
            skip = "=" not in t and _long_opt_hits(t, ("skip-fields", "skip-chars",
                                                       "check-chars"))
        else:
            skip = t[-1] in "fsw" and not any(c.isdigit() for c in t)
    if operands > 1:
        return False, ("uniq writes its second file operand; give it at most one "
                       "input in read-only mode")
    return True, ""


def _vet_grep(binary, args):
    """grep/egrep: a recursive search only under the open roots, since it would
    read every file below a directory, secrets included."""
    recursive = any(any(c in _short_cluster(t) for c in "rRd")
                    or _long_opt_hits(t, ("recursive", "dereference-recursive", "directories"))
                    for t in args)
    if recursive:
        # '.' and '..' have no '/', but recurse from run-command's working
        # directory, whose parent holds every earlier run-command script.
        if any(t in (".", "..") for t in args):
            return False, f"{binary}: a recursive search needs an absolute path under /var/log"
        return _vet_read_operands(binary, args, open_only=True)
    return True, ""


def _vet_files0(binary, args):
    """du/wc: --files0-from reads file names from a file and echoes them in
    errors, which prints that file's contents."""
    for t in args:
        if _long_opt_hits(t, ("files0-from",)):
            return _refuse_opt(binary, t, "prints another file's contents in its errors")
    return True, ""


# ip: allowlisted options, and the verb after the object must be a read one.
_IP_FLAGS = {"-4", "-6", "-s", "-stats", "-statistics", "-d", "-details", "-o",
             "-oneline", "-br", "-brief", "-c", "-color", "-j", "-json", "-p",
             "-pretty", "-h", "-human", "-r", "-resolve"}
_IP_VALUE_FLAGS = {"-f", "-family"}
_IP_READ_VERBS = {"show", "list", "ls", "lst", "get", "help"}


def _vet_ip(binary, args):
    """ip: `ip [opts] <object> [show|list|get …]`. Refuses add/del/set/flush,
    `netns exec` (runs a command) and -batch (reads a file)."""
    positional, i = [], 0
    while i < len(args):
        t = args[i]
        if not t.startswith("-"):
            positional.append(t)
        elif t in _IP_VALUE_FLAGS:
            if i + 1 >= len(args):
                return False, f"ip '{t}' needs a value"
            i += 1
        elif t not in _IP_FLAGS:
            return _refuse_opt(binary, t, "is not allowed in read-only mode")
        i += 1
    if len(positional) > 1 and positional[1].lower() not in _IP_READ_VERBS:
        return False, (f"ip '{positional[0]} {positional[1]}' is not a read-only command "
                       f"(allowed: show/list/get)")
    return True, ""


_DATE_FLAGS = {"-u", "--utc", "--universal", "-R", "--rfc-email", "-I", "--iso-8601"}
_DATE_SPEC_RE = re.compile(r"(-I|--iso-8601=|--rfc-3339=)(date|hours|minutes|seconds|ns)")


def _vet_date(binary, args):
    """date: print-only options. -s sets the clock and -f prints another
    file's lines in its errors."""
    i = 0
    while i < len(args):
        t = args[i]
        if t in ("-d", "--date"):
            if i + 1 >= len(args):
                return False, f"date '{t}' needs a value"
            i += 1
        elif not (t.startswith(("+", "--date=")) or t in _DATE_FLAGS
                  or _DATE_SPEC_RE.fullmatch(t)):
            return _refuse_opt(binary, t, "is not allowed in read-only mode (allowed: "
                               "+FORMAT, -u, -d/--date, -I, -R, --rfc-3339)")
        i += 1
    return True, ""


_HOSTNAME_FLAGS = {"-f", "--fqdn", "--long", "-s", "--short", "-i", "--ip-address",
                   "-I", "--all-ip-addresses", "-d", "--domain", "-A", "--all-fqdns",
                   "-a", "--alias"}


def _vet_hostname(binary, args):
    """hostname: print options only. A bare name or -F sets the hostname."""
    for t in args:
        if t not in _HOSTNAME_FLAGS:
            return _refuse_opt(binary, t, "sets the hostname or is not a print option")
    return True, ""


def _vet_ss(binary, args):
    """ss: refuse -K (kills sockets), -D (writes a file) and -F (reads one)."""
    for t in args:
        if any(c in _short_cluster(t) for c in "KDF") or _long_opt_hits(t, ("kill", "diag", "filter")):
            return _refuse_opt(binary, t, "kills sockets or reads/writes a file")
    return True, ""


# ps option letters that take the next token as their value, for dash-style
# and BSD-style clusters, and long options that do.
# ps prints every process's environment (where services keep their secrets)
# through BSD 'e' or the `environ` format field. procps first parses all
# arguments SysV-style; if ANY step fails (`-x`, an unknown user, field or sort
# key, a missing value, `-f` with `-o`, `-L` with `-T` or `-H`, two heading
# options), it re-reads EVERY argument BSD-style, so `-ef` becomes BSD e+f
# (procps-ng ps/parser.c arg_parse -> try_bsd). A command with no 'e' anywhere
# cannot print environments either way. One with an 'e' must fit a strict SysV
# grammar that procps always accepts: the flags below, -o/-p/-C values and a few
# long options, with format and sort fields from a known list.
_PS_FLAGS = set("AadefFHlLNTw")
_PS_FIELDS = {
    "pid", "ppid", "pgid", "sid", "tid", "lwp", "nlwp", "uid", "user", "ruser", "euser",
    "group", "comm", "cmd", "args", "command", "ucmd", "etime", "etimes", "time",
    "start", "stime", "lstart", "pcpu", "%cpu", "pmem", "%mem", "rss", "vsz", "sz",
    "stat", "state", "s", "tty", "tname", "ni", "nice", "pri", "psr", "wchan", "f",
    "c", "cls", "rtprio", "maj_flt", "min_flt"}
_PS_HEADER_RE = re.compile(r"[A-Za-z0-9_%]*")
_PS_NAME_RE = re.compile(r"[A-Za-z0-9_.:@+-]+")
_PS_PIDS_RE = re.compile(r"[1-9]\d{0,8}(,[1-9]\d{0,8})*")
_PS_LONG = {"--format": "o", "--sort": "sort", "--pid": "p"}


def _ps_value_ok(kind, val):
    """True if a ps option value always parses: a field list (an optional
    `=header` on the last one), sort keys, a pid list or a command name."""
    if kind == "o":
        fields, _, header = val.partition("=")
        return (all(f in _PS_FIELDS for f in fields.split(","))
                and bool(_PS_HEADER_RE.fullmatch(header)))
    if kind == "sort":
        return all(k.lstrip("+-") in _PS_FIELDS for k in val.split(","))
    if kind == "p":
        return bool(_PS_PIDS_RE.fullmatch(val))
    return bool(_PS_NAME_RE.fullmatch(val))


def _ps_strict_parse(args):
    """Parse ps args with the strict SysV grammar. Returns (letters, kinds,
    no_headers) or the offending token as a str when an argument falls outside."""
    letters, kinds, no_headers = set(), set(), 0
    i = 0
    while i < len(args):
        t = args[i]
        i += 1
        if t == "--forest":
            letters.add("H")  # the same forest display as -H
            continue
        if t == "--no-headers":
            no_headers += 1
            continue
        name, eq, val = t.partition("=")
        if name in _PS_LONG:
            if not eq:
                val = args[i] if i < len(args) else ""
                i += 1
            if not _ps_value_ok(_PS_LONG[name], val):
                return t
            kinds.add(_PS_LONG[name])
            continue
        if not t.startswith("-") or t.startswith("--") or len(t) < 2:
            return t
        # A dash cluster: flags, then at most one value letter whose value is
        # the rest of the token or the next token.
        for j, c in enumerate(t[1:], 1):
            if c in _PS_FLAGS:
                letters.add(c)
                continue
            if c not in "opC":
                return t
            val = t[j + 1:]
            if not val:
                val = args[i] if i < len(args) else ""
                i += 1
            if not _ps_value_ok(c, val):
                return t
            kinds.add(c)
            break
    return letters, kinds, no_headers


def _vet_ps(binary, args):
    """ps: refuse anything that could print process environments (see above)."""
    for t in args:
        if any(c in t for c in "*?["):
            return False, f"ps argument {t!r} contains a glob character"
    if not any("e" in t or "E" in t for t in args):
        return True, ""
    usage = ("could make procps re-read the command BSD-style, where 'e' prints every "
             "process's environment; with an 'e', use dash options only: -A -a -d -e "
             "-f -F -H -l -L -N -T -w, -o FIELDS (not with -f/-F/-l), -p PIDS, "
             "-C NAME, --sort, --forest, --no-headers (or drop the 'e': `ps aux`)")
    parsed = _ps_strict_parse(args)
    if isinstance(parsed, str):
        return _refuse_opt(binary, parsed, usage)
    letters, kinds, no_headers = parsed
    # The conflicts that fail procps's later SysV steps (thread_option_check,
    # process_sf_options) and so also send it to the BSD re-read.
    conflict = (("o" in kinds and letters & set("fFl"))
                or {"L", "T"} <= letters
                or (letters & {"L", "T"} and "H" in letters)
                or no_headers > 1)
    if conflict:
        return False, f"ps options {' '.join(args)!r} conflict, so {usage}"
    return True, ""


# dig reads leading flag letters, then a value letter, so `-4f FILE` is -4 -f.
_DIG_FILE_OPT_RE = re.compile(r"-[46dhimnruv]*[fk]")


def _vet_dig(binary, args):
    """dig: refuse -f (batch file, echoed in errors) and -k (reads a key file),
    also when glued behind flag letters."""
    for t in args:
        if _DIG_FILE_OPT_RE.match(t):
            return _refuse_opt(binary, t, "reads a file")
    return True, ""


# ping runs as root, which may flood (-f), set any interval (-i 0), send
# adaptively (-A), preload (-l) or send 64 KB packets, so options are an
# allowlist: flags, and value options with an upper bound.
_PING_FLAGS = set("46nqvDO")
_PING_LIMITS = {"c": 100, "W": 30, "w": 300, "s": 1472, "t": 255}


def _vet_ping(binary, args):
    """ping: allowlisted flags and bounded -c/-W/-w/-s/-t values only."""
    i = 0
    while i < len(args):
        t = args[i]
        i += 1
        if not t.startswith("-") or t == "-":
            continue
        # Walk a short cluster: flags until a value letter, whose value is the
        # rest of the token or the next token (`-c3`, `-nc 3`).
        for j, c in enumerate(t[1:], 1):
            if c in _PING_FLAGS:
                continue
            if c not in _PING_LIMITS:
                return _refuse_opt(binary, t, "is not allowed in read-only mode (allowed: "
                                   "-4 -6 -n -q -v -D -O, -c N<=100, -W N<=30, "
                                   "-w N<=300, -s N<=1472, -t N<=255)")
            val = t[j + 1:]
            if not val:
                val = args[i] if i < len(args) else ""
                i += 1
            if not val.isdigit() or int(val) > _PING_LIMITS[c]:
                return _refuse_opt(binary, t, f"-{c} needs a number up to {_PING_LIMITS[c]}")
            break
    return True, ""


def _vet_find(binary, args):
    """find: refuse actions that run commands, write files or echo a file."""
    for a in args:
        if a.lower() in _FIND_ACTION_FLAGS:
            return _refuse_opt(binary, a, "executes, writes or reads another file")
    return True, ""


# Allowlisted binaries whose options need checking. The others (ls, stat, df,
# free, uptime, uname, whoami, id, printenv, echo, which, true, test, pgrep,
# top, netstat, nslookup, cat, head, tail) have no option that writes, runs a
# command or reads a file; cat/head/tail paths are checked by _vet_read_operands.
_BINARY_VETTERS = {
    "find": _vet_find, "curl": _vet_curl,
    "docker": _vet_container, "env": _vet_env,
    "systemctl": _vet_systemctl, "service": _vet_service,
    "journalctl": _vet_journalctl, "sort": _vet_sort, "uniq": _vet_uniq,
    "grep": _vet_grep, "egrep": _vet_grep, "ip": _vet_ip, "date": _vet_date,
    "hostname": _vet_hostname, "ss": _vet_ss, "ps": _vet_ps, "du": _vet_files0,
    "wc": _vet_files0, "dig": _vet_dig, "ping": _vet_ping,
}


def vet_shell_command(cmd, allow_mutation=False):
    """Vet a shell command to run ON an LM VM. Returns (ok, reason). In
    read-only mode: no redirects, substitutions or expansions; every
    pipeline/`;`/`&&` segment must START with a read-only binary whose options
    pass its vetter; and content readers may only read allowlisted paths.
    Conservative — anything unrecognised fails."""
    # Refuse empty or non-string commands.
    if not cmd or not isinstance(cmd, str) or not cmd.strip():
        return False, "no shell command provided"
    # With mutation enabled, the service principal's RBAC is the only limit.
    if allow_mutation:
        return True, "mutation allowed (bounded by the service principal's RBAC scope)"
    # Lower-case copy for case-insensitive marker checks.
    low = cmd.lower()
    # Refuse output redirection anywhere in the command.
    for op in _SHELL_MUTATION_OPERATORS:
        if op in low:
            return False, (f"'{op}' redirects output, which is a state change; enable "
                           f"CHAT_AZURE_ALLOW_MUTATION to permit it")
    # Refuse command/process substitution, which can hide a second command.
    for sub in _SHELL_SUBSTITUTION:
        if sub in cmd:
            return False, (f"'{sub}' (command/process substitution) can run an "
                           f"arbitrary nested command; enable CHAT_AZURE_ALLOW_MUTATION "
                           f"to permit it")
    # Refuse variable/ANSI-C expansion and input redirection.
    for ch in _SHELL_EXPANSION_CHARS:
        if ch in cmd:
            return False, (f"'{ch}' (expansion or input redirection) hides what the "
                           f"command really runs or reads; enable "
                           f"CHAT_AZURE_ALLOW_MUTATION to permit it")
    # Refuse unquoted brace expansion and comments.
    for ch in _SHELL_UNQUOTED_CHARS:
        if ch in _unquoted_chars(cmd):
            return False, (f"unquoted '{ch}' changes what bash runs after this check "
                           f"(brace expansion or a comment); quote it")
    # Refuse unquoted globs except plain paths under the open log roots.
    for word in _glob_words(cmd):
        if not _glob_word_ok(word):
            return False, (f"unquoted glob '{word}' could expand into an option or "
                           f"another file; quote it, or glob only under /var/log")
    # Refuse in-place sed edits.
    if _SED_INPLACE_RE.search(low):
        return False, "in-place sed edit (sed -i) is a mutation; enable CHAT_AZURE_ALLOW_MUTATION"
    # Newlines/CRs are command separators the segment split below doesn't
    # know about, so a leading read-only binary would hide whatever follows.
    if "\n" in cmd or "\r" in cmd:
        return False, ("newlines/carriage returns separate commands; enable "
                       "CHAT_AZURE_ALLOW_MUTATION to permit them")
    # A lone `&` backgrounds the command and starts another one after it;
    # only `&&` (already a segment separator below) is not a mutation marker.
    if re.search(r"(?<!&)&(?!&)", cmd):
        return False, ("'&' runs a command in the background and starts another; "
                       "enable CHAT_AZURE_ALLOW_MUTATION to permit it")
    # Split into segments on ; | && || and vet each segment's leading binary.
    segments = re.split(r"\|\||&&|[;|]", cmd)
    # Check every segment of the pipeline/command list.
    for seg in segments:
        # Skip empty segments (e.g. a trailing ";").
        seg = seg.strip()
        if not seg:
            continue
        # Tokenise the segment like a shell; unbalanced quotes mean we refuse.
        try:
            toks = shlex.split(seg)
        except ValueError:
            return False, "could not parse shell command safely"
        if not toks:
            continue
        # The program name, without any directory prefix; a prefix must be a
        # system binary directory.
        binary = os.path.basename(toks[0]).lower()
        if "/" in toks[0] and os.path.dirname(toks[0]) not in _SYSTEM_BIN_DIRS:
            return False, (f"'{toks[0]}' is not a system binary path; use the bare "
                           f"command name")
        # Interpreters can run anything, so they are never allowed in read-only mode.
        if binary in _SHELL_INTERPRETERS:
            return False, (f"'{binary}' is a general-purpose interpreter and can run "
                           f"arbitrary commands regardless of its arguments; enable "
                           f"CHAT_AZURE_ALLOW_MUTATION for arbitrary commands")
        # Known state-changing commands are refused.
        if binary in _SHELL_MUTATION_COMMANDS:
            return False, (f"'{binary}' changes state; enable "
                           f"CHAT_AZURE_ALLOW_MUTATION to permit it")
        # Anything not explicitly allowlisted is refused.
        if binary not in _SHELL_READ_BINARIES:
            return False, (f"'{binary}' is not an allowlisted read-only command; "
                           f"enable CHAT_AZURE_ALLOW_MUTATION for arbitrary commands")
        # Files a content reader prints must be on the read allowlist.
        if binary in _CONTENT_READERS:
            ok, why = _vet_read_operands(binary, toks[1:])
            if not ok:
                return False, why
        # Then the binary's own option checks.
        vetter = _BINARY_VETTERS.get(binary)
        if vetter:
            ok, why = vetter(binary, toks[1:])
            if not ok:
                return False, why
    # Every segment passed.
    return True, "read-only shell command"


def azure_enabled(config):
    """Return True when the operator has enabled chat access to Azure."""
    return bool((config or {}).get("CHAT_AZURE_ENABLED", False))


def _login_helper(config):
    """Return the path of the secretless az login helper (configurable)."""
    return (config or {}).get("AZURE_LOGIN_HELPER") or "/usr/local/bin/lm-az-login"


def _resource_group(config):
    """Return the name of the Azure resource group holding the LM servers (default "LM")."""
    return (config or {}).get("AZURE_LM_RESOURCE_GROUP") or "LM"


def _ensure_login(config, timeout=60):
    """Run the secretless login helper so the subsequent az call has a valid SP
    context. Returns (ok, message). Missing helper (dev host) -> (False, note)."""
    # The helper only exists on LM-AB; elsewhere report that Azure is unavailable.
    helper = _login_helper(config)
    if not os.path.exists(helper):
        return False, (f"Azure login helper not found at {helper} — Azure access is only "
                       f"available from LM-AB, not this host.")
    # Run the helper; timeouts and start-up errors become a failure message.
    try:
        p = subprocess.run([helper], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "az login helper timed out"
    except Exception as e:  # noqa: BLE001
        return False, f"az login helper failed to start: {type(e).__name__}: {e}"
    # Non-zero exit means login failed; include a short piece of its output.
    if p.returncode != 0:
        return False, f"az login helper failed: {(p.stderr or p.stdout or '').strip()[:400]}"
    return True, "authenticated"


def _cap(text, limit=12000):
    """Return text capped at limit characters, marking when it was truncated."""
    # Treat None as empty, then truncate long output so it fits in a chat reply.
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n…[output truncated]"


def run_az(argv, config, timeout=120):
    """Run a vetted `az` control-plane command. `argv` is the arg list AFTER
    'az'. Returns a result dict (never raises)."""
    # Azure access must be switched on first.
    if not azure_enabled(config):
        return {"error": "Azure access is disabled (set CHAT_AZURE_ENABLED to enable)."}
    # Vet the command against the read-only rules unless mutation is allowed.
    allow = bool((config or {}).get("CHAT_AZURE_ALLOW_MUTATION", False))
    ok, reason = vet_az_args(argv, allow_mutation=allow)
    if not ok:
        return {"error": f"refused: {reason}", "argv": list(argv)}
    # Log az in through the helper before running the command.
    ok, msg = _ensure_login(config)
    if not ok:
        return {"error": msg}
    # az binary path (configurable for tests or non-standard installs).
    az = (config or {}).get("AZURE_AZ_BIN") or "az"
    # Run az; report timeouts or a missing az binary as errors.
    try:
        p = subprocess.run([az, *argv], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"az command timed out after {timeout}s", "argv": list(argv)}
    except FileNotFoundError:
        return {"error": f"az binary not found ({az})"}
    # Return the exit code and size-capped stdout/stderr.
    return {"argv": list(argv), "exit_code": p.returncode,
            "stdout": _cap(p.stdout), "stderr": _cap((p.stderr or "").strip(), 2000)}


# AppBuilder runs on LM-AB: resizing it restarts AB in the middle of the run.
NEVER_RESIZE = frozenset({"LM-AB"})


def resize_vm(vm, size, config, timeout=900):
    """Resize one LM VM (Platform Auto Scale's only change). It is a separate
    path from run_az so auto-scale does not need CHAT_AZURE_ALLOW_MUTATION,
    which would open every az command. LM-AB is always refused. Never raises."""
    if not azure_enabled(config):
        return {"error": "Azure access is disabled (set CHAT_AZURE_ENABLED to enable)."}
    vm, size = (vm or "").strip(), (size or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", vm) or not re.fullmatch(r"Standard_[A-Za-z0-9_]{1,40}", size):
        return {"error": f"invalid vm or size: {vm!r} {size!r}"}
    if vm.upper() in NEVER_RESIZE:
        return {"error": f"refused: {vm} is never resized automatically"}
    ok, msg = _ensure_login(config)
    if not ok:
        return {"error": msg}
    az = (config or {}).get("AZURE_AZ_BIN") or "az"
    argv = ["vm", "resize", "-g", _resource_group(config), "-n", vm, "--size", size,
            "--query", "hardwareProfile.vmSize", "-o", "tsv"]
    try:
        p = subprocess.run([az, *argv], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"resize timed out after {timeout}s", "argv": argv}
    except FileNotFoundError:
        return {"error": f"az binary not found ({az})"}
    return {"argv": argv, "exit_code": p.returncode, "stdout": _cap(p.stdout, 2000),
            "stderr": _cap((p.stderr or "").strip(), 2000)}


def run_server_shell(vm, cmd, config, timeout=180):
    """Run a vetted shell command ON one LM VM via `az vm run-command invoke`.
    Returns a result dict (never raises)."""
    # Azure access must be switched on first.
    if not azure_enabled(config):
        return {"error": "Azure access is disabled (set CHAT_AZURE_ENABLED to enable)."}
    # A VM name is required and must look like a plain Azure resource name.
    vm = (vm or "").strip()
    if not vm:
        return {"error": "vm name is required"}
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", vm):
        return {"error": f"invalid vm name: {vm!r}"}
    # Vet the shell command against the read-only rules unless mutation is allowed.
    allow = bool((config or {}).get("CHAT_AZURE_ALLOW_MUTATION", False))
    ok, reason = vet_shell_command(cmd, allow_mutation=allow)
    if not ok:
        return {"error": f"refused: {reason}", "vm": vm}
    # Log az in through the helper before running anything.
    ok, msg = _ensure_login(config)
    if not ok:
        return {"error": msg}
    # az binary and the resource group the VM lives in.
    az = (config or {}).get("AZURE_AZ_BIN") or "az"
    rg = _resource_group(config)
    # In read-only mode, limit the script on the VM too: the local timeout only
    # stops `az`, and a command that never exits (tail -f, ping, docker stats)
    # would hold the VM's run-command slot for about 90 minutes.
    script = cmd if allow else (
        f"timeout -k 5 {max(timeout - 30, 10)} bash -c {shlex.quote(cmd)}")
    # Build the run-command call; the script output comes back as plain text.
    argv = [az, "vm", "run-command", "invoke", "-g", rg, "-n", vm,
            "--command-id", "RunShellScript", "--scripts", script,
            "--query", "value[0].message", "-o", "tsv"]
    # Run it; report timeouts or a missing az binary as errors.
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"run-command timed out after {timeout}s", "vm": vm}
    except FileNotFoundError:
        return {"error": f"az binary not found ({az})"}
    # Return the VM, command, exit code and size-capped output.
    return {"vm": vm, "resource_group": rg, "command": cmd, "exit_code": p.returncode,
            "output": _cap(p.stdout), "stderr": _cap((p.stderr or "").strip(), 2000)}
