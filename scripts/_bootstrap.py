"""Put src/ on sys.path so scripts run without an editable install.

Import this first in every script:  import _bootstrap  # noqa: F401
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
