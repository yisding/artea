// Copyright 2026 The Artea Authors. All rights reserved.
//
// PEP 503 project-name normalization, applied by the gateway BEFORE the
// Gitea-first lookup (docs/ARCHITECTURE.md "Resolution flows"): lowercase,
// collapse every run of '-', '_', '.' into a single '-'. Lives in njs because
// stock nginx has no lowercasing and no iterative regex replacement. The devpi
// fallback must use the SAME normalized name (see @public_pypi in nginx.conf).

function normalize(r) {
    return (r.variables.pypi_project_raw || '').toLowerCase().replace(/[-_.]+/g, '-');
}

export default {normalize};
