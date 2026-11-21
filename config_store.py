"""Persistent configuration + state-file storage for AppBuilder.

Extracted verbatim from main.py: the config-path constants, chat-config defaults,
and the load/save helpers for config, processed-issue history, update-recovery
state, the version file, and the startup stamp. Pure move, no behavior change.

main.py keeps the logging setup (get_log_path / logger) and re-exports these
names via ``from config_store import *`` positioned right after `logger` is
defined, so both main's own module-level code and the sibling modules keep
resolving ``from main import load_config / save_processed / CONFIG_DIR`` etc.

The one adaptation required to preserve behavior: write_startup_stamp records the
*main.py* mtime (the watchdog compares it against disk to detect stale-running
code). ``__file__`` here would be config_store.py, so it explicitly reads the
main module's file instead.
"""
# os builds paths and swaps files; json reads/writes the stores; git reads the running
# commit for the startup stamp; datetime timestamps records.
import os
import copy
import json
import threading
import git
from datetime import datetime

# Shared AppBuilder logger (main defines it before importing this module).
from main import logger

# Directory that holds every persistent AppBuilder file.
# Persistent Configuration Paths
CONFIG_DIR = "/etc/ab"
# Main settings file, and the .env file with secrets for the service.
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
ENV_FILE = os.path.join(CONFIG_DIR, ".env")
# Record of every issue AppBuilder has handled and its status.
STATE_FILE = os.path.join(CONFIG_DIR, "processed_issues.json")
# Saved PR pre-review results shown on the "PRs Reviewed" page.
PR_REVIEWS_FILE = os.path.join(CONFIG_DIR, "pr_reviews.json")
# Lifetime merge ledger. pr_reviews.json is a ring buffer capped at
# _PR_REVIEWS_MAX, so counting merged PRs by scanning it yields "merged among
# the last N reviewed" — a number that goes DOWN whenever an older merged
# record is evicted to make room. This file is never trimmed.
PR_MERGE_LEDGER_FILE = os.path.join(CONFIG_DIR, "pr_merge_ledger.json")
# Update-recovery state, self-log scan position, chat conversations, and tok/s samples.
UPDATE_STATE_FILE = os.path.join(CONFIG_DIR, "update_state.json")
SELF_SCAN_OFFSET_FILE = os.path.join(CONFIG_DIR, "self_scan_offset.json")
CHAT_HISTORY_FILE = os.path.join(CONFIG_DIR, "chat_history.json")
LLM_TPS_FILE = os.path.join(CONFIG_DIR, "llm_tps.json")
#: llm_perf.py's v2 store (latency + tok/s, ModelKey-keyed) — a separate file
#: from LLM_TPS_FILE, not a migration of it (llm_tps.json's bare model-name
#: keys can't be safely mapped to a 3-tuple ModelKey; see llm_perf.load()'s
#: docstring). llm_perf.py owns its own load()/save(), so no wrapper
#: functions live here — this constant just keeps the path centralized with
#: every other state file, per this module's existing convention.
LLM_PERF_FILE = os.path.join(CONFIG_DIR, "llm_perf.json")
# VERSION file in the working directory (the deployed checkout).
VERSION_FILE = os.path.join(os.getcwd(), "VERSION")

# Chat-agent configuration defaults. Applied (without overriding user values) by
# load_config() so every code path sees a fully-populated config, and persisted by
# save_settings when the user edits them on the Settings page.
CHAT_CONFIG_DEFAULTS = {
    "CHAT_TOOLS_ENABLED": True,        # Master switch; False -> chat runs the legacy single-turn path.
    "CHAT_TOOL_MAX_ITERATIONS": 6,     # Cap on tool-call <-> LLM round trips per turn.
    "CHAT_TOOL_MAX_TOKENS": 12000,     # Soft cap on cumulative tool-result text appended in a turn.
    "CHAT_INDEX_ISSUE_LIMIT": 8,       # Max open issues listed per repo in the system-prompt index.
    "CHAT_INDEX_CACHE_TTL": 60,        # Seconds to cache the GitHub issue index across turns.
    "CHAT_FIX_PROPOSAL_TTL": 600,      # Seconds a fix-proposal confirmation token stays valid.
    # Console account selector for profiling. List of usernames from the vault
    # that can be used for console profiling (auto-identify, credential login).
    "console_accounts": [],             # List of username strings selected for console
    # AI fix-generation context bounds. apply_ai_fix used to concatenate every
    # relevant file in full, which blew past provider limits (groq 413 Payload
    # Too Large, ollama "Response ended prematurely", then truncated JSON →
    # "unmatched '}'"). These bound the prompt so the returned fix JSON stays
    # complete and parseable.
    "FIX_MAX_FILES": 5,                # Max relevant files included in one fix prompt.
    "FIX_MAX_FILE_CHARS": 12000,       # Max chars per file in the prompt (truncated past this).
    "FIX_MAX_CONTEXT_CHARS": 60000,   # Max total chars across all files in one prompt.
    "FIX_MAX_OUTPUT_TOKENS": 8192,     # max_tokens sent on OpenAI-compatible fix requests (output headroom).
    # Per-module heartbeat triage. scan_heartbeats reads the raw Hub logs and
    # files/reopens an issue when an expected module's [heartbeat] line is
    # missing or older than HEARTBEAT_STALE_S. heartbeat_exclude is a list of
    # spoke_ids and/or module_types to never triage (e.g. an undeployed spoke).
    "HEARTBEAT_STALE_S": 300,          # Max age (seconds) of a heartbeat line before triage.
    "heartbeat_exclude": [],           # spoke_ids / module_types to skip (list).
    # The AI_SPEND_GATE knob: the daily gate on paid AI spend (fleet rule 1.10,
    # ai_spend.py). Off = no gate; spend is still counted.
    "AI_SPEND_GATE": True,
    # API key required by the Anthropic-compatible LLM-router proxy (/v1/*, see
    # llm_proxy.py). Empty = proxy is open (a warning is logged per request).
    # The AB_PROXY_KEY env var overrides this when set.
    "llm_proxy_api_key": "",
}


def _atomic_write_json(path, data, *, indent=2, chmod=None):
    """Write `data` as JSON to `path` atomically, creating its directory first.

    Two separate hazards, one helper, because every writer here needs both:

    * ENOENT. Only save_config ever called os.makedirs(CONFIG_DIR); the other
      four writers opened paths under /etc/ab directly, so whenever that
      directory was absent each one failed with
      "[Errno 2] No such file or directory" — and because those failures are
      logged at ERROR, the self-log scanner filed a fresh issue per occurrence
      (twelve of them before this was tracked down).
    * Truncation. open(path, "w") truncates before writing, so a crash in that
      window leaves an empty file that the next load cannot parse. Writing to a
      temp file and swapping with os.replace (atomic on POSIX) means readers
      only ever see the fully-written old or new file.

    Raises on failure after removing the temp file: callers own their own
    fallback and logging, so the error must still reach them.
    """
    import tempfile

    # Each writer owns its temp file: a shared path lets concurrent saves
    # rename or remove each other's file, causing ENOENT even with a parent dir.
    tmp = None
    try:
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)
        # Keep the unique temp on the same filesystem for atomic replacement.
        with tempfile.NamedTemporaryFile(
                mode="w", dir=parent, prefix=os.path.basename(path) + ".",
                suffix=".tmp", delete=False) as f:
            tmp = f.name
            json.dump(data, f, indent=indent)
        # Optionally restrict permissions (e.g. 0600 for files holding secrets).
        if chmod is not None:
            try:
                os.chmod(tmp, chmod)
            except Exception:
                pass
        # Atomically swap the finished file into place.
        os.replace(tmp, path)
    # Clean up only this writer's temp file, then let the caller handle the error.
    except Exception:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


class ConfigUnreadableError(RuntimeError):
    """CONFIG_FILE exists with content AB cannot read or parse.

    Saving defaults over such a file destroys settings and credentials an
    operator could still recover (a file left root-owned by a maintenance
    script, or a hand-edit with a syntax error). On 2026-11-17 exactly that
    wiped all 14 LLM endpoints. So load_config keeps the last good config and
    save_config refuses to write until the file is readable again.
    """


class ConfigConflictError(RuntimeError):
    """A settings snapshot no longer matches the current config."""


# Serialize read-modify-write transactions and legacy full-snapshot writes.
_config_lock = threading.RLock()
# Last config that loaded cleanly from CONFIG_FILE; served while the file is unreadable.
_last_good_config = None
# The unreadable-file error already logged, so the 5-second heartbeat poll logs it once.
_unreadable_logged = None


def _read_persistent_config():
    """Return the parsed CONFIG_FILE, or None when it is missing or empty.

    An empty file holds nothing to lose (the old non-atomic writer left them
    after a crash), so it counts as missing. Anything else AB cannot read or
    parse raises ConfigUnreadableError rather than looking like an empty config.
    """
    try:
        with open(CONFIG_FILE, "r") as f:
            if not f.read(1):
                return None
            f.seek(0)
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ConfigUnreadableError(f"{CONFIG_FILE}: {e}") from e


def _apply_config_defaults(config):
    """Fill missing defaults without sharing mutable values across snapshots."""
    config.setdefault("enabled_models", [])
    # Apply chat-agent defaults for any keys the stored config does not set, so
    # every caller (chat agent loop, index builder, settings form) sees a complete
    # config regardless of how old the persisted config.json is.
    for _k, _v in CHAT_CONFIG_DEFAULTS.items():
        if _k not in config:
            config[_k] = copy.deepcopy(_v)
    return config


def _remember_good_config(config):
    """Keep an independent, defaulted copy of a config that is on disk."""
    global _last_good_config
    _last_good_config = _apply_config_defaults(copy.deepcopy(config))


def save_config(config):
    """Saves configuration to persistent storage, falling back to local if needed.

    Raises ConfigUnreadableError, writing nothing, while an existing CONFIG_FILE
    cannot be read: `config` may then be the built-in defaults, and replacing the
    stored file with them is unrecoverable. It also raises the underlying error
    when neither CONFIG_FILE nor the local fallback could be written (full disk,
    read-only mount). The local fallback is used only when no CONFIG_FILE exists:
    load_config reads an existing CONFIG_FILE first, so a copy saved beside it would
    be ignored, and its failure raises as well. All are raised rather than logged so
    a settings save reports the failure instead of a false success.

    Written via a temp file + os.replace (same pattern as save_llm_tps): a
    process death mid-write with a direct open(...,"w") truncates the file
    first and writes second, so a crash/restart in that window leaves an
    empty/partial CONFIG_FILE. The next load_config() then fails to parse it
    ("Expecting value: line 1 column 1 (char 0)") and silently falls back to
    stale/default config, losing whatever was persisted. os.replace is atomic
    on POSIX, so readers only ever see the fully-written old or new file.
    """
    with _config_lock:
        _save_config(config, _read_persistent_config())


def _save_config(config, existing):
    """Write under the caller's lock, using the validated primary to gate fallback."""
    try:
        # No pre-flight existence/access check: os.access(CONFIG_DIR, W_OK) is
        # False for a directory that merely does not exist yet, so the old guard
        # sent a first-boot host down the cwd-fallback branch and left config in
        # a throwaway ./config.json instead of creating /etc/ab. Just attempt the
        # write -- _atomic_write_json creates the directory, and a genuine
        # permission failure still raises and still lands in the fallback below.
        _atomic_write_json(CONFIG_FILE, config, chmod=0o600)
    # Persistent location failed: fall back to ./config.json in the working directory.
    except Exception as e:
        if existing is not None:
            # load_config prefers a readable CONFIG_FILE, so a ./config.json written now
            # would never be read: raise instead of reporting a save that has no effect.
            logger.error(f"Could not save to {CONFIG_FILE} ({e}); the existing file stays in use")
            raise
        logger.warning(f"Could not save to persistent storage ({e}), falling back to local config.json")
        try:
            _atomic_write_json("config.json", config)
        except Exception as fe:
            logger.error(f"Critical failure saving config: {fe}")
            # Nothing was stored anywhere: raise so a caller never reports a false success
            # (a settings save returns 500, the Copilot sign-in ends in an error status).
            raise
        # load_config reads ./config.json when there is no CONFIG_FILE and treats it as the
        # last good config, so the one just written replaces an older load of it.
        _remember_good_config(config)
    else:
        # The file now holds this config: it, not an older load, is the last good one
        # to serve if the file becomes unreadable before the next load_config.
        _remember_good_config(config)
        logger.info(f"Config saved to persistent storage: {CONFIG_FILE}")


def update_config(mutator, *, expected=None):
    """Apply a synchronous, in-memory edit to the latest config and return its result.

    The settings form can supply its original defaulted view as ``expected`` to
    reject stale whole-form edits. A callback runs once, only after validation,
    and must not perform I/O or suspend. Unchanged config is not written.
    """
    with _config_lock:
        current, _ = _config_view(_read_persistent_config())
        if expected is not None and current != expected:
            raise ConfigConflictError("Settings changed while saving. Reload the Settings page and retry.")
        edited = copy.deepcopy(current)
        result = mutator(edited)
        if edited != current:
            save_config(edited)
        return result


def load_config():
    """Load settings from /etc/ab/config.json, else ./config.json, else built-in defaults.

    When /etc/ab/config.json exists but cannot be read, the last good config is
    returned (logged once at ERROR); with none yet (a fresh start) the fallbacks
    below apply, but save_config still refuses to overwrite the file.
    """
    global _unreadable_logged
    # Try persistent config first
    config = None
    try:
        config = _read_persistent_config()
        if _unreadable_logged:
            # Only a parsed object is a recovery; a file that was deleted or emptied
            # is a different state (defaults load, as for any missing config).
            if config is None:
                logger.info(f"Persistent config {CONFIG_FILE} is no longer unreadable, but it is "
                            "now missing or empty; the fallback config is in use")
            else:
                logger.info(f"Persistent config {CONFIG_FILE} is readable again")
            _unreadable_logged = None
    except ConfigUnreadableError as e:
        if _unreadable_logged != str(e):
            logger.error(f"Persistent config is unreadable; not using defaults and not "
                         f"overwriting it until it is fixed: {e}")
            _unreadable_logged = str(e)
        if _last_good_config is not None:
            return copy.deepcopy(_last_good_config)
    config, from_file = _config_view(config)
    # Only a config read from disk replaces the last good one, not built-in defaults.
    if from_file:
        _remember_good_config(config)
    return config


def _config_view(config):
    """Default the primary, or use the cwd file and then built-in defaults."""
    from_file = config is not None
    if config is None:
        try:
            with open("config.json", "r") as f:
                config = json.load(f)
                if "enabled_models" not in config:
                    config["enabled_models"] = []
            from_file = True
        except Exception:
            config = {
                "monitored_repos": [],
                "trusted_repos": [],
                "default_branch": "main",
                "direct_push_enabled": False,
                "dev_branch": "dev",
                "repo_tests": {},
                "GITHUB_TOKEN": "",
                "monitored_labels": ["automated-fix"],
                "enabled_models": [],
                "self_diagnosis_repo": ""
            }
    return _apply_config_defaults(config), from_file

class ProcessedStoreError(RuntimeError):
    """History persistence failed; never retry a fix or report success for this."""


class ProcessedUnreadableError(ProcessedStoreError):
    """The authoritative history exists but cannot safely be read."""


# Single-interpreter coordination, not a process-shared/file lock. The application
# layer holds this same RLock through committed feed/counter publication.
_processed_lock = threading.RLock()


def _read_processed_file(path):
    """Read one history, preserving the legacy list-of-issue-ids conversion."""
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            data = {issue_id: {"status": "fixed", "timestamp": datetime.now().isoformat()}
                    for issue_id in data}
        _validate_processed(data)
        return data
    except FileNotFoundError:
        raise
    except (OSError, ValueError, TypeError) as e:
        raise ProcessedUnreadableError(f"{path}: {e}") from e


def _validate_processed(data):
    """Require records, not mandatory fields/statuses; older records remain valid."""
    if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
        raise ValueError("expected an issue-history object containing record objects")


def _read_processed_authority():
    """Return (fresh history, primary exists); only absence permits cwd authority."""
    try:
        return _read_processed_file(STATE_FILE), True
    except FileNotFoundError:
        try:
            return _read_processed_file("processed_issues.json"), False
        except FileNotFoundError:
            return {}, False


def load_processed():
    """Return a detached advisory snapshot; degraded reads never authorize writes."""
    with _processed_lock:
        for path in (STATE_FILE, "processed_issues.json"):
            try:
                return _read_processed_file(path)
            except FileNotFoundError:
                continue
            except ProcessedStoreError as e:
                logger.error(f"Could not read processed history (snapshot only): {e}")
        return {}


def _write_processed(processed, primary_exists):
    """Publish validated history, never falling back behind an existing primary."""
    try:
        _validate_processed(processed)
        # Validate before attempting either destination, including for no-op/raw saves.
        json.dumps(processed)
        try:
            _atomic_write_json(STATE_FILE, processed)
        except OSError:
            if primary_exists:
                raise
            _atomic_write_json("processed_issues.json", processed)
    except Exception as e:
        raise ProcessedStoreError(f"Could not publish processed history: {e}") from e


def _update_processed(mutator):
    """Mutate fresh authority once; return its committed snapshot and callback result.

    Callbacks do local dictionary work only: no network, application-state changes
    or nested transactions. RLock reentrancy is for the application/storage layers.
    """
    with _processed_lock:
        current, primary_exists = _read_processed_authority()
        before = copy.deepcopy(current)
        result = mutator(current)
        try:
            _validate_processed(current)
            json.dumps(current)
        except (ValueError, TypeError) as e:
            raise ProcessedStoreError(f"Invalid processed history mutation: {e}") from e
        if current != before:
            _write_processed(current, primary_exists)
        return current, result


def save_processed(processed):
    """Legacy explicit replacement, not a safe stale-snapshot update API.

    Production writers use app_state.update_processed. Even this compatibility
    writer refuses to overwrite an unreadable primary OR cwd-only authority.
    """
    with _processed_lock:
        _, primary_exists = _read_processed_authority()
        _write_processed(processed, primary_exists)

def load_pr_reviews():
    """Load the persisted PR pre-review store (keyed 'repo#number').

    pr_reviews used to be in-memory only, so it reset to {} on every restart.
    Terminal PRs (merged/denied) are closed and never re-scanned, so after a
    restart they vanished from the list entirely; approved-but-open PRs lost
    their APPROVED badge. Persisting the store fixes that."""
    # Read the file if present; only a dict is accepted.
    if os.path.exists(PR_REVIEWS_FILE):
        try:
            with open(PR_REVIEWS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        # A bad file is logged and treated as empty.
        except Exception as e:
            logger.error(f"Error loading PR reviews from {PR_REVIEWS_FILE}: {e}")
    # No saved reviews.
    return {}


def save_pr_reviews(pr_reviews):
    """Persist the PR pre-review store so merged/denied/approved survive restarts.

    Written via a temp file + os.replace (see save_config) so a crash mid-write
    can't leave a truncated/empty PR_REVIEWS_FILE for the next load to choke on.
    Errors propagate: callers must not treat a failed approval write as success.
    """
    # Log and propagate failure to the review-state caller.
    try:
        _atomic_write_json(PR_REVIEWS_FILE, pr_reviews)
    except Exception as e:
        logger.error(f"Error saving PR reviews to {PR_REVIEWS_FILE}: {e}")
        raise


def load_pr_merge_ledger():
    """Load the lifetime merge ledger: ``{"merged": [key...],
    "auto_merged": [key...]}`` where key is the usual ``'repo#number'``.

    Sets of KEYS rather than running integers, for the same reason the issue
    counters are derived from the processed store: an increment drifts the
    moment anything is recorded twice, and PR records are rewritten on every
    poll. Re-recording a key that is already present is a no-op, so the tally
    is idempotent by construction and can only ever go up.
    """
    # Read the file if present; keep only the two known keys whose values are lists,
    # converting each list to a set.
    if os.path.exists(PR_MERGE_LEDGER_FILE):
        try:
            with open(PR_MERGE_LEDGER_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: set(v) for k, v in data.items()
                        if k in ("merged", "auto_merged") and isinstance(v, list)}
        # A bad file is logged and treated as empty.
        except Exception as e:
            logger.error(f"Error loading PR merge ledger from {PR_MERGE_LEDGER_FILE}: {e}")
    return {}


def save_pr_merge_ledger(ledger):
    """Persist the lifetime merge ledger. Sets are stored as sorted lists so the
    file is stable/diffable and does not churn on every write."""
    # Atomic write of sorted lists; failures are logged, not raised.
    try:
        _atomic_write_json(PR_MERGE_LEDGER_FILE,
                           {k: sorted(v) for k, v in (ledger or {}).items()})
    except Exception as e:
        logger.error(f"Error saving PR merge ledger to {PR_MERGE_LEDGER_FILE}: {e}")


def load_llm_tps():
    """Warm-load the per-model tok/s samples measured before the last restart.

    Without this the Model Performance panel is empty after every restart and
    stays empty until each model happens to run again -- which, for a model used
    by one provider slot on an occasional task, can be hours. An operator
    comparing models then sees a blank table and reasonably concludes the feature
    is broken rather than merely cold.

    Shape is {model: [tps, ...]}. Anything else on disk is discarded rather than
    trusted: a malformed entry would poison the averages, and the cost of
    dropping it is only that the numbers rebuild.
    """
    # No saved samples yet.
    if not os.path.exists(LLM_TPS_FILE):
        return {}
    # Read the file; any error is logged and treated as empty.
    try:
        with open(LLM_TPS_FILE, "r") as f:
            data = json.load(f)
        # Only a {model: [numbers]} dict is accepted.
        if not isinstance(data, dict):
            return {}
        out = {}
        # Keep only positive, plausible numbers per model, and only the last 20.
        for model, samples in data.items():
            if not isinstance(samples, list):
                continue
            clean = [float(x) for x in samples
                     if isinstance(x, (int, float)) and 0 < float(x) < 100000]
            if clean:
                out[str(model)] = clean[-20:]
        return out
    except Exception as e:
        logger.error(f"Error loading LLM tok/s cache from {LLM_TPS_FILE}: {e}")
        return {}


def save_llm_tps(llm_tps):
    """Persist the per-model tok/s samples.

    Written via a temp file + os.replace: this is called from the LLM path, so a
    process death mid-write must not leave a truncated JSON that load_llm_tps
    then discards -- losing the very history the cache exists to keep.
    """
    # Compact (no indent) atomic write; failures are logged, not raised.
    try:
        _atomic_write_json(LLM_TPS_FILE, llm_tps, indent=None)
    except Exception as e:
        logger.error(f"Error saving LLM tok/s cache to {LLM_TPS_FILE}: {e}")


def load_update_state():
    """Loads the update state for recovery."""
    # Read the recovery state if the file exists; a bad file falls through to the default.
    if os.path.exists(UPDATE_STATE_FILE):
        try:
            with open(UPDATE_STATE_FILE, "r") as f:
                return json.load(f)
        except: pass
    # Default: no known-good commit and no failed commits.
    return {"last_known_good_commit": None, "failed_commits": []}

def save_update_state(state):
    """Saves the update state for recovery.

    Written via a temp file + os.replace (see save_config) so a crash mid-write
    can't leave a truncated/empty UPDATE_STATE_FILE for the next load to choke
    on -- this file specifically backs update-recovery, so a corrupt read here
    would defeat the recovery mechanism it exists for.
    """
    # Atomic write; failures are logged, not raised.
    try:
        _atomic_write_json(UPDATE_STATE_FILE, state)
    except Exception as e:
        logger.error(f"Error saving update state: {e}")


def get_version():
    """Return the contents of the VERSION file, or "Unknown" if it cannot be read."""
    # Read and trim the version string; any error gives "Unknown".
    try:
        with open(VERSION_FILE, "r") as f: return f.read().strip()
    except: return "Unknown"

# File recording which commit/process is running, read by the watchdog.
STARTUP_STAMP_FILE = os.path.join(CONFIG_DIR, "startup_stamp.json")

def write_startup_stamp():
    """Record which commit this process actually booted on, plus start time / pid /
    main.py mtime. The watchdog reads this to detect stale-running code (disk updated
    but the process never restarted) and force a restart to load the pending update.
    Also drives the Diagnostics panel's running-vs-disk version comparison."""
    # Never let a stamp failure stop startup; it is only logged.
    try:
        import main  # for main.__file__ — the stamp records main.py's mtime, not config_store.py's
        # Current git commit of the working directory, or "unknown".
        commit = "unknown"
        try:
            commit = git.Repo(os.getcwd()).head.commit.hexsha
        except Exception as ge:
            logger.warning(f"Startup stamp: could not read git commit: {ge}")
        # Commit, version, start time, process id and main.py's modification time.
        stamp = {
            "commit": commit,
            "version": get_version(),
            "started_at": datetime.now().isoformat(),
            "pid": os.getpid(),
            "main_mtime": os.path.getmtime(main.__file__),
        }
        # Write the stamp atomically and log a short summary.
        _atomic_write_json(STARTUP_STAMP_FILE, stamp)
        logger.info(f"Startup stamp written: commit={commit[:7] if commit != 'unknown' else 'unknown'} pid={os.getpid()}")
    except Exception as e:
        logger.warning(f"Could not write startup stamp: {e}")


# Names re-exported by main via "from config_store import *".
__all__ = [
    "_atomic_write_json",
    "load_llm_tps", "save_llm_tps", "LLM_TPS_FILE", "LLM_PERF_FILE",

    "CONFIG_DIR",
    "CONFIG_FILE",
    "ENV_FILE",
    "STATE_FILE",
    "PR_REVIEWS_FILE",
    "PR_MERGE_LEDGER_FILE",
    "UPDATE_STATE_FILE",
    "SELF_SCAN_OFFSET_FILE",
    "CHAT_HISTORY_FILE",
    "VERSION_FILE",
    "CHAT_CONFIG_DEFAULTS",
    "STARTUP_STAMP_FILE",
    "save_config",
    "update_config",
    "ConfigConflictError",
    "load_config",
    "load_processed",
    "save_processed",
    "ProcessedStoreError",
    "ProcessedUnreadableError",
    "load_pr_reviews",
    "save_pr_reviews",
    "load_pr_merge_ledger",
    "save_pr_merge_ledger",
    "load_update_state",
    "save_update_state",
    "get_version",
    "write_startup_stamp",
]
