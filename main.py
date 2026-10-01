from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from file_organizer.cli import load_env, run  # noqa: E402


if __name__ == "__main__":
    load_env(PROJECT_ROOT / ".env")
    raise SystemExit(run(project_root=PROJECT_ROOT))
