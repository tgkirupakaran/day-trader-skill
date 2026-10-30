"""Load a script from a skill folder (the folder names have hyphens)."""

import atexit
import importlib.util
import os
import shutil
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")

# The scripts read trader/config.json from the current folder. Run the suite
# from an empty one, so your own config (say "market": "ca") can't leak in.
_HOME = tempfile.mkdtemp(prefix="trader-tests-")
atexit.register(shutil.rmtree, _HOME, True)
os.chdir(_HOME)


def load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
