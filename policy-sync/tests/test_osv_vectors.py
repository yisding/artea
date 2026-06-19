"""policy-sync produces the OSV decision wire shape the npm filter and the devpi
plugin parse — driven by the shared cross-language vector
(docs/policy-spec/osv-decision-vectors.json) so a producer-side field rename
breaks CI instead of only an integration test.
"""

import json
import os

from policy_sync.osv import OsvDecisionResult, OsvVerdict, response_payload

_VECTORS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docs", "policy-spec", "osv-decision-vectors.json"
)
with open(_VECTORS_PATH) as f:
    VECTORS = json.load(f)


def test_response_payload_matches_shared_wire_shape():
    response = VECTORS["response"]
    result = OsvDecisionResult(
        status=response["status"],
        verdicts=tuple(
            OsvVerdict(version=r["version"], blocked=r["blocked"], ids=tuple(r["ids"]))
            for r in response["results"]
        ),
    )
    # reason is observability-only and omitted when empty, matching the vector.
    assert response_payload(result) == response
