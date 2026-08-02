import os
import sys

# Project root on the import path so `import jarvis_core` works in CI.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Deterministic auth token for all web tests (must be set before importing app).
os.environ.setdefault("JARVIS_TOKEN", "ci-test-token")
