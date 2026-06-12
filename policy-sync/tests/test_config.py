import pytest

from policy_sync.config import Config, ConfigError

REQUIRED = {
    "POLICY_SYNC_TOKEN": "tok",
    "POLICY_WEBHOOK_SECRET": "sec",
    "DEVPI_ROOT_PASSWORD": "pw",
}


def test_defaults_match_architecture_contract():
    cfg = Config.from_env(dict(REQUIRED))
    assert cfg.gitea_url == "http://gitea:3000"
    assert cfg.devpi_url == "http://devpi:3141"
    assert cfg.policy_repo == "artea/registry-policy"
    assert cfg.policy_file_path == "/policy/npm-rules.yaml"
    assert cfg.poll_interval == 300


def test_namespace_sets_default_policy_repo():
    cfg = Config.from_env(dict(REQUIRED, ARTEA_NAMESPACE="acme"))
    assert cfg.policy_repo == "acme/registry-policy"


def test_empty_policy_repo_still_uses_namespace_default():
    cfg = Config.from_env(dict(REQUIRED, ARTEA_NAMESPACE="acme", POLICY_REPO=""))
    assert cfg.policy_repo == "acme/registry-policy"


def test_policy_dir_env_still_sets_the_file_location():
    cfg = Config.from_env(dict(REQUIRED, POLICY_DIR="/data/policy/"))
    assert cfg.policy_file_path == "/data/policy/npm-rules.yaml"


def test_policy_file_path_overrides_policy_dir():
    cfg = Config.from_env(dict(REQUIRED, POLICY_DIR="/data", POLICY_FILE_PATH="/tmp/private/rules.yaml"))
    assert cfg.policy_file_path == "/tmp/private/rules.yaml"


def test_empty_policy_file_path_means_http_only_mode():
    cfg = Config.from_env(dict(REQUIRED, POLICY_FILE_PATH=""))
    assert cfg.policy_file_path == ""


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_missing_required_env_raises(missing):
    env = dict(REQUIRED)
    del env[missing]
    with pytest.raises(ConfigError, match=missing):
        Config.from_env(env)


def test_trailing_slashes_stripped():
    env = dict(REQUIRED, GITEA_URL="http://gitea:3000/", DEVPI_URL="http://devpi:3141/")
    cfg = Config.from_env(env)
    assert cfg.gitea_url == "http://gitea:3000"
    assert cfg.devpi_url == "http://devpi:3141"


def test_invalid_poll_interval_raises():
    with pytest.raises(ConfigError):
        Config.from_env(dict(REQUIRED, POLICY_SYNC_POLL_SECONDS="nope"))
