import logging
from datetime import datetime, timedelta, timezone

from aw_core.models import Event
from aw_datastore import Datastore
from aw_datastore.storages import MemoryStorage
from aw_query import query

from aw_client.cli import (
    _WINDOW_APP_FIELD,
    _WINDOW_TITLE_FIELD,
    _canonical_query_for_capabilities,
    _report_query,
    _source_qualified_category_specs,
)


SOURCE_ONLY_CAPABILITIES = [
    "query.active_periods_v2.v1",
    "query.categorize_v2.v1",
    "query.merge_subwatcher_fields.source_namespace.v1",
    "query.query_bucket_optional.expected_hostname.v1",
]


def test_cli_builds_source_only_canonical_query_when_supported():
    generated, uses_v2 = _canonical_query_for_capabilities(
        "laptop",
        [(["Work"], {"regex": "Editor", "select_keys": ["app"]})],
        SOURCE_ONLY_CAPABILITIES,
    )

    assert uses_v2
    assert "find_bucket" not in generated
    assert 'query_bucket_optional("aw-watcher-window_laptop", "laptop")' in generated
    assert 'query_bucket_optional("aw-watcher-afk_laptop", "laptop")' in generated
    assert '"source_id":"window"' in generated
    assert '"source":"window"' in generated
    assert '"source":"afk"' in generated
    assert "events = filter_period_intersect(events, not_afk);" in generated


def test_cli_legacy_classes_are_qualified_to_window_source():
    specs = _source_qualified_category_specs(
        [
            (["App"], {"regex": "Editor", "select_keys": ["app"]}),
            (["Never"], {"regex": ""}),
        ]
    )

    assert specs == [
        {
            "id": "cli-legacy-0",
            "name": ["App"],
            "rule": {
                "type": "regex",
                "regex": "Editor",
                "source": "window",
                "field": "app",
            },
        },
        {
            "id": "cli-legacy-1",
            "name": ["Never"],
            "rule": {"type": "none"},
        },
    ]


def test_cli_report_aggregates_namespaced_window_fields():
    generated = _report_query(
        "events = [];",
        _WINDOW_APP_FIELD,
        _WINDOW_TITLE_FIELD,
    )

    assert 'events, ["$source.window.app", "$source.window.title"]' in generated
    assert 'title_events, ["$source.window.app"]' in generated
    assert 'events, ["app", "title"]' not in generated


def test_cli_source_only_report_query_executes_with_namespaced_fields():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    window = datastore.create_bucket(
        bucket_id="aw-watcher-window_laptop",
        type="currentwindow",
        client="test",
        hostname="laptop",
        name="window",
    )
    afk = datastore.create_bucket(
        bucket_id="aw-watcher-afk_laptop",
        type="afkstatus",
        client="test",
        hostname="laptop",
        name="afk",
    )
    window.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"app": "Editor", "title": "Planning"},
        )
    )
    afk.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"status": "not-afk"},
        )
    )
    canonical_query, uses_v2 = _canonical_query_for_capabilities(
        "laptop",
        [(["Work"], {"regex": "Editor"})],
        SOURCE_ONLY_CAPABILITIES,
    )

    result = query(
        "cli-source-only-report",
        _report_query(canonical_query, _WINDOW_APP_FIELD, _WINDOW_TITLE_FIELD),
        start,
        end,
        datastore,
    )

    assert uses_v2
    assert result["window"]["duration"] == timedelta(seconds=20)
    assert result["window"]["title_events"][0].data == {
        "$source.window.app": "Editor",
        "$source.window.title": "Planning",
    }
    assert result["window"]["cat_events"][0].data == {"$category": ["Work"]}


def test_cli_warns_and_uses_legacy_builder_without_capabilities(caplog):
    with caplog.at_level(logging.WARNING):
        generated, uses_v2 = _canonical_query_for_capabilities(
            "laptop",
            [(["Work"], {"regex": "Editor"})],
            [],
        )

    assert not uses_v2
    assert 'find_bucket("aw-watcher-window_laptop")' in generated
    assert 'find_bucket("aw-watcher-afk_laptop")' in generated
    assert "categorize(events" in generated
    assert "legacy currentwindow/AFK compatibility fallback" in caplog.text
