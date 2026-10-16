"""Pytest discovers the project root through this file, so `import fatcopy`
works whether the tests are run with `python -m pytest` or a bare `pytest`."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
