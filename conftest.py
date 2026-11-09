"""Test-wide setup: never start AB's background workers inside pytest.

Importing main starts the updater worker, which hard-resets this checkout to
origin and exits the process to restart -- wiping uncommitted work and killing
the test run."""
import os

os.environ["AB_DISABLE_WORKERS"] = "1"
