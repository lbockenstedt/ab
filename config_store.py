"""Persistent configuration + state-file storage for BugFixer.

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
import os
import json
import git
from datetime import datetime

from main import logger

# Persistent Configuration Paths
CONFIG_DIR = "/etc/bugfixer"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
ENV_FILE = os.path.join(CONFIG_DIR, ".env")
STATE_FILE = os.path.join(CONFIG_DIR, "processed_issues.json")
PR_REVIEWS_FILE = os.path.join(CONFIG_DIR, "pr_reviews.json")
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
}


def save_config(config):
    """Saves configuration to persistent storage, falling back to local if needed.

    Written via a temp file + os.replace (same pattern as save_llm_tps): a
    process death mid-write with a direct open(...,"w") truncates the file
    first and writes second, so a crash/restart in that window leaves an
    empty/partial CONFIG_FILE. The next load_config() then fails to parse it
    ("Expecting value: line 1 column 1 (char 0)") and silently falls back to
    stale/default config, losing whatever was persisted. os.replace is atomic
    on POSIX, so readers only ever see the fully-written old or new file.
    """
    try:
        if os.path.exists(CONFIG_DIR) or os.access(CONFIG_DIR, os.W_OK):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(config, f, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except Exception:
                pass
            os.replace(tmp, CONFIG_FILE)
            logger.info(f"Config saved to persistent storage: {CONFIG_FILE}")
        else:
            raise IOError("Persistent config directory not writable")
    except Exception as e:
        logger.warning(f"Could not save to persistent storage ({e}), falling back to local config.json")
        try:
            tmp = "config.json.tmp"
            with open(tmp, "w") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp, "config.json")
        except Exception as fe:
            logger.error(f"Critical failure saving config: {fe}")

def load_config():
    # Try persistent config first
    config = None
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                # Ensure enabled_models exists
                if "enabled_models" not in config:
                    config["enabled_models"] = []
        except Exception as e:
            logger.error(f"Error reading persistent config {CONFIG_FILE}: {e}")
            config = None
    # Fallback to local config
    if config is None:
        try:
            with open("config.json", "r") as f:
                config = json.load(f)
                if "enabled_models" not in config:
                    config["enabled_models"] = []
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
    # Apply chat-agent defaults for any keys the stored config does not set, so
    # every caller (chat agent loop, index builder, settings form) sees a complete
    # config regardless of how old the persisted config.json is.
    for _k, _v in CHAT_CONFIG_DEFAULTS.items():
        if _k not in config:
            config[_k] = _v
    return config

def load_processed():
    # Try persistent state first
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {issue_id: {"status": "fixed", "timestamp": datetime.now().isoformat()} for issue_id in data}
                return data
        except: pass
    # Fallback to local state
    if os.path.exists("processed_issues.json"):
        try:
            with open("processed_issues.json", "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {issue_id: {"status": "fixed", "timestamp": datetime.now().isoformat()} for issue_id in data}
                return data
        except: pass
    return {}

def save_processed(processed):
    """Saves processed issues to persistent storage, with fallback to local file.

    Written via a temp file + os.replace (see save_config) so a crash mid-write
    can't leave a truncated/empty STATE_FILE for the next load_processed() to
    choke on.
    """
    try:
        # Primary: Persistent storage
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(processed, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error(f"Error saving to persistent state file {STATE_FILE}: {e}")
        try:
            # Fallback: Local directory
            tmp = "processed_issues.json.tmp"
            with open(tmp, "w") as f:
                json.dump(processed, f, indent=2)
            os.replace(tmp, "processed_issues.json")
        except Exception as fe:
            logger.error(f"Critical failure saving processed history to both locations: {fe}")

def load_pr_reviews():
    """Load the persisted PR pre-review store (keyed 'repo#number').

    pr_reviews used to be in-memory only, so it reset to {} on every restart.
    Terminal PRs (merged/denied) are closed and never re-scanned, so after a
    restart they vanished from the list entirely; approved-but-open PRs lost
    their APPROVED badge. Persisting the store fixes that."""
    if os.path.exists(PR_REVIEWS_FILE):
        try:
            with open(PR_REVIEWS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.error(f"Error loading PR reviews from {PR_REVIEWS_FILE}: {e}")
    return {}


def save_pr_reviews(pr_reviews):
    """Persist the PR pre-review store so merged/denied/approved survive restarts.

    Written via a temp file + os.replace (see save_config) so a crash mid-write
    can't leave a truncated/empty PR_REVIEWS_FILE for the next load to choke on.
    """
    try:
        tmp = PR_REVIEWS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(pr_reviews, f, indent=2)
        os.replace(tmp, PR_REVIEWS_FILE)
    except Exception as e:
        logger.error(f"Error saving PR reviews to {PR_REVIEWS_FILE}: {e}")



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
    if not os.path.exists(LLM_TPS_FILE):
        return {}
    try:
        with open(LLM_TPS_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out = {}
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
    try:
        tmp = LLM_TPS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(llm_tps, f)
        os.replace(tmp, LLM_TPS_FILE)
    except Exception as e:
        logger.error(f"Error saving LLM tok/s cache to {LLM_TPS_FILE}: {e}")


def load_update_state():
    """Loads the update state for recovery."""
    if os.path.exists(UPDATE_STATE_FILE):
        try:
            with open(UPDATE_STATE_FILE, "r") as f:
                return json.load(f)
        except: pass
    return {"last_known_good_commit": None, "failed_commits": []}

def save_update_state(state):
    """Saves the update state for recovery.

    Written via a temp file + os.replace (see save_config) so a crash mid-write
    can't leave a truncated/empty UPDATE_STATE_FILE for the next load to choke
    on -- this file specifically backs update-recovery, so a corrupt read here
    would defeat the recovery mechanism it exists for.
    """
    try:
        tmp = UPDATE_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, UPDATE_STATE_FILE)
    except Exception as e:
        logger.error(f"Error saving update state: {e}")


def get_version():
    try:
        with open(VERSION_FILE, "r") as f: return f.read().strip()
    except: return "Unknown"

STARTUP_STAMP_FILE = os.path.join(CONFIG_DIR, "startup_stamp.json")

def write_startup_stamp():
    """Record which commit this process actually booted on, plus start time / pid /
    main.py mtime. The watchdog reads this to detect stale-running code (disk updated
    but the process never restarted) and force a restart to load the pending update.
    Also drives the Diagnostics panel's running-vs-disk version comparison."""
    try:
        import main  # for main.__file__ — the stamp records main.py's mtime, not config_store.py's
        commit = "unknown"
        try:
            commit = git.Repo(os.getcwd()).head.commit.hexsha
        except Exception as ge:
            logger.warning(f"Startup stamp: could not read git commit: {ge}")
        stamp = {
            "commit": commit,
            "version": get_version(),
            "started_at": datetime.now().isoformat(),
            "pid": os.getpid(),
            "main_mtime": os.path.getmtime(main.__file__),
        }
        with open(STARTUP_STAMP_FILE, "w") as f:
            json.dump(stamp, f, indent=2)
        logger.info(f"Startup stamp written: commit={commit[:7] if commit != 'unknown' else 'unknown'} pid={os.getpid()}")
    except Exception as e:
        logger.warning(f"Could not write startup stamp: {e}")


__all__ = [
    "load_llm_tps", "save_llm_tps", "LLM_TPS_FILE", "LLM_PERF_FILE",

    "CONFIG_DIR",
    "CONFIG_FILE",
    "ENV_FILE",
    "STATE_FILE",
    "PR_REVIEWS_FILE",
    "UPDATE_STATE_FILE",
    "SELF_SCAN_OFFSET_FILE",
    "CHAT_HISTORY_FILE",
    "VERSION_FILE",
    "CHAT_CONFIG_DEFAULTS",
    "STARTUP_STAMP_FILE",
    "save_config",
    "load_config",
    "load_processed",
    "save_processed",
    "load_pr_reviews",
    "save_pr_reviews",
    "load_update_state",
    "save_update_state",
    "get_version",
    "write_startup_stamp",
]
