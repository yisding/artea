import pytest

from policy_sync.gitea import GiteaError, GiteaNotFound, fetch_raw
from tests.conftest import TEST_REPO, TEST_TOKEN


def test_fetch_returns_bytes_and_sends_token(mock_gitea):
    mock_gitea.files["npm-rules.yaml"] = b"block: []\n"
    data = fetch_raw(mock_gitea.url, TEST_REPO, "npm-rules.yaml", TEST_TOKEN)
    assert data == b"block: []\n"
    req = mock_gitea.requests[-1]
    assert req["path"] == f"/api/v1/repos/{TEST_REPO}/raw/npm-rules.yaml"
    assert req["authorization"] == f"token {TEST_TOKEN}"


def test_fetch_missing_file_raises_not_found(mock_gitea):
    with pytest.raises(GiteaNotFound):
        fetch_raw(mock_gitea.url, TEST_REPO, "nope.txt", TEST_TOKEN)


def test_fetch_server_error_raises_gitea_error(mock_gitea):
    mock_gitea.files["npm-rules.yaml"] = b"x"
    mock_gitea.fail_remaining = 1
    with pytest.raises(GiteaError) as exc:
        fetch_raw(mock_gitea.url, TEST_REPO, "npm-rules.yaml", TEST_TOKEN)
    assert not isinstance(exc.value, GiteaNotFound)


def test_fetch_bad_token_raises_gitea_error(mock_gitea):
    mock_gitea.files["npm-rules.yaml"] = b"x"
    with pytest.raises(GiteaError):
        fetch_raw(mock_gitea.url, TEST_REPO, "npm-rules.yaml", "wrong-token")


def test_fetch_connection_refused_raises_gitea_error(mock_gitea):
    url = mock_gitea.url
    mock_gitea.stop()  # fixture teardown re-stop is a no-op
    with pytest.raises(GiteaError):
        fetch_raw(url, TEST_REPO, "npm-rules.yaml", TEST_TOKEN)
