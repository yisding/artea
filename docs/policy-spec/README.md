# Policy spec — cross-language test vectors

Some policy primitives are enforced by independent implementations in different
languages that must stay in exact agreement, or fail-closed enforcement silently
drifts. Before these vectors, the agreement was kept by hand-copied regexes and
"ports the filter's regex" code comments.

Each file here is the single source of truth for one such contract; every
implementation's unit tests load the file, so a divergence breaks CI instead of
shipping.

- **`min-age-vectors.json`** — the `upstream.min_age` ISO-8601 duration string
  (`policy.toml [upstream] min_age`, devpi index `min_upstream_age`). Parsed by
  `verdaccio-filter-artea` (`parseDurationMs`, TS → ms),
  `devpi` (`parse_iso_duration_seconds`, Py → seconds), and
  `policy-sync` (`policy_model._validate_min_age`, Py → validate only).
