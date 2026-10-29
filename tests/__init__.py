# Lets `python -m unittest` find the package from a plain checkout (no `pip install -e .` needed).
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain"))
