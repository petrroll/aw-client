import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from click.testing import CliRunner

from aw_core import Event
from aw_datastore import Datastore
from aw_datastore.storages import MemoryStorage
from aw_query import query as execute_query

from aw_client.cli import _report_query, main
from aw_client.rules_v2 import compile_profile_v2, migrate_legacy_settings


SOURCE_ONLY_CAPABILITIES = [
    "query.active_periods_v2.v1",
    "query.categorize_v2.v1",
    "query.merge_subwatcher_fields.source_namespace.v1",
    "query.query_bucket_optional.expected_hostname.v1",
    "query.query_bucket_optional_raw.v1",
    "query.query_period.v1",
    "query.flood_v2.v1",
]


def test_cli_report_aggregates_profile_presentation_fields():
    generated = _report_query(
        "events = [];",
        "$source.builtin_window.app",
        "$source.builtin_window.title",
    )

    assert 'events, ["$source.builtin_window.app", "$source.builtin_window.title"]' in generated
    assert 'title_events, ["$source.builtin_window.app"]' in generated
    assert 'events, ["app", "title"]' not in generated


def test_cli_report_executes_absent_settings_defaults():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    window = datastore.create_bucket(
        bucket_id="window",
        type="currentwindow",
        client="test",
        hostname="host",
        name="window",
    )
    afk = datastore.create_bucket(
        bucket_id="afk",
        type="afkstatus",
        client="test",
        hostname="host",
        name="afk",
    )
    window.insert(Event(timestamp=start, duration=60, data={"app": "vim", "title": "Code"}))
    afk.insert(Event(timestamp=start, duration=60, data={"status": "not-afk"}))
    buckets = {
        "window": {"id": "window", "type": "currentwindow", "hostname": "host"},
        "afk": {"id": "afk", "type": "afkstatus", "hostname": "host"},
    }
    profile = compile_profile_v2(
        migrate_legacy_settings({}),
        buckets,
        SOURCE_ONLY_CAPABILITIES,
        hostname="host",
    )
    report = _report_query(
        profile.query(),
        "$source.builtin_window.app",
        "$source.builtin_window.title",
    )
    result = execute_query(
        "cli-default-report", report, start, start + timedelta(minutes=1), datastore
    )

    assert result["window"]["duration"] == timedelta(minutes=1)
    assert result["window"]["cat_events"][0].data["$category"] == ["Work", "Programming"]
    assert sum((event.duration for event in result["window"]["active_events"]), timedelta()) == timedelta(minutes=1)


def test_cli_report_supports_meeting_only_profile_over_http():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    meeting_bucket = datastore.create_bucket(
        bucket_id="meeting",
        type="meeting",
        client="test",
        hostname="host",
        name="meeting",
    )
    meeting_bucket.insert(Event(timestamp=start, duration=8, data={"subject": "Planning"}))

    document = {
        "revision": 3,
        "activity_profiles_v2": [
            {
                "schema_version": 2,
                "id": "meeting-only",
                "category_set_ids": ["meetings"],
                "sources": [
                    {
                        "id": "meeting",
                        "label": "Meetings",
                        "bucket_ids": ["meeting"],
                        "scope": "global",
                        "fields": ["subject"],
                        "creates_activity": True,
                        "keeps_active": True,
                        "interval_policy": "exact",
                    }
                ],
                "active_time": {"type": "expression", "rule": {"type": "none"}},
            }
        ],
        "category_sets_v2": [
            {
                "schema_version": 2,
                "id": "meetings",
                "categories": [
                    {
                        "id": "meeting",
                        "name": ["Meeting"],
                        "rule": {
                            "type": "regex",
                            "source": "meeting",
                            "field": "subject",
                            "regex": ".",
                        },
                    }
                ],
            }
        ],
    }
    routes = {
        "/api/0/info": {"hostname": "host", "capabilities": SOURCE_ONLY_CAPABILITIES + ["settings.rules_v2.v1"]},
        "/api/0/settings/rules_v2": document,
        "/api/0/buckets": {
            "meeting": {
                "id": "meeting",
                "type": "meeting",
                "client": "test",
                "hostname": "host",
            }
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            value = routes.get(self.path.rstrip("/"))
            if value is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            assert self.path.rstrip("/") == "/api/0/query"
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            program = "\n".join(body["query"])
            period_start, period_end = body["timeperiods"][0].split("/", 1)
            result = execute_query(
                "cli-profile-report",
                program,
                datetime.fromisoformat(period_start),
                datetime.fromisoformat(period_end),
                datastore,
            )

            def json_value(value):
                if isinstance(value, Event):
                    return value.to_json_dict()
                if isinstance(value, timedelta):
                    return value.total_seconds()
                if isinstance(value, dict):
                    return {key: json_value(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [json_value(item) for item in value]
                return value

            payload = json.dumps([json_value(result)]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = CliRunner().invoke(
            main,
            [
                "--host",
                "127.0.0.1",
                "--port",
                str(server.server_port),
                "report",
                "host",
                "--start",
                "2026-01-01",
                "--stop",
                "2026-01-02",
            ],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert result.exit_code == 0, result.output
    assert "Meeting" in result.output
    assert "Total duration" in result.output
    assert "0:00:08" in result.output
    assert "Unavailable (profile has no app/title presentation source)" in result.output
