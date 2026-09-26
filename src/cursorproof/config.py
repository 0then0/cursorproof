import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import parse_qs, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cursorproof.paths import tokens

type Parameter = str | int
type Identity = str | int
Positive = Annotated[int, Field(gt=0)]
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def http_url(value: str) -> str:
    parsed = urlsplit(value)
    _ = parsed.port
    if any(char.isspace() or not char.isprintable() for char in value):
        raise ValueError("URL must not contain whitespace or control characters")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("An absolute HTTP(S) URL is required")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError(
            "URL userinfo and fragments are not supported; use headers for credentials"
        )
    return value


def path_field(value: str) -> str:
    tokens(value)
    return value


def http_headers(value: dict[str, str]) -> dict[str, str]:
    for name, content in value.items():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError("Invalid HTTP header name")
        if not content.isascii() or any(ord(char) < 32 and char != "\t" for char in content):
            raise ValueError("HTTP header values must be ASCII without control characters")
    return value


def terminal_default() -> list[str | None]:
    return [None]


class Pagination(Model):
    cursor_param: str = "cursor"
    limit_param: str = "limit"
    terminal_values: list[str | None] = Field(default_factory=terminal_default, min_length=1)

    @model_validator(mode="after")
    def distinct_names(self) -> Self:
        if not self.cursor_param or not self.limit_param or self.cursor_param == self.limit_param:
            raise ValueError("Cursor and limit parameter names must be nonempty and different")
        return self


class Response(Model):
    items: str = "$.results"
    next_cursor: str = "$.next_cursor"
    has_more: str | None = None
    id: str = "$.id"
    snapshot_fields: list[str] = Field(default_factory=list)

    _paths = field_validator("items", "next_cursor", "id")(path_field)

    @field_validator("snapshot_fields")
    @classmethod
    def snapshot_paths(cls, values: list[str]) -> list[str]:
        checked = [path_field(value) for value in values]
        if len(set(checked)) != len(checked):
            raise ValueError("Snapshot fields must be unique")
        return checked

    @field_validator("has_more")
    @classmethod
    def optional_path(cls, value: str | None) -> str | None:
        return path_field(value) if value is not None else None


class Ordering(Model):
    field: str
    direction: Literal["asc", "desc"] = "asc"
    type: Literal["auto", "string", "number", "datetime"] = "auto"
    nulls: Literal["first", "last"] = "last"

    _path = field_validator("field")(path_field)


class Command(Model):
    command: list[str] = Field(min_length=1)
    timeout: float = Field(default=30.0, gt=0)

    @field_validator("command")
    @classmethod
    def executable(cls, value: list[str]) -> list[str]:
        if not value[0] or any("\x00" in part for part in value):
            raise ValueError("Command must contain an executable and no NUL bytes")
        return value


class Mutation(Command):
    after_page: Positive


class Oracle(Model):
    url: str | None = None
    command: list[str] | None = None
    timeout: float = Field(default=30.0, gt=0)
    headers: dict[str, str] = Field(default_factory=dict)
    format: Literal["json", "lines"] = "json"
    items: str = "$"
    id: str = "$"
    ordered: bool = True

    _paths = field_validator("items", "id")(path_field)
    _headers = field_validator("headers")(http_headers)

    @model_validator(mode="after")
    def source(self) -> Self:
        if (self.url is None) == (self.command is None):
            raise ValueError("Oracle requires exactly one of url or command")
        if self.url is not None:
            http_url(self.url)
        if self.command is not None:
            Command(command=self.command, timeout=self.timeout)
        if self.format == "lines" and (self.items != "$" or self.id != "$"):
            raise ValueError("Lines oracle returns string IDs directly; paths must be $")
        return self


class Binding(Model):
    parameters: dict[str, list[Parameter]] = Field(min_length=1)
    mismatch_behavior: Literal["reject"] = "reject"
    reject_statuses: list[int] = Field(default_factory=lambda: [400, 409, 422], min_length=1)

    @model_validator(mode="after")
    def alternatives(self) -> Self:
        if any(not values for values in self.parameters.values()):
            raise ValueError("Binding parameters need explicit alternative values")
        if any(not 400 <= status < 500 for status in self.reject_statuses):
            raise ValueError("Binding rejection statuses must be 4xx")
        return self


class Config(Model):
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    parameters: dict[str, Parameter] = Field(default_factory=dict)
    pagination: Pagination = Field(default_factory=Pagination)
    response: Response = Field(default_factory=Response)
    ordering: list[Ordering] = Field(default_factory=list)
    limits: list[Positive] = Field(default_factory=lambda: [1, 10, 50, 51, 100], min_length=1)
    repeats: Positive = 1
    timeout: float = Field(default=10.0, gt=0)
    max_pages: Positive = 10_000
    max_items: Positive = 100_000
    max_response_bytes: Positive = 10_000_000
    consistency: Literal["static", "snapshot", "live-keyset"] = "static"
    immutable_ordering: bool = False
    oracle: Oracle | None = None
    mutations: list[Mutation] = Field(default_factory=list)
    cursor_binding: Binding | None = None

    _url = field_validator("url")(http_url)
    _headers = field_validator("headers")(http_headers)

    @model_validator(mode="after")
    def contract(self) -> Self:
        if len(set(self.limits)) != len(self.limits):
            raise ValueError("Limits must be unique")
        reserved = {self.pagination.cursor_param, self.pagination.limit_param}
        query = parse_qs(urlsplit(self.url).query, keep_blank_values=True)
        if reserved & (self.parameters.keys() | query.keys()):
            raise ValueError("Cursor and limit belong in pagination/limits, not URL or parameters")
        if self.mutations and self.consistency == "static":
            raise ValueError("Mutation hooks require snapshot or live-keyset consistency")
        if self.consistency != "static" and (len(self.limits) != 1 or self.repeats != 1):
            raise ValueError("Mutation modes require one limit and one traversal")
        if self.consistency == "snapshot" and self.oracle is None:
            raise ValueError("Snapshot consistency requires an oracle for the initial item stream")
        if self.consistency == "snapshot":
            if not self.response.snapshot_fields:
                raise ValueError("Snapshot consistency requires response.snapshot_fields")
            if self.oracle and self.oracle.format == "lines":
                raise ValueError("Snapshot oracle must return JSON records, not ID lines")
        elif self.response.snapshot_fields:
            raise ValueError("response.snapshot_fields is only valid with snapshot consistency")
        if self.consistency == "live-keyset":
            if not self.ordering or not self.immutable_ordering:
                raise ValueError("Live keyset requires ordering and immutable_ordering: true")
            if self.oracle is not None:
                raise ValueError("A static oracle cannot establish completeness for live keyset")
        if self.cursor_binding:
            for name, alternatives in self.cursor_binding.parameters.items():
                if name in reserved or name not in self.parameters:
                    raise ValueError("Binding parameters must be declared in parameters")
                if any(str(value) == str(self.parameters[name]) for value in alternatives):
                    raise ValueError("Binding alternatives must differ from the original value")
            if self.consistency != "static":
                raise ValueError("Binding probes currently require static consistency")
        return self


class ConfigError(ValueError):
    pass


def load_config(path: Path, *, resolve_env: bool = True) -> tuple[Config, set[str]]:
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError):
        raise ConfigError("Cannot read or parse the configuration") from None
    secrets: set[str] = set()

    def expand(value: object) -> object:
        if isinstance(value, str):

            def replace(match: re.Match[str]) -> str:
                name = match.group(1)
                if not resolve_env:
                    return match.group(0)
                if name not in os.environ or not os.environ[name]:
                    raise ConfigError(f"Required environment variable is missing or empty: {name}")
                secret = os.environ[name]
                secrets.add(secret)
                return secret

            return _ENV.sub(replace, value)
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    try:
        config = Config.model_validate(expand(raw))
    except ValueError as exc:
        if isinstance(exc, ConfigError):
            raise
        # ValidationError includes input values, which may contain credentials.
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            details = [
                f"{'.'.join(map(str, error['loc'])) or 'config'}: {error['msg']}"
                for error in exc.errors(include_input=False)
            ]
            message = "Invalid configuration: " + "; ".join(details)
            for secret in sorted(secrets, key=len, reverse=True):
                message = message.replace(secret, "[REDACTED]")
            raise ConfigError(message) from None
        raise ConfigError("Invalid configuration") from None
    secrets.update(config.headers.values())
    if config.oracle:
        secrets.update(config.oracle.headers.values())
    return config, secrets - {""}
