import sys
from pathlib import Path

# server.py lives at the repo root; make it importable regardless of the
# directory pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
