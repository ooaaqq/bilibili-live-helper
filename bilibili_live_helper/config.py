import os
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver


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


class NtfyConfig(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, hide_input_in_errors=True
    )
    server: str
    topic: Annotated[str, Field(pattern=r"^[-_A-Za-z0-9]{1,64}$")]
    token: str | None = Field(default=None, repr=False)

    @field_validator("server")
    @classmethod
    def server_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "ntfy.server must be an HTTP(S) URL without credentials, query or fragment"
            )
        return value

    @field_validator("token")
    @classmethod
    def token_header(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("ntfy.token must contain printable ASCII characters")
        return value.strip() or None if value is not None else None


Uid = Annotated[int, Field(gt=0)]


class Settings(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, hide_input_in_errors=True
    )
    include_uids: Annotated[tuple[Uid, ...], Field(min_length=1)]
    watch_uids: tuple[Uid, ...] = ()
    poll_interval_seconds: Annotated[int, Field(ge=30, le=3600)] = 120
    request_timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 15
    api_interval_seconds: Annotated[int, Field(ge=1, le=60)] = 1
    like_clicks_per_request: Annotated[int, Field(ge=1, le=300)] = 30
    like_request_count: Annotated[int, Field(ge=1, le=100)] = 10
    like_interval_seconds: Annotated[int, Field(ge=1, le=3600)] = 60
    watch_minutes: Annotated[int, Field(ge=0, le=1440)] = 150
    heartbeat_interval_seconds: Annotated[int, Field(ge=30, le=300)] = 60
    danmaku_count: Annotated[int, Field(ge=0, le=100)] = 10
    danmaku_interval_seconds: Annotated[int, Field(ge=1, le=3600)] = 180
    global_danmaku_interval_seconds: Annotated[int, Field(ge=1, le=300)] = 3
    ntfy: NtfyConfig | None = None

    @field_validator("include_uids", "watch_uids", mode="before")
    @classmethod
    def yaml_sequences(cls, value):
        # YAML represents sequences as lists; keep the validated config immutable.
        return tuple(value) if isinstance(value, list) else value

    @field_validator("include_uids", "watch_uids")
    @classmethod
    def unique_uids(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("cannot contain duplicate UIDs")
        return value

    @model_validator(mode="after")
    def watch_configuration(self):
        if not set(self.watch_uids).issubset(self.include_uids):
            raise ValueError("watch_uids must be included in include_uids")
        if self.watch_uids and self.watch_minutes == 0:
            raise ValueError(
                "watch_minutes must be positive when watch_uids is not empty"
            )
        return self


def load_settings(path: Path) -> Settings:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML configuration: {error}") from error
    return Settings.model_validate(raw)


def load_access_key(path: Path) -> str:
    try:
        access_key = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"Unable to read access key: {error}") from error
    if not access_key:
        raise ValueError("access key cannot be empty")
    return access_key


def main() -> None:
    config_path = Path(os.environ.get("BILIBILI_LIVE_HELPER_CONFIG", "config.yaml"))
    load_settings(config_path)
    print("configuration ok")
