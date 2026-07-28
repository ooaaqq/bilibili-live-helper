import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

ROOT_FIELDS = {
    "include_uids",
    "watch_uids",
    "poll_interval_seconds",
    "request_timeout_seconds",
    "api_interval_seconds",
    "like_clicks_per_request",
    "like_request_count",
    "like_interval_seconds",
    "watch_minutes",
    "heartbeat_interval_seconds",
    "danmaku_count",
    "danmaku_interval_seconds",
    "global_danmaku_interval_seconds",
    "ntfy",
}

NTFY_TOPIC_PATTERN = re.compile(r"[-_A-Za-z0-9]{1,64}")


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


@dataclass(frozen=True)
class NtfyConfig:
    server: str
    topic: str
    token: str | None


@dataclass(frozen=True)
class Settings:
    include_uids: tuple[int, ...]
    watch_uids: tuple[int, ...]
    poll_interval_seconds: int
    request_timeout_seconds: int
    api_interval_seconds: int
    like_clicks_per_request: int
    like_request_count: int
    like_interval_seconds: int
    watch_minutes: int
    heartbeat_interval_seconds: int
    danmaku_count: int
    danmaku_interval_seconds: int
    global_danmaku_interval_seconds: int
    ntfy: NtfyConfig | None


def load_settings(path: Path) -> Settings:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader) or {}
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML configuration: {error}") from error
    if not isinstance(raw, dict):
        raise TypeError("Configuration root must be a YAML mapping")
    _reject_unknown(raw, ROOT_FIELDS, "configuration")

    include_uids = _uid_list(raw.get("include_uids"), "include_uids", required=True)
    watch_uids = _uid_list(raw.get("watch_uids", []), "watch_uids")
    unknown_watch_uids = [uid for uid in watch_uids if uid not in include_uids]
    if unknown_watch_uids:
        raise ValueError(
            f"watch_uids must be included in include_uids: {unknown_watch_uids}"
        )

    settings = Settings(
        include_uids=include_uids,
        watch_uids=watch_uids,
        poll_interval_seconds=_bounded_int(raw, "poll_interval_seconds", 120, 30, 3600),
        request_timeout_seconds=_bounded_int(
            raw, "request_timeout_seconds", 15, 1, 120
        ),
        api_interval_seconds=_bounded_int(raw, "api_interval_seconds", 1, 1, 60),
        like_clicks_per_request=_bounded_int(
            raw, "like_clicks_per_request", 30, 1, 300
        ),
        like_request_count=_bounded_int(raw, "like_request_count", 10, 1, 100),
        like_interval_seconds=_bounded_int(raw, "like_interval_seconds", 60, 1, 3600),
        watch_minutes=_bounded_int(raw, "watch_minutes", 150, 0, 1440),
        heartbeat_interval_seconds=_bounded_int(
            raw, "heartbeat_interval_seconds", 60, 30, 300
        ),
        danmaku_count=_bounded_int(raw, "danmaku_count", 10, 0, 100),
        danmaku_interval_seconds=_bounded_int(
            raw, "danmaku_interval_seconds", 180, 1, 3600
        ),
        global_danmaku_interval_seconds=_bounded_int(
            raw, "global_danmaku_interval_seconds", 3, 1, 300
        ),
        ntfy=_parse_ntfy(raw.get("ntfy")),
    )
    if settings.watch_uids and settings.watch_minutes == 0:
        raise ValueError("watch_minutes must be positive when watch_uids is not empty")
    return settings


def _parse_ntfy(value: Any) -> NtfyConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("ntfy must be a YAML mapping")
    _reject_unknown(value, {"server", "topic", "token"}, "ntfy")
    server = value.get("server")
    if not isinstance(server, str):
        raise TypeError("ntfy.server must be a URL")
    server = server.strip().rstrip("/")
    parsed = urlsplit(server)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("ntfy.server must be an HTTP(S) URL")
    topic = value.get("topic")
    if isinstance(topic, str):
        topic = topic.strip()
    if not isinstance(topic, str) or not NTFY_TOPIC_PATTERN.fullmatch(topic):
        raise ValueError(
            "ntfy.topic must contain 1-64 letters, numbers, underscores, or hyphens"
        )
    token = value.get("token")
    if token is not None and not isinstance(token, str):
        raise ValueError("ntfy.token must be a string")
    token = token.strip() if token else None
    if token and not token.isascii():
        raise ValueError("ntfy.token must contain only ASCII characters")
    return NtfyConfig(server=server, topic=topic, token=token)


def load_access_key(path: Path) -> str:
    try:
        access_key = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"Unable to read access key: {error}") from error
    if not access_key:
        raise ValueError("access key cannot be empty")
    return access_key


def _uid_list(value: Any, field: str, *, required: bool = False) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0 for uid in value
    ):
        raise ValueError(f"{field} must be a list of positive integers")
    if required and not value:
        raise ValueError(f"{field} cannot be empty")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} cannot contain duplicate UIDs")
    return tuple(value)


def _bounded_int(
    raw: dict[str, Any], field: str, default: int, minimum: int, maximum: int
) -> int:
    value = raw.get(field, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _reject_unknown(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"Unknown {label} fields: {', '.join(unknown)}")


def main() -> None:
    config_path = Path(os.environ.get("BILIBILI_LIVE_HELPER_CONFIG", "config.yaml"))
    load_settings(config_path)
    print("configuration ok")
