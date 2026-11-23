import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

FIXTURES = ROOT / "tests" / "fixtures"
REAL = FIXTURES / "real"


@pytest.fixture
def fixture():
    """Path to a test model by file name."""
    def get(name):
        p = FIXTURES / name
        assert p.exists(), f"missing fixture {name} - run tests/fixtures/make_fixtures.py"
        return p
    return get
