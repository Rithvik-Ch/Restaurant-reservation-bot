#!/usr/bin/env python3
"""Launcher script — run resbot without pip install.

Usage:
    python3 run.py --help
    python3 run.py profile setup
    python3 run.py target add
    python3 run.py run
"""

import sys
from pathlib import Path

# Add src/ to Python's module search path so 'import resbot' works
sys.path.insert(0, str(Path(__file__).parent / "src"))

from resbot.cli import cli

if __name__ == "__main__":
    cli()
