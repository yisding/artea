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

function stripArchiveExtension(filename) {
    var lower = filename.toLowerCase();
    var exts = ['.tar.gz', '.tar.bz2', '.tar.xz', '.zip', '.tgz'];
    for (var i = 0; i < exts.length; i++) {
        if (lower.endsWith(exts[i])) {
            return filename.slice(0, -exts[i].length);
        }
    }
    return null;
}

function projectFromFilename(filename) {
    if (filename.toLowerCase().endsWith('.whl')) {
        var wheelBase = filename.slice(0, -4);
        var firstDash = wheelBase.indexOf('-');
        return firstDash > 0 ? normalizeName(wheelBase.slice(0, firstDash)) : null;
    }

    var base = stripArchiveExtension(filename);
    if (base === null) {
        return null;
    }
    var parts = base.split('-');
    for (var i = parts.length - 1; i > 0; i--) {
        if (/^v?\d/.test(parts[i])) {
            return normalizeName(parts.slice(0, i).join('-'));
        }
    }
    return null;
}

function originalUri(r) {
    if (r.parent && r.parent.uri) {
        return r.parent.uri;
    }
    return r.variables.artea_original_uri || r.uri;
}

function canonicalPath(value) {
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
    if (path.charAt(0) !== '/') {
        return null;
    }
    try {
        return decodeURIComponent(path);
    } catch (e) {
        return null;
    }
}

function pypiFileProject(path) {
    if (path === null) {
        return null;
    }
    var match = /^\/root\/pypi\/\+(?:f|e)\/(?:[^/]+\/)+([^/?#]+)$/.exec(path);
    if (match === null) {
        return null;
    }
    return projectFromFilename(match[1]);
}

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

        var uri = canonicalPath(originalUri(r));
        var project = pypiFileProject(uri);
        if (project === null) {
            r.return(403);
            return;
        }

        var probe = '/_artea_devpi_file_allowed/' + encodeURIComponent(project) + '/?path=' + encodeURIComponent(uri);
        r.subrequest(probe, {method: 'GET'}, function(allowed) {
            if (allowed.status >= 200 && allowed.status < 300) {
                r.return(204);
                return;
            }
            r.return(allowed.status >= 500 ? 503 : 403);
        });
    });
}

export default {normalize, auth, pypiFileGuard};
