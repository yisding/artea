// Copyright 2026 The Artea Authors. All rights reserved.
//
// PEP 700 upload-time enrichment orchestrator for the PyPI Simple API JSON
// surface. Runs ONLY when the client asked for
// `application/vnd.pypi.simple.v1+json` (the gateway gates this via the
// $pypi_wants_json map); the common non-JSON path (pip install, browsers,
// curl) keeps the byte-for-byte Gitea-first/devpi-fallback routing untouched.
//
// Why njs here and not nginx error_page: the precedence decision routes a
// Gitea *200* to a different body source (policy-sync's gitea enrichment) and a
// Gitea *404* to the devpi enrichment. error_page can re-route on an error
// status but not on a 200, so the orchestration lives in njs (the established
// authStatus subrequest pattern from pep503.js).
//
// Flow (enrichRoute):
//   1. probe Gitea's PEP 503 page (/_gitea_simple_probe, body off, client auth)
//   2. 200 -> /_enrich?upstream=gitea  (private wins; devpi never consulted)
//   3. 404 -> /_enrich?upstream=devpi  (public fallback)
//   4. /_enrich 404 on the gitea branch -> retry devpi (private vanished mid-flight)
//   5. probe 5xx or enrich 5xx -> 502 (a Gitea outage must NOT silently fall
//      through to public for a possibly-private name)

function responseBody(reply) {
    if (reply.responseText !== undefined) {
        return reply.responseText;
    }
    if (reply.responseBuffer !== undefined) {
        return String(reply.responseBuffer);
    }
    return '';
}

// Relay a policy-sync /_enrich reply to the client as a v1+json Simple page.
function relay(r, reply) {
    if (reply.status >= 200 && reply.status < 300) {
        r.headersOut['Content-Type'] = 'application/vnd.pypi.simple.v1+json';
        r.return(reply.status, responseBody(reply));
        return;
    }
    // A real "no such project" (the public mirror 404s) must reach the client as
    // 404 — JSON pip/uv read that as "no candidates", not a transient failure.
    // On the Gitea branch a 404 means "fall through to devpi" and is handled in
    // enrichRoute before relay() runs, so this only fires for the devpi branch's
    // genuine misses (absent from both Gitea and the public mirror).
    if (reply.status === 404) {
        r.return(404);
        return;
    }
    // 5xx from policy-sync (unreachable, upstream metadata down) -> 502.
    r.return(502);
}

function enrichDevpi(r, name) {
    r.subrequest('/_enrich', {args: 'upstream=devpi&name=' + encodeURIComponent(name)}, function(reply) {
        relay(r, reply);
    });
}

function enrichRoute(r) {
    var name = r.variables.pypi_project;
    if (!name) {
        r.return(400);
        return;
    }

    r.subrequest('/_gitea_simple_probe', {method: 'GET'}, function(probe) {
        if (probe.status === 404) {
            // No private package of this name -> public mirror.
            enrichDevpi(r, name);
            return;
        }
        if (probe.status < 200 || probe.status >= 300) {
            // Anything that is not a 200 (hit) or 404 (miss -> devpi, handled
            // above) — 5xx outage, auth surprise, or an unexpected 3xx — must NOT
            // fall through to public for a name that might be private. Per the
            // module contract (200->gitea, 404->devpi, else->502) surface it as a
            // gateway error.
            r.return(502);
            return;
        }
        // Gitea 200: enrich the private package.
        r.subrequest('/_enrich', {args: 'upstream=gitea&name=' + encodeURIComponent(name)}, function(reply) {
            if (reply.status === 404) {
                // Race: the package disappeared between probe and enrich. Fall
                // through to the public mirror, preserving precedence.
                enrichDevpi(r, name);
                return;
            }
            relay(r, reply);
        });
    });
}

export default {enrichRoute};
