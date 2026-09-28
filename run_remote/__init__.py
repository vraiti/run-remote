"""Every file in this package uses bare sibling imports (import sync, from
models import Profile, ...), not relative ones -- relative imports would
break the primary way these files are actually used: run directly as a
script (worker.py via `uv run`, tail_watch.py over ssh, main.py/recipes.py
locally), where relative imports fail outright ("no known parent package").

Importing this package from outside (e.g. `from run_remote import sync`,
as aws-manage's providers.py might) skips the "run directly as a script"
step that normally puts this directory on sys.path -- so it's done here
instead, once, as an import-time side effect, keeping every existing bare
import in every sibling file working unchanged either way.
"""
import sys
from pathlib import Path

_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
