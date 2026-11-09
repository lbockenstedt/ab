# Fleet code rules

These are the rules every change to the LM fleet must follow. They come from the owner's working sessions. Breaking one of them is a defect, even when the code works.

1. **Readable and small.** Use the fewest lines that stay clear, and plain Python wherever possible. When code repeats, move it into a shared helper.
2. **Documented.** Every Python function has a docstring, and every JS function a `//` comment above it. A function longer than about 15 lines has short step comments. A new route, WebUI page or form field comes with a docs/*.md (or README / MODULE_REQUIREMENTS.md) update.
3. **Few dependencies.** Don't add packages unless there's no reasonable alternative. There is no npm or Node build; the WebUI is plain JavaScript.
4. **JSON storage only.** Store state in plain JSON files. No SQLite, ORM, YAML, XML or CSV storage.
5. **Nothing hard-coded.** No lab hostnames, IPs, tenants, sites or vendor lists. Read them from config or discover them.
6. **Generic, not vendor-specific.** Code must work for the whole class of device, e.g. any BMC (iLO, iDRAC, …), not one model.
7. **Credentials go in the vault.** When a vault is configured (any provider), every password, API key and token is stored in the Credential Vault. A record keeps only a `vault_credential {bucket, name}` reference. With no vault, secrets are Fernet-encrypted on disk.
   - Wire every new secret field through `instance_vault` (`SECRET_FIELDS`, `validate_ref`, `strip_inline_secrets`, `overlay`).
   - Give the WebUI field a vault picker (`vaultPicker`, `vaultRequired`, `vaultSecret`).
   - List routes return only an `*_set` flag, never the secret.
   - See `lm/MODULE_REQUIREMENTS.md`.
8. **Tenant-scoped.** Every page and API route is scoped to the caller's tenant, or explicitly guarded as admin-only.
   - A spoke assigned to a tenant hides the shared instance.
   - Never show UUIDs in the UI; show human names. UUIDs belong only on diagnostic pages.
9. **Safe outbound URLs.** Every outbound URL setting passes `safe_external_url` when it's saved.
10. **The spoke does the heavy lifting.** Spokes do the work; the hub relays and stores. Hub↔spoke wire changes stay backward-compatible, using push, ack and retry with atomic rollback.
11. **Reversible and gentle.**
    - A change can be backed out.
    - Prefer letting existing processes pick up the change on their next run over forcing it.
    - Customisations survive a reinstall.
12. **Twins stay in step.** When a file has a twin (hub↔spoke copies, linux↔windows scripts, pipeline files), change both copies the same way.
13. **VERSION is bot-owned.** Never edit `VERSION` files; the promotion workflow bumps them. Branch flow is dev → qa → main.
14. **Vendor responses are untrusted.**
    - Check the HTTP status and the body, e.g. `result: failed` inside a 200.
    - Reconnect when a session expires.
    - Make anything you push JSON-safe (datetimes, sets, bytes).
15. **Sync is idempotent.**
    - Before creating, look the record up by its natural key (serial, MAC, primary IP, name in the same site).
    - Never match across tenants.
    - Don't skip a repair pass just because the record looks unchanged.
16. **Don't block the event loop.**
    - In `async def`, wrap sleeps, subprocesses, sync HTTP and file I/O in `asyncio.to_thread`.
    - Bound fan-out with a semaphore or queue.
    - Never start a new poll while the last one is still running.
17. **WebUI hygiene.**
    - Cancel or ignore stale async renders when the user navigates away.
    - Build markup with `safeHtml`; wire events with `data-action` and `delegate()`.
    - No new `escapeHtml(`, inline `on*=` handlers or `window.x =` globals; the ratchet test fails on any increase.
18. **Never log secrets.** Log whether a secret is set, or mask it. This includes secrets nested in command payloads.
19. **Share, don't copy.**
    - Call or move the existing helper instead of pasting it.
    - When you replace code, delete the old path. Leave no dead functions behind.
