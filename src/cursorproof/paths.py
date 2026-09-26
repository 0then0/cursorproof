"""A deliberately small field notation: $, $.field, $.field[0].child."""

import re

_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[(0|[1-9][0-9]*)\]")


def tokens(path: str) -> list[str | int]:
    if not path.startswith("$"):
        path = "$." + path
    result: list[str | int] = []
    position = 1
    while position < len(path):
        match = _TOKEN.match(path, position)
        if match is None:
            raise ValueError("Use $, dotted fields, and nonnegative array indices only")
        field, index = match.groups()
        result.append(field if field is not None else int(index))
        position = match.end()
    return result


def extract(value: object, path: str) -> object:
    for token in tokens(path):
        if isinstance(token, str) and isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(token, int) and isinstance(value, list) and token < len(value):
            value = value[token]
        else:
            raise ValueError("Required response field is missing or has an invalid container")
    return value
