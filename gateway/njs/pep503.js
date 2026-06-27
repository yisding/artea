// Copyright 2026 The Artea Authors. All rights reserved.
//
// PEP 503 project-name normalization, applied by the gateway BEFORE the
// Gitea-first lookup (docs/ARCHITECTURE.md "Resolution flows"): lowercase,
// collapse every run of '-', '_', '.' into a single '-'. Lives in njs because
// stock nginx has no lowercasing and no iterative regex replacement. The devpi
// fallback must use the SAME normalized name (see @public_pypi in nginx.conf).

function normalizeName(name) {
    return (name || '').toLowerCase().replace(/[-_.]+/g, '-');
}

function normalize(r) {
    return normalizeName(r.variables.pypi_project_raw || '');
}

function authStatus(r, done) {
    if (!r.headersIn.Authorization) {
        done(401);
        return;
    }

    r.subrequest('/_artea_user', {method: 'GET'}, function(user) {
        if (user.status < 200 || user.status >= 300) {
            done(user.status);
            return;
        }

        var login = giteaLogin(user);
        if (login === null) {
            done(503);
            return;
        }

        r.subrequest('/_artea_org_member/' + encodeURIComponent(login), {method: 'GET'}, function(member) {
            if (member.status < 200 || member.status >= 300) {
                done(member.status);
                return;
            }
            r.subrequest('/_artea_package_scope', {method: 'GET'}, function(scope) {
                done(scope.status >= 200 && scope.status < 300 ? 204 : scope.status);
            });
        });
    });
}

function giteaLogin(reply) {
    try {
        var body = JSON.parse(responseBody(reply));
        return typeof body.login === 'string' && body.login.length > 0 ? body.login : null;
    } catch (e) {
        return null;
    }
}

function auth(r) {
    authStatus(r, function(status) {
        r.return(status);
    });
}

function originalUri(r) {
    if (r.parent && r.parent.uri) {
        return r.parent.uri;
    }
    return r.variables.artea_original_uri || r.uri;
}

// The path of a public-PyPI mirror file, stripped of scheme/host/query/fragment.
// Returns null for anything that is not a /root/pypi/+f|+e/<dirs>/<file> path.
// We deliberately do NOT derive the project here: the gateway forwards only the
// path and devpi resolves the authoritative project from its own mirror
// metadata (see pypi_file_allowed_view), so njs never parses package filenames.
function pypiFilePath(value) {
    if (!value) {
        return null;
    }
    var path = String(value).trim();
    path = path.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/]+/i, '');
    path = path.replace(/^\/\/[^/]+/, '');
    var cut = path.search(/[?#]/);
    if (cut >= 0) {
        path = path.slice(0, cut);
    }
    if (!/^\/root\/pypi\/\+(?:f|e)\/(?:[^/]+\/)+[^/]+$/.test(path)) {
        return null;
    }
    return path;
}

// Identical copy of responseBody in pep700.js; the two njs modules are separate
// ConfigMap files and cannot import each other, so keep the two copies in sync.
function responseBody(reply) {
    if (reply.responseText !== undefined) {
        return reply.responseText;
    }
    if (reply.responseBuffer !== undefined) {
        return String(reply.responseBuffer);
    }
    return '';
}

function pypiFileGuard(r) {
    authStatus(r, function(status) {
        if (status < 200 || status >= 300) {
            r.return(status);
            return;
        }

        var path = pypiFilePath(originalUri(r));
        if (path === null) {
            r.return(403);
            return;
        }

        // Pass the path through the subrequest options `args` (the form pep700.js
        // already uses for /_enrich), NOT embedded in the URI string. Some njs
        // builds do not split a query off a URI-string subrequest target: the
        // location's $arg_path then comes up empty and the subrequest can resolve
        // without ever proxying to devpi, so the file-allowed check is skipped and
        // a stale/blocked mirror file would pass the guard (fail-open). The
        // options form keeps $arg_path populated across njs versions; nginx
        // url-decodes it once for the upstream's ?path= just as before.
        r.subrequest('/_artea_devpi_file_allowed',
                     {method: 'GET', args: 'path=' + encodeURIComponent(path)},
                     function(allowed) {
            if (allowed.status >= 200 && allowed.status < 300) {
                r.return(204);
                return;
            }
            r.return(allowed.status >= 500 ? 503 : 403);
        });
    });
}

export default {normalize, auth, pypiFileGuard};
