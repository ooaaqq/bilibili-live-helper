from pathlib import Path

import pytest

from bilibili_live_helper import config
from bilibili_live_helper.config import load_access_key, load_settings


def test_loads_single_account_and_preserves_watch_priority(tmp_path: Path):
    config = _write_config(
        tmp_path,
        """include_uids: [1, 2, 3]
watch_uids: [3, 1]
poll_interval_seconds: 60
ntfy:
  server: https://ntfy.example
  topic: notifications
  token: secret
""",
    )

    settings = load_settings(config)

    assert settings.include_uids == (1, 2, 3)
    assert settings.watch_uids == (3, 1)
    assert settings.poll_interval_seconds == 60
    assert settings.like_clicks_per_request == 30
    assert settings.ntfy is not None
    assert settings.ntfy.server == "https://ntfy.example"
    assert settings.ntfy.topic == "notifications"


@pytest.mark.parametrize(
    ("ntfy", "message"),
    [
        ("server: https://ntfy.example\n  topic: invalid topic", "ntfy.topic"),
        (
            "server: https://ntfy.example/?token=bad\n  topic: notifications",
            "ntfy.server",
        ),
    ],
)
def test_rejects_invalid_ntfy_configuration(tmp_path: Path, ntfy: str, message: str):
    path = _write_config(tmp_path, f"include_uids: [1]\nntfy:\n  {ntfy}\n")

    with pytest.raises(ValueError, match=message):
        load_settings(path)


def test_checked_in_config_is_valid():
    root = Path(__file__).parents[1]
    load_settings(root / "config.example.yaml")


def test_rejects_removed_multi_account_format(tmp_path: Path):
    config = _write_config(tmp_path, "accounts: []\n")
    with pytest.raises(ValueError, match="accounts"):
        load_settings(config)


def test_rejects_duplicate_yaml_keys(tmp_path: Path):
    config = _write_config(
        tmp_path,
        """include_uids: [1]
include_uids: [2]
""",
    )
    with pytest.raises(ValueError, match="duplicate key 'include_uids'"):
        load_settings(config)


def test_rejects_watch_uid_outside_whitelist(tmp_path: Path):
    config = _write_config(
        tmp_path,
        """include_uids: [1]
watch_uids: [2]
""",
    )
    with pytest.raises(ValueError, match="watch_uids must be included"):
        load_settings(config)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("include_uids", "[]", "at least 1 item"),
        ("include_uids", "[1, 1]", "cannot contain duplicate"),
        ("like_clicks_per_request", "301", "less than or equal to 300"),
        ("watch_minutes", "0", "watch_minutes must be positive"),
    ],
)
def test_rejects_unsafe_values(tmp_path: Path, field: str, value: str, message: str):
    base = {
        "include_uids": "[1]",
        "watch_uids": "[1]",
        field: value,
    }
    config = _write_config(
        tmp_path, "\n".join(f"{key}: {item}" for key, item in base.items())
    )
    with pytest.raises(ValueError, match=message):
        load_settings(config)


def test_check_config_entrypoint_uses_environment_path(
    tmp_path: Path, monkeypatch, capsys
):
    config_path = _write_config(tmp_path, "include_uids: [1]\n")
    monkeypatch.setenv("BILIBILI_LIVE_HELPER_CONFIG", str(config_path))

    config.main()

    assert capsys.readouterr().out == "configuration ok\n"


def test_load_access_key_reads_a_separate_file(tmp_path: Path):
    path = tmp_path / "access_key"
    path.write_text(" key\n", encoding="utf-8")

    assert load_access_key(path) == "key"


@pytest.mark.parametrize("value", ["", "\n"])
def test_load_access_key_rejects_empty_file(tmp_path: Path, value: str):
    path = tmp_path / "access_key"
    path.write_text(value, encoding="utf-8")

    with pytest.raises(ValueError, match="access key cannot be empty"):
        load_access_key(path)


def _write_config(tmp_path: Path, value: str) -> Path:
    path = tmp_path / "users.yaml"
    path.write_text(value, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "text",
    [
        "include_uids: [true]",
        'include_uids: ["1"]',
        'include_uids: [1]\npoll_interval_seconds: "60"',
        "include_uids: [1]\npoll_interval_seconds: true",
        "include_uids: [1]\nunknown: 1",
        "include_uids: [1]\nntfy:\n  server: https://example.com\n  topic: ok\n  unknown: 1",
    ],
)
def test_strict_types_and_unknown_fields(tmp_path, text):
    with pytest.raises(ValueError):
        load_settings(_write_config(tmp_path, text))


def test_configuration_is_immutable_and_errors_hide_tokens(tmp_path):
    settings = load_settings(_write_config(tmp_path, "include_uids: [1]"))
    with pytest.raises(ValueError):
        settings.watch_minutes = 1
    with pytest.raises(ValueError) as captured:
        load_settings(
            _write_config(
                tmp_path,
                'include_uids: [1]\nntfy:\n  server: https://example.com\n  topic: ok\n  token: "PRIVATE_TOKEN_汉字"',
            )
        )
    assert "PRIVATE_TOKEN" not in str(captured.value)
