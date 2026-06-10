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
    assert cfg.policy_dir == "/policy"
    assert cfg.devpi_index == "root/constrained"
    assert cfg.port == 8920
    assert cfg.poll_interval == 300


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


def test_invalid_port_raises():
    with pytest.raises(ConfigError):
        Config.from_env(dict(REQUIRED, POLICY_SYNC_PORT="nope"))
