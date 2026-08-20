#!/usr/bin/env python3
"""Self-test for az_console — the chat→LM-fleet Azure bridge. The security-
critical surface is the DEFAULT-DENY vetting (vet_az_args / vet_shell_command):
in read-only mode nothing that mutates may pass, and mutation is only allowed
when the operator has explicitly opted in. az_console is import-light (no app
init), so this imports it directly and exercises the pure vetting functions
plus the config gate. No `az` binary is required — run_az/run_server_shell are
tested only for their fail-closed guards (disabled / missing helper), never for
a real cloud call.

Run:  python3 test_az_console.py
"""
import sys

import az_console as azc


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


def main():
    ok = True

    # ── vet_az_args: read-only mode ─────────────────────────────────────────
    ok &= _check("`vm list` is allowed read-only", azc.vet_az_args(["vm", "list", "-g", "LM"])[0] is True)
    ok &= _check("`vm get-instance-view` allowed", azc.vet_az_args(["vm", "get-instance-view", "-g", "LM", "-n", "LM-AB"])[0] is True)
    ok &= _check("`resource list` allowed", azc.vet_az_args(["resource", "list"])[0] is True)
    ok &= _check("`vm delete` BLOCKED in read-only", azc.vet_az_args(["vm", "delete", "-n", "LM-AB"])[0] is False)
    ok &= _check("`vm create` BLOCKED in read-only", azc.vet_az_args(["vm", "create", "-n", "x"])[0] is False)
    ok &= _check("`vm start` BLOCKED in read-only", azc.vet_az_args(["vm", "start", "-n", "x"])[0] is False)
    ok &= _check("`vm run-command invoke` BLOCKED in read-only (must use server_shell)",
                 azc.vet_az_args(["vm", "run-command", "invoke", "-n", "x"])[0] is False)
    ok &= _check("unknown group BLOCKED in read-only (fail closed)",
                 azc.vet_az_args(["keyvault", "secret", "show"])[0] is False)
    ok &= _check("non-string arg BLOCKED", azc.vet_az_args(["vm", 3])[0] is False)
    ok &= _check("empty argv BLOCKED", azc.vet_az_args([])[0] is False)

    # ── vet_az_args: mutation opt-in ────────────────────────────────────────
    ok &= _check("`vm delete` ALLOWED when allow_mutation", azc.vet_az_args(["vm", "delete"], allow_mutation=True)[0] is True)
    ok &= _check("`vm run-command invoke` ALLOWED when allow_mutation",
                 azc.vet_az_args(["vm", "run-command", "invoke"], allow_mutation=True)[0] is True)

    # ── vet_shell_command: read-only mode ───────────────────────────────────
    ok &= _check("`systemctl status ab` allowed", azc.vet_shell_command("systemctl status ab")[0] is True)
    ok &= _check("`journalctl -u ab -n 100 --no-pager` allowed",
                 azc.vet_shell_command("journalctl -u ab -n 100 --no-pager")[0] is True)
    ok &= _check("`curl -s localhost/health` allowed", azc.vet_shell_command("curl -s localhost/health")[0] is True)
    ok &= _check("piped read `journalctl -u ab | grep ERROR` allowed",
                 azc.vet_shell_command("journalctl -u ab | grep ERROR")[0] is True)
    ok &= _check("`systemctl restart ab` BLOCKED (not a read subcommand)",
                 azc.vet_shell_command("systemctl restart ab")[0] is False)
    ok &= _check("`rm -rf /opt/ab` BLOCKED", azc.vet_shell_command("rm -rf /opt/ab")[0] is False)
    ok &= _check("redirect `echo x > /etc/hosts` BLOCKED", azc.vet_shell_command("echo x > /etc/hosts")[0] is False)
    ok &= _check("`sed -i s/a/b/ f` (in-place) BLOCKED", azc.vet_shell_command("sed -i s/a/b/ f")[0] is False)
    ok &= _check("`apt install nginx` BLOCKED", azc.vet_shell_command("apt install nginx")[0] is False)
    ok &= _check("`git pull` BLOCKED", azc.vet_shell_command("git pull")[0] is False)
    ok &= _check("chained read;mutation `ls; rm x` BLOCKED (mutation marker)",
                 azc.vet_shell_command("ls; rm x")[0] is False)
    ok &= _check("chained reads `ls; ps aux` allowed", azc.vet_shell_command("ls; ps aux")[0] is True)
    ok &= _check("unknown binary `mysqldump` BLOCKED (fail closed)",
                 azc.vet_shell_command("mysqldump db")[0] is False)
    ok &= _check("empty command BLOCKED", azc.vet_shell_command("")[0] is False)

    # ── vet_shell_command: mutation opt-in ──────────────────────────────────
    ok &= _check("`systemctl restart ab` ALLOWED when allow_mutation",
                 azc.vet_shell_command("systemctl restart ab", allow_mutation=True)[0] is True)
    ok &= _check("`rm -rf x` ALLOWED when allow_mutation (RBAC is the boundary)",
                 azc.vet_shell_command("rm -rf x", allow_mutation=True)[0] is True)

    # ── config gate (no az needed) ──────────────────────────────────────────
    ok &= _check("run_az disabled when CHAT_AZURE_ENABLED off",
                 "disabled" in azc.run_az(["vm", "list"], {"CHAT_AZURE_ENABLED": False}).get("error", ""))
    ok &= _check("run_server_shell disabled when CHAT_AZURE_ENABLED off",
                 "disabled" in azc.run_server_shell("LM-AB", "systemctl status ab", {"CHAT_AZURE_ENABLED": False}).get("error", ""))
    ok &= _check("run_az refuses a mutating command BEFORE any login/exec",
                 "refused" in azc.run_az(["vm", "delete", "-n", "x"], {"CHAT_AZURE_ENABLED": True}).get("error", ""))
    ok &= _check("run_server_shell rejects a bad vm name",
                 "invalid vm name" in azc.run_server_shell("bad name!", "systemctl status ab", {"CHAT_AZURE_ENABLED": True}).get("error", ""))
    ok &= _check("run_az reports missing helper on a non-LM host (fail closed)",
                 "helper not found" in azc.run_az(["vm", "list"],
                     {"CHAT_AZURE_ENABLED": True, "AZURE_LOGIN_HELPER": "/nonexistent/lm-az-login"}).get("error", ""))

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running az_console self-test...")
    sys.exit(main())
