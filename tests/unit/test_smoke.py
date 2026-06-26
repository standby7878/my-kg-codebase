import pytest

import codekg

pytestmark = pytest.mark.unit


def test_import_exposes_version() -> None:
    assert codekg.__version__ == "0.1.0"
