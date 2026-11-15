import json
import os
from pathlib import Path

import pytest

# Must be set before app.main is imported (it builds the app at import time).
os.environ.setdefault("COACH_SECRET", "test-secret")

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "test-secret"


@pytest.fixture
def sample_payload() -> dict:
    return json.loads((FIXTURES / "sample_match.json").read_text())
