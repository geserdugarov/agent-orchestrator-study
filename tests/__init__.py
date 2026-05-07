"""Unit tests for the orchestrator package."""

import os

# Keep ignored local deployment settings out of deterministic unit tests.
os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")
