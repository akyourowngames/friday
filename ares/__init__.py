"""Ares — A personal AI assistant that lives in your terminal."""

import os
from pathlib import Path

__version__ = "0.1.0"

# Load .env before anything else so env var overrides are always in place.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

# Re-export key classes for convenience
from ares.agent import Agent  # noqa: F401, E402
from ares.models import AppConfig  # noqa: F401, E402
