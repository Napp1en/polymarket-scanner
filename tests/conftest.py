import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "gamma_events.json"


@pytest.fixture(scope="module")
def real_events():
    """Echte Gamma-Events (gekürzt auf die vom Scanner gelesenen Felder)."""
    return json.loads(FIXTURE.read_text())
