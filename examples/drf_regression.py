"""Check DRF cursor pagination over SQLite with an installed CursorProof CLI.

Runs large-tie, unique-ordering and small-tie cases, checks replay, and saves evidence.
See docs/drf-regression.md for installation, commands and expected results.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def fixture_application(database: Path):
    import django
    from django.conf import settings

    settings.configure(
        SECRET_KEY="disposable-local-fixture",
        DEBUG=False,
        ALLOWED_HOSTS=["127.0.0.1"],
        ROOT_URLCONF=__name__,
        INSTALLED_APPS=["django.contrib.contenttypes", "rest_framework"],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(database)}},
        REST_FRAMEWORK={
            "DEFAULT_AUTHENTICATION_CLASSES": [],
            "DEFAULT_PERMISSION_CLASSES": [],
            "UNAUTHENTICATED_USER": None,
            "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
        },
        MIDDLEWARE=[],
    )
    django.setup()

    from django.core.wsgi import get_wsgi_application
    from django.db import connection, models
    from django.http import JsonResponse
    from django.urls import path
    from rest_framework.generics import ListAPIView
    from rest_framework.pagination import CursorPagination
    from rest_framework.serializers import ModelSerializer

    class Record(models.Model):
        id = models.IntegerField(primary_key=True)
        active = models.IntegerField(db_index=True)

        class Meta:
            app_label = "cursorproof_fixture"

    with connection.schema_editor() as editor:
        editor.create_model(Record)
    Record.objects.bulk_create([Record(id=index, active=1) for index in range(1, 2501)])

    def population(group):
        queryset = Record.objects.all()
        if group == "small":
            return queryset.filter(id__lte=800)
        if group != "all":
            raise ValueError("Unknown fixture scope")
        return queryset

    class RecordSerializer(ModelSerializer):
        class Meta:
            model = Record
            fields = ("id", "active")

    class TiePagination(CursorPagination):
        page_size = 500
        page_size_query_param = "page_size"
        max_page_size = 500
        ordering = ("active", "id")
        # offset_cutoff is inherited unchanged from the installed framework.

    class UniquePagination(TiePagination):
        ordering = ("id",)

    class TieView(ListAPIView):
        serializer_class = RecordSerializer
        pagination_class = TiePagination

        def get_queryset(self):
            return population(self.request.query_params.get("group", "all"))

    class UniqueView(TieView):
        pagination_class = UniquePagination

    def oracle(request):
        # Direct ORM read of the identical scope; CursorPagination is never used.
        ids = list(
            population(request.GET.get("group", "all")).order_by("id").values_list("id", flat=True)
        )
        return JsonResponse({"ids": ids})

    global urlpatterns
    urlpatterns = [
        path("ties/", TieView.as_view()),
        path("unique/", UniqueView.as_view()),
        path("oracle/", oracle),
    ]
    expected = list(Record.objects.order_by("id").values_list("id", flat=True))
    assert expected == list(range(1, 2501))
    assert CursorPagination.offset_cutoff == 1000
    connection.close()
    return get_wsgi_application(), expected, CursorPagination.offset_cutoff


@contextmanager
def fixture_server():
    from django.db import connections

    with tempfile.TemporaryDirectory(prefix="cursorproof-drf-") as disposable:
        server = None
        worker = None
        try:
            application, expected, cutoff = fixture_application(
                Path(disposable) / "fixture.sqlite3"
            )
            server = make_server("127.0.0.1", 0, application, handler_class=QuietHandler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            yield f"http://127.0.0.1:{server.server_port}", expected, cutoff
        finally:
            if server is not None:
                if worker is not None and worker.is_alive():
                    server.shutdown()
                    worker.join(timeout=5)
                server.server_close()
            connections.close_all()
            assert worker is None or not worker.is_alive(), "Fixture server failed to stop"


def cli(arguments: list[str]):
    result = subprocess.run(
        [sys.executable, "-m", "cursorproof", *arguments, "--format", "json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode not in {0, 1, 2}:
        raise RuntimeError(f"CursorProof process failed: {result.stderr}")
    return result.returncode, json.loads(result.stdout)


def summarize(report, expected):
    traversal = report["trace"]["traversals"][0]
    actual = [item["id"] for page in traversal["pages"] for item in page["items"]]
    return {
        "outcome": report["outcome"],
        "stop": traversal["stop"],
        "complete": traversal["stop"] == "terminal",
        "oracle_matches": actual == expected,
        "expected_ids": len(expected),
        "observed_ids": len(actual),
        "unique_ids": len(set(actual)),
        "findings": dict(sorted(Counter(f["code"] for f in report["findings"]).items())),
        "errors": [issue["message"] for issue in report["errors"]],
        "pages": [
            {
                "number": page["number"],
                "count": len(page["items"]),
                "first_id": page["items"][0]["id"] if page["items"] else None,
                "last_id": page["items"][-1]["id"] if page["items"] else None,
                "cursor": page["cursor"],
                "next_cursor": page["next_cursor"],
            }
            for page in traversal["pages"]
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="Configs, sanitized traces and summary."
    )
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        "versions": {
            name: version(name) for name in ("cursorproof", "Django", "djangorestframework")
        },
        "python": sys.version.split()[0],
        "dataset": {"count": 2500, "active": 1, "small_scope_count": 800},
        "cases": [],
    }
    with fixture_server() as (origin, expected, cutoff):
        summary["offset_cutoff"] = cutoff
        summary["dataset"]["ordered_ids_sha256"] = hashlib.sha256(
            json.dumps(expected, separators=(",", ":")).encode()
        ).hexdigest()
        for scenario, endpoint, group in (
            ("problematic", "ties", "all"),
            ("control", "unique", "all"),
            ("small-tie", "ties", "small"),
        ):
            for limit in (100, 250, 500):
                case_expected = expected[:800] if group == "small" else expected
                name = f"{scenario}-{limit}"
                config = {
                    "url": f"{origin}/{endpoint}/",
                    "parameters": {"group": group},
                    "pagination": {"mode": "url", "limit_param": "page_size"},
                    "response": {"next_cursor": "$.next"},
                    "ordering": [{"field": "id", "type": "number"}]
                    if endpoint == "unique"
                    else [{"field": "active", "type": "number"}, {"field": "id", "type": "number"}],
                    "limits": [limit],
                    "max_pages": 40,
                    "max_items": 5000,
                    "oracle": {"url": f"{origin}/oracle/?group={group}", "items": "$.ids"},
                }
                config_path = output / f"{name}.json"
                trace_path = output / f"{name}-trace.json"
                config_path.write_text(json.dumps(config, indent=2) + "\n")
                exit_code, report = cli(["run", str(config_path), "--repro", str(trace_path)])
                assert report["trace"]["oracle"] == case_expected, f"Oracle scope differs: {name}"
                replay_code, replay = cli(["replay", str(trace_path)])
                assert replay_code == exit_code and replay == report, (
                    f"Offline replay differs: {name}"
                )
                case = {
                    "name": name,
                    "limit": limit,
                    "exit_code": exit_code,
                    **summarize(report, case_expected),
                }
                case["offline_replay_matches"] = True
                if scenario == "problematic":
                    assert report["findings"] and not case["complete"], (
                        f"Violation not detected: {name}"
                    )
                    assert exit_code == 1 and not report["errors"], name
                    assert not case["oracle_matches"]
                    assert "CP003" not in case["findings"], (
                        "Incomplete traversal claims missing items"
                    )
                else:
                    assert exit_code == 0 and case["complete"] and case["oracle_matches"], name
                    assert case["observed_ids"] == case["unique_ids"], name
                # Confirm live replay starts afresh and follows the same real DRF links.
                live_code, live = cli(["replay", str(trace_path), "--config", str(config_path)])
                assert live_code == exit_code and summarize(live, case_expected) == summarize(
                    report, case_expected
                ), name
                case["live_replay_matches"] = True
                summary["cases"].append(case)
                print(
                    f"{name}: exit={exit_code} stop={case['stop']} "
                    f"IDs={case['observed_ids']}/{case['unique_ids']} "
                    f"findings={case['findings']}",
                    flush=True,
                )
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"Evidence: {output / 'summary.json'}")


if __name__ == "__main__":
    main()
