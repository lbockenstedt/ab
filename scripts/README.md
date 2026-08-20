# LM automation identity — scripts

These scripts run on **LM-AB** and give AppBuilder's chat agent a *secretless*
way to reach the LM Azure resource group (see `az_console.py`, which shells out
through `lm-az-login`).

## How it works (no secret ever on disk or in git)

1. LM-AB has a system-assigned **managed identity**. `lm-az-login` runs
   `az login --identity`, proving the host is LM-AB.
2. As that identity it downloads the automation service-principal certificate
   from **LM-VAULT** into `/dev/shm` (tmpfs / RAM only, mode `0600`). The vault's
   data-plane firewall only allows the LM subnet, so only LM hosts can do this.
3. It then `az login`s as the service principal **`lm-ab-automation-sp`**
   (role: *Virtual Machine Contributor* on resource group **LM** — console +
   run-command + VM lifecycle, but no power over the vault, networking, or
   storage).
4. `lm-az-logout` clears the az context and wipes the tmpfs cert.

The IDs embedded in `lm-az-login` (app id, tenant id, vault name) are **public
identifiers, not secrets**. The certificate — the only credential — lives solely
in LM-VAULT and, transiently, in tmpfs.

## Install (as root on LM-AB)

```bash
install -m 0755 lm-az-login  /usr/local/bin/lm-az-login
install -m 0755 lm-az-logout /usr/local/bin/lm-az-logout
```

`az_console.py` reads the helper path from config key `AZURE_LOGIN_HELPER`
(default `/usr/local/bin/lm-az-login`) and the resource group from
`AZURE_LM_RESOURCE_GROUP` (default `LM`).
