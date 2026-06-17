"""policy-sync's min_age validator must accept/reject exactly the strings the
Verdaccio filter and the devpi plugin do — driven by the shared cross-language
vectors (docs/policy-spec/min-age-vectors.json) so the lockstep is CI-enforced.
"""

import json
import os

import pytest

from policy_sync.policy_model import PolicyError, _validate_min_age

_VECTORS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docs", "policy-spec", "min-age-vectors.json"
)
with open(_VECTORS_PATH) as f:
    VECTORS = json.load(f)


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["input"])
def test_valid_min_age_accepted(case):
    # policy-sync validates only (returns the stripped string unchanged)
    assert _validate_min_age(case["input"]) == case["input"].strip()


@pytest.mark.parametrize("bad", VECTORS["invalid"])
def test_invalid_min_age_rejected(bad):
    with pytest.raises(PolicyError):
        _validate_min_age(bad)
