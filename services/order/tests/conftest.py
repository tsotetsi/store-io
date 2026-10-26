"""Make the service packages importable when pytest runs from any directory."""

import sys
from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parents[2]

if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))
