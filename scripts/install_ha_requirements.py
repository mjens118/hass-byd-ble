"""Install the pinned requirements of the Home Assistant components the tests import.

`pytest-homeassistant-custom-component` installs Home Assistant itself, but not the third-party
packages that individual components (here: bluetooth and its dependencies) need at import time.
Those change between Home Assistant releases, so read them from the installed Home Assistant
instead of keeping a list that goes stale.

Usage:
    python scripts/install_ha_requirements.py            # pip install them
    python scripts/install_ha_requirements.py --print    # just print them (for uv or a lock file)
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT_COMPONENTS = ("bluetooth",)


def collect() -> list[str]:
    spec = importlib.util.find_spec("homeassistant")
    if spec is None or spec.origin is None:
        sys.exit("Home Assistant is not installed. Run: pip install pytest-homeassistant-custom-component")
    components = Path(spec.origin).parent / "components"
    seen: set[str] = set()
    todo = list(ROOT_COMPONENTS)
    requirements: list[str] = []
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        manifest = components / name / "manifest.json"
        if not manifest.exists():
            continue
        data = json.loads(manifest.read_text())
        requirements += data.get("requirements", [])
        todo += data.get("dependencies", [])
    return sorted(set(requirements))


def main() -> int:
    requirements = collect()
    if "--print" in sys.argv:
        print("\n".join(requirements))
        return 0
    if not requirements:
        return 0
    return subprocess.call([sys.executable, "-m", "pip", "install", *requirements])


if __name__ == "__main__":
    sys.exit(main())
