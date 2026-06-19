from __future__ import annotations

import logging
from pathlib import Path
import os

logger = logging.getLogger(__name__)

def patch_microbench():
    """Monkey-patch microbench12.tasks.tasks_root to point to the correct directory."""
    try:
        import microbench12.tasks
    except ImportError:
        logger.warning("microbench12 is not installed, skipping patch.")
        return

    # If the default tasks_root exists, no patching needed
    try:
        default_tasks = microbench12.tasks.tasks_root()
        if default_tasks.exists():
            logger.info(f"microbench12 tasks found at default path: {default_tasks}")
            return
    except Exception:
        pass

    # Look for uv cache git checkouts on Windows/Linux/Mac
    home = Path.home()
    possible_caches = [
        Path(os.environ.get("UV_CACHE_DIR") or home / "AppData/Local/uv/cache"),
        home / ".cache/uv",
        home / "Library/Caches/uv",
    ]
    
    for cache in possible_caches:
        if cache.exists():
            # Search recursively for tasks/manifest.json in git-v0 checkouts
            for p in cache.glob("git-v0/checkouts/**/tasks/manifest.json"):
                found_tasks = p.parent
                microbench12.tasks.tasks_root = lambda: found_tasks
                logger.info(f"Dynamically patched microbench12 tasks_root to: {found_tasks}")
                return

    logger.warning("Could not find microbench12 tasks directory in cache or default paths.")
