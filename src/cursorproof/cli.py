import json
import os
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import httpx
import typer
from pydantic import ValidationError
from rich.console import Console

from cursorproof import __version__
from cursorproof.checks import analyze
from cursorproof.config import ConfigError, load_config
from cursorproof.models import Report, Trace, parse_trace
from cursorproof.privacy import Redactor, replay_fingerprint
from cursorproof.runner import build_request
from cursorproof.runner import run as execute

app = typer.Typer(
    help="Verify cursor pagination across the entire REST stream.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


class Format(StrEnum):
    text = "text"
    json = "json"


def version(value: bool) -> None:
    if value:
        typer.echo(f"CursorProof {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    show_version: Annotated[
        bool, typer.Option("--version", callback=version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    pass


def fail(message: str, output_format: Format) -> None:
    if output_format == Format.json:
        typer.echo(json.dumps({"outcome": "error", "errors": [{"message": message}]}))
    else:
        Console(stderr=True, markup=False, highlight=False).print(f"Error: {message}")
    raise typer.Exit(2)


def display(report: Report, output_format: Format) -> None:
    if output_format == Format.json:
        typer.echo(report.model_dump_json(indent=2))
        return
    console = Console(markup=False, highlight=False)
    summary = report.summary
    console.print(f"CursorProof: {report.outcome.upper()}")
    console.print(
        f"Traversals: {summary.traversals}  Pages: {summary.pages}  "
        f"Items: {summary.items}  Unique identities: {summary.unique_items}"
    )
    for finding in report.findings:
        console.print(f"\n{finding.code} {finding.name}")
        console.print(finding.message)
        if finding.item_ids:
            console.print(f"Items ({finding.count}): {finding.item_ids}")
        for request in finding.requests:
            console.print(f"  GET {request}")
        for location in finding.locations:
            console.print(
                f"  traversal {location.traversal}, page {location.page}"
                + (f", position {location.position}" if location.position is not None else "")
            )
            traversal = report.trace.traversals[location.traversal - 1]
            if location.page <= len(traversal.pages):
                page = traversal.pages[location.page - 1]
                console.print(f"    GET {page.request}")
                console.print(f"    cursor: {page.cursor} -> {page.next_cursor}")
        if finding.possible_cause:
            console.print(f"Possible cause: {finding.possible_cause}")
    for error in report.errors:
        console.print(f"Error: {error.message}")
    for note in report.notes:
        console.print(f"Note: {note}")


def save_trace(trace: Trace, path: Path) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".cursorproof-",
            delete=False,
        ) as file:
            temporary = file.name
            file.write(trace.model_dump_json(indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


@app.command()
def check(config: Annotated[Path, typer.Argument(help="YAML or JSON configuration.")]) -> None:
    """Validate configuration and environment variables without HTTP or command execution."""
    try:
        load_config(config)
    except ConfigError as exc:
        fail(str(exc), Format.text)
    typer.echo("Configuration is valid.")


@app.command()
def run(
    config: Annotated[Path, typer.Argument(help="YAML or JSON configuration.")],
    output_format: Annotated[Format, typer.Option("--format")] = Format.text,
    repro: Annotated[Path, typer.Option(help="Path for the sanitized offline trace.")] = Path(
        "cursorproof-repro.json"
    ),
) -> None:
    """Traverse the API, check invariants, and save a reproduction trace."""
    if repro.resolve() == config.resolve():
        fail("Reproduction path must differ from configuration path", output_format)
    try:
        parsed, secrets = load_config(config)
    except ConfigError as exc:
        fail(str(exc), output_format)
        return
    try:
        report = execute(parsed, cwd=config.resolve().parent, secrets=secrets)
    except (OSError, ValueError):
        fail("Cannot initialize HTTP client or execute the configured request", output_format)
        return
    try:
        save_trace(report.trace, repro)
    except OSError:
        fail("Cannot write reproduction trace", output_format)
    display(report, output_format)
    raise typer.Exit(report.exit_code)


@app.command()
def replay(
    trace: Annotated[Path, typer.Argument(help="Previously saved reproduction trace.")],
    output_format: Annotated[Format, typer.Option("--format")] = Format.text,
    config: Annotated[
        Path | None, typer.Option("--config", help="Reissue one traversal against the API.")
    ] = None,
    traversal_number: Annotated[int, typer.Option("--traversal", min=1)] = 1,
    execute_hooks: Annotated[
        bool,
        typer.Option(
            "--execute-hooks",
            help="Run configured oracle and mutation commands during live replay.",
        ),
    ] = False,
) -> None:
    """Replay recorded evidence offline, or reissue one traversal with --config."""
    try:
        recorded = parse_trace(trace.read_bytes())
    except (OSError, ValidationError, ValueError):
        fail("Cannot read trace or unsupported/invalid trace schema", output_format)
        return
    if config is None:
        if execute_hooks:
            fail("--execute-hooks requires --config", output_format)
            return
        if traversal_number > 1:
            fail("--traversal requires --config", output_format)
            return
        report = analyze(recorded)
    else:
        if traversal_number > len(recorded.traversals):
            fail("Traversal number is outside the saved trace", output_format)
            return
        try:
            parsed, secrets = load_config(config)
        except ConfigError as exc:
            fail(str(exc), output_format)
            return
        if parsed.consistency != recorded.consistency:
            fail("Replay configuration consistency does not match the saved trace", output_format)
            return
        if recorded.replay_fingerprint is None:
            fail(
                "Saved trace lacks replay contract evidence; live replay is unavailable",
                output_format,
            )
            return
        if replay_fingerprint(parsed) != recorded.replay_fingerprint:
            fail("Replay configuration contract does not match the saved trace", output_format)
            return
        selected = recorded.traversals[traversal_number - 1]
        if not selected.pages:
            fail("Selected traversal has no recorded request to match", output_format)
            return
        request_params = {
            **parsed.parameters,
            parsed.pagination.limit_param: selected.limit,
        }
        try:
            with httpx.Client(headers=parsed.headers, follow_redirects=False) as client:
                request = build_request(client, parsed.url, request_params)
        except (OSError, ValueError, httpx.HTTPError, httpx.InvalidURL):
            fail("Cannot initialize HTTP client or build the replay request", output_format)
            return
        redaction_secrets = set(secrets) | set(parsed.headers.values())
        if parsed.oracle:
            redaction_secrets.update(parsed.oracle.headers.values())
        current_request = Redactor(redaction_secrets).url(
            str(request.url), parsed.pagination.cursor_param
        )
        if current_request != selected.pages[0].request:
            fail(
                "Replay configuration URL or query parameters do not match the saved traversal",
                output_format,
            )
            return
        has_commands = bool(parsed.mutations) or bool(parsed.oracle and parsed.oracle.command)
        if has_commands and not execute_hooks:
            fail(
                "Live replay has configured commands; pass --execute-hooks to run them",
                output_format,
            )
            return
        if execute_hooks and not has_commands:
            fail(
                "--execute-hooks was set, but the replay configuration has no commands",
                output_format,
            )
            return
        replay_config = parsed.model_copy(
            update={
                "limits": [selected.limit],
                "repeats": 1,
            }
        )
        report = execute(replay_config, cwd=config.resolve().parent, secrets=secrets)
        report.notes.append(
            f"Live replay of saved traversal {traversal_number}; results may differ "
            "if API data or external state changed."
        )
    display(report, output_format)
    raise typer.Exit(report.exit_code)
