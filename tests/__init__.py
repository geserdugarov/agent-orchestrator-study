# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the orchestrator package."""

import os

# Keep ignored local deployment settings out of deterministic unit tests.
os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")
