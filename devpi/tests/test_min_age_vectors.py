"""The devpi plugin's ISO-8601 duration parser must agree with the Verdaccio
filter and policy-sync — driven by the shared cross-language vectors
(docs/policy-spec/min-age-vectors.json).
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "artea_devpi_policy" / "src"))

from artea_devpi_policy.main import parse_iso_duration_seconds  # noqa: E402

_VECTORS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docs", "policy-spec", "min-age-vectors.json"
)
with open(_VECTORS_PATH) as f:
    VECTORS = json.load(f)


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["input"])
def test_valid_min_age_seconds(case):
    assert parse_iso_duration_seconds(case["input"]) == case["seconds"]


@pytest.mark.parametrize("bad", VECTORS["invalid"])
def test_invalid_min_age_rejected(bad):
    with pytest.raises(ValueError):
        parse_iso_duration_seconds(bad)
