import json
from pathlib import Path

import pytest

from support_desk.desk import Desk

ROOT = Path(__file__).resolve().parents[1]
SEED = json.loads((ROOT / "data" / "seed.json").read_text())
NOW = "2026-09-28T00:00:00Z"


@pytest.fixture
def seed():
    return json.loads(json.dumps(SEED))


@pytest.fixture
def desk(tmp_path, seed):
    return Desk.create(tmp_path / "desk.sqlite", seed, clock=lambda: NOW)
