import sys
import os
import types
import gc

from dotenv import load_dotenv
from datasets import disable_caching

disable_caching()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..'))
load_dotenv(os.path.join(PROJECT_ROOT, '.env'))

# Keep the project root on the import path but avoid the script directory
# shadowing the package name when launched as: python worker/main.py
for path in list(sys.path):
    if os.path.abspath(path) == SCRIPT_DIR:
        sys.path.remove(path)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(1, SCRIPT_DIR)

# Install a lightweight package stub so imports like "from config import ..."
# still work under the legacy project layout when main.py is run directly.
if 'worker' not in sys.modules or not hasattr(sys.modules['worker'], '__path__'):
    pkg = types.ModuleType('worker')
    pkg.__path__ = [SCRIPT_DIR]
    sys.modules['worker'] = pkg

import shutil

from worker.worker import ImageWorker


def clear_local_image_store(image_dir: str) -> None:
    """Ensure the local image cache remains empty before and during a run."""
    if not image_dir:
        return
    os.makedirs(image_dir, exist_ok=True)
    for root, dirs, files in os.walk(image_dir, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except FileNotFoundError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    print(f"[LOCAL CLEANUP] Cleared image cache: {image_dir}")


if __name__ == "__main__":
    image_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'images'))
    clear_local_image_store(image_dir)
    gc.collect()
    worker = ImageWorker()
    worker.run()