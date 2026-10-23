import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_gesture_engine_unit_tests():
    r = subprocess.run(["node", "--test", str(ROOT / "js" / "gestures.test.mjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
