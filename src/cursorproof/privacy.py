import hashlib
import json
import re
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from cursorproof.config import Identity

_SENSITIVE_PARTS = {
    "auth",
    "authorization",
    "cookie",
    "credential",
    "jwt",
    "pin",
    "password",
    "secret",
    "session",
    "sig",
    "signature",
    "token",
}
_SENSITIVE_KEY_PREFIXES = {"access", "api", "client", "private", "secret", "x"}
_COMMAND_CREDENTIAL = re.compile(
    r"(?i)(\b(?:password|passwd|pass|token|secret|credential|"
    r"(?:api|access|client)[_-]?(?:key|secret)|sslpassword)\s*=\s*)"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s;&]+)"
)


def is_sensitive_name(name: str) -> bool:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower().replace("-", "_").split("_")
    if any(part in _SENSITIVE_PARTS for part in parts):
        return True
    compact = "".join(parts)
    if any(compact.endswith(f"{prefix}key") for prefix in _SENSITIVE_KEY_PREFIXES):
        return True
    return name.lower() == "key" or any(
        prefix in _SENSITIVE_KEY_PREFIXES and parts[index + 1] == "key"
        for index, prefix in enumerate(parts[:-1])
    )


class Redactor:
    def __init__(self, secrets: set[str]) -> None:
        values = set(secrets)
        for value in secrets:
            if value.lower().startswith(("bearer ", "basic ")):
                values.add(value.split(" ", 1)[1])
        self.secrets = sorted(
            {
                variant
                for value in values
                if value
                for variant in (
                    value,
                    quote(value, safe=""),
                    quote_plus(value),
                )
            },
            key=len,
            reverse=True,
        )
        self.cursors: dict[str, str] = {}
        self.identities: dict[tuple[type[str] | type[int], Identity], str] = {}
        self.prefix = uuid4().hex

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        # Do not allow response-controlled control sequences in terminal output.
        return "".join(char if char.isprintable() else "?" for char in value)

    def identity(self, value: Identity) -> Identity:
        if (
            isinstance(value, str)
            and (self.text(value) != value or value.startswith("[redacted-item:"))
            or any(secret in str(value) for secret in self.secrets)
        ):
            key = type(value), value
            if key not in self.identities:
                self.identities[key] = f"[redacted-item:{self.prefix}:{len(self.identities) + 1}]"
            return self.identities[key]
        return value

    def cursor(self, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in self.cursors:
            self.cursors[value] = f"cursor_{len(self.cursors) + 1}"
        return self.cursors[value]

    def url(self, value: str, cursor_param: str) -> str:
        parts = urlsplit(value)
        query = []
        for name, item in parse_qsl(parts.query, keep_blank_values=True):
            if name == cursor_param:
                safe = self.cursor(item) or ""
            elif is_sensitive_name(name):
                safe = "[REDACTED]"
            else:
                safe = self.text(item)
            query.append((self.text(name), safe))
        return self.text(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")))


def replay_fingerprint(config: object, secrets: set[str] | None = None) -> str:
    """Hash the replay contract while omitting credentials from commands and headers."""
    from cursorproof.config import Config

    if not isinstance(config, Config):
        raise TypeError("Expected a validated CursorProof configuration")

    def sanitize(value: object, field: str | None = None) -> object:
        if isinstance(value, dict):
            if field == "headers":
                return {
                    name: "[REDACTED]" if is_sensitive_name(name) else item
                    for name, item in value.items()
                }
            return {key: sanitize(item, key) for key, item in value.items()}
        if isinstance(value, list):
            if field in {"command", "setup", "cleanup"}:
                return sanitize_command(value, secrets or set())
            return [sanitize(item) for item in value]
        if isinstance(value, str):
            if field == "url":
                parts = urlsplit(value)
                query = [
                    (name, "[REDACTED]" if is_sensitive_name(name) else item)
                    for name, item in parse_qsl(parts.query, keep_blank_values=True)
                ]
                value = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
            elif field is not None and is_sensitive_name(field):
                return "[REDACTED]"
            return value
        return value

    payload = sanitize(config.model_dump(mode="json"))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sanitize_command(command: list[object], secrets: set[str]) -> list[object]:
    names_by_value = getattr(secrets, "names_by_value", {})
    command_secrets = {
        secret
        for secret in secrets
        if not names_by_value.get(secret)
        or any(is_sensitive_name(name) for name in names_by_value[secret])
    }
    secret_variants = sorted(
        {
            variant
            for secret in command_secrets
            if secret
            for variant in (secret, quote(secret, safe=""), quote_plus(secret))
        },
        key=len,
        reverse=True,
    )
    sanitized: list[object] = []
    redact_next = False
    for value in command:
        if not isinstance(value, str):
            sanitized.append(value)
            redact_next = False
            continue
        for secret in secret_variants:
            value = value.replace(secret, "[REDACTED]")
        value = _COMMAND_CREDENTIAL.sub(r"\1[REDACTED]", value)
        if redact_next:
            sanitized.append("[REDACTED]")
            redact_next = False
            continue
        match = re.fullmatch(r"(-{1,2}[A-Za-z0-9_-]+)=(.*)", value)
        if match and is_sensitive_name(match.group(1).lstrip("-")):
            sanitized.append(f"{match.group(1)}=[REDACTED]")
            continue
        option = value.lstrip("-") if value.startswith("-") else ""
        if option and is_sensitive_name(option):
            sanitized.append(value)
            redact_next = True
            continue
        env_names = names_by_value.get(value, set())
        if any(is_sensitive_name(name) for name in env_names):
            sanitized.append("[REDACTED]")
        else:
            sanitized.append(sanitize_command_url(value))
    return sanitized


def sanitize_command_url(value: str) -> str:
    option = re.fullmatch(r"(--[A-Za-z0-9_-]+=)(.*)", value)
    prefix = option.group(1) if option else ""
    url = option.group(2) if option else value
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return value
        port = parts.port
    except ValueError:
        return value
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{port}" if port is not None else "")
    query = [
        (name, "[REDACTED]" if is_sensitive_name(name) else item)
        for name, item in parse_qsl(parts.query, keep_blank_values=True)
    ]
    safe_url = urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))
    return prefix + safe_url
