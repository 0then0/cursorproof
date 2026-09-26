import re
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from cursorproof.config import Identity

_SENSITIVE = re.compile(r"token|secret|password|authorization|api[_-]?key|credential", re.I)


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
            elif _SENSITIVE.search(name):
                safe = "[REDACTED]"
            else:
                safe = self.text(item)
            query.append((self.text(name), safe))
        return self.text(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")))
