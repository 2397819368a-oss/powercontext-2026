import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from powercontext_eval_capture_ownership.models import Manifest, read_json


@pytest.fixture
def manifest():
    return Manifest.model_validate(read_json(Path(__file__).resolve().parents[1] / "fixtures/synthetic.v1.json"))
