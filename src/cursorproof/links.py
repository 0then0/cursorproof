"""A bounded parser for the RFC 8288 `Link` header, used to find the `next` relation.

Supported grammar (RFC 8288 section 3, with RFC 7230 list and quoted-string rules):

    Link       = #link-value
    link-value = "<" URI-Reference ">" *( OWS ";" OWS link-param )
    link-param = token BWS [ "=" BWS ( token / quoted-string ) ]

Anything else is malformed and raises `LinkError`; the caller must not treat a
malformed header as the end of a traversal.
"""

from dataclasses import dataclass

MAX_LINK_HEADER_BYTES = 8192
_TOKEN = frozenset("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_OWS = " \t"


class LinkError(ValueError):
    """A safe diagnostic; it never contains header content."""


@dataclass(frozen=True)
class NextLink:
    """The outcome of a successful parse of all `Link` field lines of one response."""

    target: str | None
    field_lines: int
    links: int
    next_links: int


def _text(char: str) -> bool:
    # HTAB, SP, VCHAR and obs-text; other control characters never appear in a field value.
    return char == "\t" or " " <= char <= "~" or "\x80" <= char <= "\xff"


def _parse_line(value: str) -> list[tuple[str, list[tuple[str, str | None]]]]:
    links: list[tuple[str, list[tuple[str, str | None]]]] = []
    position = 0
    end = len(value)

    def skip_whitespace() -> None:
        nonlocal position
        while position < end and value[position] in _OWS:
            position += 1

    def token() -> str:
        nonlocal position
        start = position
        while position < end and value[position] in _TOKEN:
            position += 1
        if position == start:
            raise LinkError("Link header is malformed: expected a parameter name or token value")
        return value[start:position]

    def quoted_string() -> str:
        nonlocal position
        position += 1
        chars: list[str] = []
        while position < end:
            char = value[position]
            position += 1
            if char == '"':
                return "".join(chars)
            if char == "\\":
                if position == end:
                    break
                char = value[position]
                position += 1
            if not _text(char):
                raise LinkError("Link header is malformed: invalid character in a quoted string")
            chars.append(char)
        raise LinkError("Link header is malformed: unterminated quoted string")

    while True:
        skip_whitespace()
        if position == end:
            return links
        if value[position] == ",":
            # RFC 7230 list syntax: recipients accept empty elements.
            position += 1
            continue
        if value[position] != "<":
            raise LinkError("Link header is malformed: a link must start with <target>")
        closing = value.find(">", position)
        if closing == -1:
            raise LinkError("Link header is malformed: unterminated <target>")
        target = value[position + 1 : closing]
        if not all(_text(char) for char in target) or "<" in target:
            raise LinkError("Link header is malformed: invalid character in a link target")
        position = closing + 1
        parameters: list[tuple[str, str | None]] = []
        while True:
            skip_whitespace()
            if position == end or value[position] == ",":
                break
            if value[position] != ";":
                raise LinkError("Link header is malformed: expected ';' or ',' after a link")
            position += 1
            skip_whitespace()
            name = token().lower()
            skip_whitespace()
            content: str | None = None
            if position < end and value[position] == "=":
                position += 1
                skip_whitespace()
                if position < end and value[position] == '"':
                    content = quoted_string()
                else:
                    content = token()
            parameters.append((name, content))
        links.append((target, parameters))


def next_link(field_lines: list[bytes]) -> NextLink:
    """Select the single `next` target from every `Link` field line of a response.

    - The relation is the first `rel` parameter of a link; later ones are ignored (RFC 8288
      section 3.3). Its value is a whitespace-separated list compared case-insensitively.
    - Links without `next` are ignored, whatever their other parameters.
    - Repeated `next` links are accepted only when their targets are identical strings.
    - A `next` link with an `anchor` parameter is rejected: its context is not this response.
    """
    if sum(len(line) for line in field_lines) > MAX_LINK_HEADER_BYTES:
        raise LinkError(f"Link header exceeds the supported {MAX_LINK_HEADER_BYTES} bytes")
    links = [link for line in field_lines for link in _parse_line(line.decode("latin-1"))]
    targets: list[str] = []
    for target, parameters in links:
        relation = next((content for name, content in parameters if name == "rel"), None)
        if relation is None or "next" not in relation.lower().split():
            continue
        if any(name == "anchor" for name, _ in parameters):
            raise LinkError(
                "Link next relation has an anchor parameter; "
                "a link context other than the response is not supported"
            )
        targets.append(target)
    if len(set(targets)) > 1:
        raise LinkError("Link header has several different next targets")
    return NextLink(
        target=targets[0] if targets else None,
        field_lines=len(field_lines),
        links=len(links),
        next_links=len(targets),
    )
