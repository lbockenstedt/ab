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
    """Saves configuration to persistent storage, falling back to local if needed."""
    try:
        if os.path.exists(CONFIG_DIR) or os.access(CONFIG_DIR, os.W_OK):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            try:
                os.chmod(CONFIG_FILE, 0o600)
            except Exception:
                pass
            logger.info(f"Config saved to persistent storage: {CONFIG_FILE}")
        else:
            raise IOError("Persistent config directory not writable")
    except Exception as e:
        logger.warning(f"Could not save to persistent storage ({e}), falling back to local config.json")
        try:
            with open("config.json", "w") as f:
                json.dump(config, f, indent=2)
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
    """Saves processed issues to persistent storage, with fallback to local file."""
    try:
        # Primary: Persistent storage
        with open(STATE_FILE, "w") as f:
            json.dump(processed, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving to persistent state file {STATE_FILE}: {e}")
        try:
            # Fallback: Local directory
            with open("processed_issues.json", "w") as f:
                json.dump(processed, f, indent=2)
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
    """Persist the PR pre-review store so merged/denied/approved survive restarts."""
    try:
        with open(PR_REVIEWS_FILE, "w") as f:
            json.dump(pr_reviews, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving PR reviews to {PR_REVIEWS_FILE}: {e}")


def load_update_state():
    """Loads the update state for recovery."""
    if os.path.exists(UPDATE_STATE_FILE):
        try:
            with open(UPDATE_STATE_FILE, "r") as f:
                return json.load(f)
        except: pass
    return {"last_known_good_commit": None, "failed_commits": []}

def save_update_state(state):
    """Saves the update state for recovery."""
    try:
        with open(UPDATE_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
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
