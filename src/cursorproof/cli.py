import json
import os
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console

from cursorproof import __version__
from cursorproof.checks import analyze
from cursorproof.config import ConfigError, load_config
from cursorproof.models import Report, Trace, parse_trace
from cursorproof.privacy import has_http_credentials, replay_fingerprint
from cursorproof.runner import ExecutionError, _run_boundary_command, run_boundaries
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
    if parsed.boundary_testing is not None:
        fail("This configuration requires the boundary command", output_format)
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
def boundary(
    config: Annotated[Path, typer.Argument(help="YAML or JSON configuration.")],
    output_format: Annotated[Format, typer.Option("--format")] = Format.text,
    repro: Annotated[Path, typer.Option(help="Path for the first failing scenario trace.")] = Path(
        "cursorproof-boundary-repro.json"
    ),
) -> None:
    """Run API traversals with fixture sizes around each configured limit."""
    try:
        parsed, secrets = load_config(config)
    except ConfigError as exc:
        fail(str(exc), output_format)
        return
    if parsed.boundary_testing is None:
        fail("Configuration must define boundary_testing setup and cleanup commands", output_format)
    if repro.resolve() == config.resolve():
        fail("Reproduction path must differ from configuration path", output_format)
    try:
        report = run_boundaries(parsed, cwd=config.resolve().parent, secrets=secrets)
    except (OSError, ValueError):
        fail("Cannot initialize boundary tests", output_format)
        return
    failing_case = next(
        (case for case in report.cases if case.outcome != "pass" and case.report is not None),
        None,
    )
    if failing_case is not None:
        failing_report = failing_case.report
        assert failing_report is not None
        try:
            save_trace(failing_report.trace, repro)
        except OSError:
            fail("Cannot write boundary reproduction trace", output_format)
        report.repro_path = str(repro)
    if output_format == Format.json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        console = Console(markup=False, highlight=False)
        console.print(f"CursorProof boundary: {report.outcome.upper()}")
        if report.repro_path:
            console.print(f"Reproduction trace: {report.repro_path}")
        for case in report.cases:
            observed = "not run" if case.observed_items is None else str(case.observed_items)
            console.print(
                f"limit={case.limit} expected={case.expected_items} "
                f"observed={observed} {case.outcome.upper()}"
            )
            if case.error:
                console.print(f"  {case.error}")
            if case.report and case.report.outcome != "pass":
                display(case.report, Format.text)
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
    allow_unverified_auth_scope: Annotated[
        bool,
        typer.Option(
            "--allow-unverified-auth-scope",
            help="Accept that live replay cannot verify the credentials' principal or scope.",
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
        if allow_unverified_auth_scope:
            fail("--allow-unverified-auth-scope requires --config", output_format)
            return
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
        selected = recorded.traversals[traversal_number - 1]
        fingerprint_config = parsed
        is_boundary_replay = recorded.expected_unique_items is not None
        if is_boundary_replay:
            if parsed.boundary_testing is None:
                fail("Boundary replay requires its fixture configuration", output_format)
                return
            fingerprint_config = parsed.model_copy(
                update={"limits": [selected.limit], "repeats": 1}
            )
        if replay_fingerprint(fingerprint_config, secrets) != recorded.replay_fingerprint:
            fail("Replay configuration contract does not match the saved trace", output_format)
            return
        if not selected.pages:
            fail("Selected traversal has no recorded request to match", output_format)
            return
        if selected.limit not in parsed.limits:
            fail("Saved traversal limit is not configured", output_format)
            return
        # Display URLs may redact accidental matches with a short secret. The
        # versioned configuration fingerprint verifies the request contract.
        unverified_auth_scope = has_http_credentials(parsed)
        if unverified_auth_scope and not allow_unverified_auth_scope:
            fail(
                "Live replay cannot verify authentication scope; confirm it is unchanged "
                "and pass --allow-unverified-auth-scope",
                output_format,
            )
            return
        has_commands = (
            bool(parsed.mutations)
            or bool(parsed.oracle and parsed.oracle.command)
            or is_boundary_replay
        )
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
        if is_boundary_replay:
            boundary = parsed.boundary_testing
            assert boundary is not None
            setup = [
                argument.replace("{count}", str(recorded.expected_unique_items)).replace(
                    "{limit}", str(selected.limit)
                )
                for argument in boundary.setup
            ]
            replay_error: str | None = None
            boundary_report: Report | None = None
            try:
                _run_boundary_command(setup, boundary.timeout, config.resolve().parent)
                boundary_report = execute(
                    replay_config, cwd=config.resolve().parent, secrets=secrets
                )
                boundary_report.trace.expected_unique_items = recorded.expected_unique_items
                boundary_report = analyze(boundary_report.trace)
            except (ExecutionError, OSError, ValueError):
                replay_error = "Cannot prepare or run boundary fixtures for live replay"
            finally:
                try:
                    _run_boundary_command(
                        boundary.cleanup, boundary.timeout, config.resolve().parent
                    )
                except ExecutionError:
                    replay_error = "Boundary fixture cleanup failed during live replay"
            if replay_error is not None:
                fail(replay_error, output_format)
                return
            assert boundary_report is not None
            report = boundary_report
        else:
            report = execute(replay_config, cwd=config.resolve().parent, secrets=secrets)
        report.notes.append(
            f"Live replay of saved traversal {traversal_number}; results may differ "
            "if API data or external state changed."
        )
        if unverified_auth_scope:
            report.notes.append(
                "Authentication principal and scope were not verified during replay."
            )
    display(report, output_format)
    raise typer.Exit(report.exit_code)
