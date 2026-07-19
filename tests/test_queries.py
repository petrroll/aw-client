from datetime import datetime, timedelta, timezone
import inspect

import pytest
from aw_core.models import Event
from aw_datastore import Datastore
from aw_datastore.storages import MemoryStorage
from aw_query import query

from aw_client.queries import (
    _serialize_query_json,
    ActiveTimeSource,
    ActivityCoverageSource,
    ActivitySource,
    AndroidQueryParams,
    ContextSource,
    DesktopQueryParams,
    activeTimeQuery,
    activityQuery,
    canonicalEvents,
    fullDesktopQuery,
    legacyActiveTimeQuery,
)


def test_query_json_serialization_preserves_regex_escapes():
    assert _serialize_query_json({"regex": r"\d+"}) == r'{"regex":"\d+"}'
    assert _serialize_query_json({"regex": r"a\"b"}) == r'{"regex":"a\\\"b"}'
    assert _serialize_query_json({"regex": r"path\\"}) == r'{"regex":"path\\"}'
    assert _serialize_query_json({"path\\name": "source\\field"}) == (
        r'{"path\name":"source\field"}'
    )
    assert _serialize_query_json({"category": "Личное"}) == '{"category":"Личное"}'
    with pytest.raises(ValueError, match="odd number of backslashes"):
        _serialize_query_json({"category": ["invalid\\"]})


def test_legacy_query_parameter_positional_order_is_preserved():
    desktop_names = list(inspect.signature(DesktopQueryParams).parameters)
    android_names = list(inspect.signature(AndroidQueryParams).parameters)

    assert desktop_names[:8] == [
        "bid_window",
        "bid_afk",
        "always_active_pattern",
        "bid_browsers",
        "classes",
        "filter_classes",
        "filter_afk",
        "include_audible",
    ]
    assert android_names[:6] == [
        "bid_android",
        "bid_browsers",
        "classes",
        "filter_classes",
        "filter_afk",
        "include_audible",
    ]


def test_canonical_events_uses_v2_categories_after_context_enrichment():
    query = canonicalEvents(
        DesktopQueryParams(
            bid_window="aw-watcher-window_test",
            bid_afk="aw-watcher-afk_test",
            capabilities=[
                "query.categorize_v2.v1",
                "query.merge_subwatcher_fields.source_namespace.v1",
            ],
            category_specs=[
                {
                    "id": "personal",
                    "name": ["Personal"],
                    "rule": {
                        "type": "regex",
                        "source": "vdesktop",
                        "field": "vdesktop",
                        "regex": "Personal",
                    },
                }
            ],
            context_sources=[
                ContextSource(
                    source_id="vdesktop",
                    bucket_ids=["aw-watcher-win-vdesktop_test"],
                    fields=["vdesktop"],
                    scope="global",
                )
            ],
        )
    )

    assert 'query_bucket_optional("aw-watcher-win-vdesktop_test")' in query
    assert '"source_id":"vdesktop"' in query
    assert 'context_fields_0 = ["vdesktop"];' in query
    assert (
        "merge_subwatcher_fields("
        "events, context_0, context_fields_0, context_options_0);"
    ) in query
    assert "events = categorize_v2(events" in query
    assert query.index("merge_subwatcher_fields") < query.index("categorize_v2")
    assert "events = categorize(events" not in query


def test_generated_canonical_query_executes_context_categorization():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    window_id = "aw-watcher-window_test"
    afk_id = "aw-watcher-afk_test"
    context_id = "aw-watcher-win-vdesktop_test"
    window = datastore.create_bucket(
        bucket_id=window_id,
        type="currentwindow",
        client="test",
        hostname="test",
        name="window",
    )
    afk = datastore.create_bucket(
        bucket_id=afk_id,
        type="afkstatus",
        client="test",
        hostname="test",
        name="afk",
    )
    context = datastore.create_bucket(
        bucket_id=context_id,
        type="vdesktop-name",
        client="test",
        hostname="test",
        name="virtual desktop",
    )
    window.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=30),
            data={"app": "devenv.exe", "title": "ActivityWatch"},
        )
    )
    afk.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=30),
            data={"status": "not-afk"},
        )
    )
    context.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=30),
            data={"vdesktop": "Personal-aw"},
        )
    )

    generated = canonicalEvents(
        DesktopQueryParams(
            hostname="test",
            bid_window=window_id,
            bid_afk=afk_id,
            capabilities=[
                "query.categorize_v2.v1",
                "query.merge_subwatcher_fields.source_namespace.v1",
            ],
            category_specs=[
                {
                    "id": "personal",
                    "name": ["Personal"],
                    "rule": {
                        "type": "regex",
                        "source": "vdesktop",
                        "field": "vdesktop",
                        "regex": "personal-aw",
                        "ignore_case": True,
                    },
                }
            ],
            context_sources=[
                ContextSource(
                    source_id="vdesktop",
                    bucket_ids=[context_id],
                    fields=["vdesktop"],
                    scope="global",
                )
            ],
        )
    )
    result = query(
        "generated-canonical-query",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert len(result) == 1
    assert result[0].data["$category"] == ["Personal"]


def test_canonical_events_keeps_legacy_categorization_by_default():
    query = canonicalEvents(
        DesktopQueryParams(
            bid_window="aw-watcher-window_test",
            bid_afk="aw-watcher-afk_test",
            classes=[
                (
                    ["Work"],
                    {"type": "regex", "regex": "devenv"},
                )
            ],
        )
    )

    assert "events = categorize(events" in query
    assert "categorize_v2" not in query


def test_context_sources_with_similar_ids_use_distinct_variables():
    query = canonicalEvents(
        DesktopQueryParams(
            bid_window="aw-watcher-window_test",
            bid_afk="aw-watcher-afk_test",
            capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
            context_sources=[
                ContextSource("a-b", ["bucket-a"], ["field"], scope="global"),
                ContextSource("a_b", ["bucket-b"], ["field"], scope="global"),
            ],
        )
    )

    assert "context_0 = [];" in query
    assert "context_1 = [];" in query


def test_context_sources_select_only_buckets_for_current_host():
    query_code = canonicalEvents(
        DesktopQueryParams(
            hostname="laptop",
            bid_window="aw-watcher-window_laptop",
            bid_afk="aw-watcher-afk_laptop",
            capabilities=[
                "query.categorize_v2.v1",
                "query.merge_subwatcher_fields.source_namespace.v1",
            ],
            category_specs=[],
            context_sources=[
                ContextSource(
                    source_id="browser",
                    bucket_ids=["browser_laptop", "browser_desktop"],
                    fields=["url"],
                    bucket_hosts={
                        "browser_laptop": "laptop",
                        "browser_desktop": "desktop",
                    },
                )
            ],
        )
    )

    assert 'query_bucket_optional("browser_laptop")' in query_code
    assert 'query_bucket_optional("browser_desktop")' not in query_code


def test_context_sources_reject_incomplete_bucket_host_mapping():
    with pytest.raises(ValueError, match="map every bucket exactly once"):
        canonicalEvents(
            DesktopQueryParams(
                hostname="laptop",
                bid_window="aw-watcher-window_laptop",
                bid_afk="aw-watcher-afk_laptop",
                capabilities=[
                    "query.categorize_v2.v1",
                    "query.merge_subwatcher_fields.source_namespace.v1",
                ],
                category_specs=[],
                context_sources=[
                    ContextSource(
                        source_id="browser",
                        bucket_ids=["browser_laptop", "browser_desktop"],
                        fields=["url"],
                        bucket_hosts={"browser_laptop": "laptop"},
                    )
                ],
            )
        )


def test_canonical_events_rejects_v2_without_server_capability():
    with pytest.raises(ValueError, match="query.categorize_v2.v1"):
        canonicalEvents(
            DesktopQueryParams(
                bid_window="aw-watcher-window_test",
                bid_afk="aw-watcher-afk_test",
                category_specs=[{"name": ["Work"], "rule": {"type": "none"}}],
            )
        )


def test_canonical_events_keeps_explicitly_empty_v2_categories():
    query = canonicalEvents(
        DesktopQueryParams(
            bid_window="aw-watcher-window_test",
            bid_afk="aw-watcher-afk_test",
            classes=[(["Legacy"], {"type": "regex", "regex": "legacy"})],
            category_specs=[],
            capabilities=["query.categorize_v2.v1"],
        )
    )

    assert "events = categorize_v2(events, []);" in query
    assert "events = categorize(events" not in query


def test_canonical_events_compiles_active_time_expressions():
    query = canonicalEvents(
        DesktopQueryParams(
            hostname="workstation",
            bid_window="aw-watcher-window_test",
            bid_afk="aw-watcher-afk_test",
            capabilities=["query.active_periods_v2.v1"],
            active_time_sources=[
                ActiveTimeSource("window", ["aw-watcher-window_test"], scope="global"),
                ActiveTimeSource("afk", ["aw-watcher-afk_test"], scope="global"),
            ],
            active_time_rule={
                "type": "all",
                "rules": [
                    {
                        "type": "regex",
                        "source": "window",
                        "field": "app",
                        "regex": "Teams",
                    },
                    {
                        "type": "regex",
                        "source": "afk",
                        "host": "workstation",
                        "field": "status",
                        "regex": "not-afk",
                    },
                ],
            },
        )
    )

    assert "not_afk = active_periods_v2(" in query
    assert '["window", active_source_0]' in query
    assert 'active_time_rule, "workstation")' in query
    assert "not_afk = filter_keyvals(not_afk" not in query
    assert query.index("active_periods_v2") < query.index(
        "filter_period_intersect(events, not_afk)"
    )


def test_canonical_events_compiles_replacement_activity_sources():
    query = canonicalEvents(
        DesktopQueryParams(
            bid_window="aw-watcher-window_test",
            bid_afk="aw-watcher-afk_test",
            capabilities=["query.map_event_fields.v1"],
            activity_sources=[
                ActivitySource("low", ["meeting-low"], scope="global"),
                ActivitySource(
                    "high",
                    ["meeting-high"],
                    {"app": "provider", "title": "subject"},
                    scope="global",
                ),
            ],
        )
    )

    assert (
        'map_event_fields(activity_source_1, {"app":"provider","title":"subject"})'
        in query
    )
    assert query.index("map_event_fields(activity_source_1") < query.index(
        "sort_by_timestamp(activity_source_1)"
    )
    assert query.index("sort_by_timestamp(activity_source_1)") < query.index(
        "events = union_no_overlap(activity_source_1, events)"
    )
    assert query.index(
        "events = union_no_overlap(activity_source_0, events)"
    ) < query.index("events = union_no_overlap(activity_source_1, events)")


def test_canonical_events_compiles_abstract_activity_coverage():
    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window=None,
            bid_afk=None,
            filter_afk=False,
            capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "meeting", ["meeting"], ["subject"], scope="global"
                ),
                ActivityCoverageSource(
                    "desktop", ["desktop"], ["vdesktop"], scope="global"
                ),
            ],
        )
    )

    assert "events = period_union(events, activity_coverage_period_0)" in generated
    assert "events = period_union(events, activity_coverage_period_1)" in generated
    assert '"source_id":"meeting"' in generated
    assert '"source_id":"desktop"' in generated
    assert "events = union_no_overlap(activity_coverage_source_" not in generated


def test_activity_coverage_rejects_duplicate_source_ids():
    with pytest.raises(ValueError, match="activity coverage source ids must be unique"):
        canonicalEvents(
            DesktopQueryParams(
                bid_window=None,
                bid_afk=None,
                filter_afk=False,
                capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
                activity_coverage_sources=[
                    ActivityCoverageSource(
                        "presence", ["meeting"], ["subject"], scope="global"
                    ),
                    ActivityCoverageSource(
                        "presence", ["desktop"], ["vdesktop"], scope="global"
                    ),
                ],
            )
        )


@pytest.mark.parametrize("filter_afk", [False, True])
def test_canonical_events_sorts_replacement_sources_before_union(filter_afk):
    query = canonicalEvents(
        DesktopQueryParams(
            bid_window="aw-watcher-window_test",
            bid_afk="aw-watcher-afk_test",
            filter_afk=filter_afk,
            capabilities=["query.map_event_fields.v1"],
            activity_sources=[
                ActivitySource("meeting", ["meeting-bucket"], scope="global")
            ],
        )
    )

    sort = "activity_source_0 = sort_by_timestamp(activity_source_0);"
    union = "events = union_no_overlap(activity_source_0, events);"
    assert sort in query
    assert query.index(sort) < query.index(union)


def test_canonical_events_passes_host_and_skips_other_host_sources():
    query = canonicalEvents(
        DesktopQueryParams(
            hostname="workstation",
            bid_window="aw-watcher-window_test",
            bid_afk="aw-watcher-afk_test",
            category_specs=[
                {
                    "name": ["Workstation"],
                    "rule": {
                        "type": "regex",
                        "host": "workstation",
                        "regex": "editor",
                    },
                }
            ],
            capabilities=[
                "query.categorize_v2.v1",
                "query.merge_subwatcher_fields.source_namespace.v1",
            ],
            context_sources=[
                ContextSource(
                    "other",
                    ["context-other"],
                    ["project"],
                    host="laptop",
                )
            ],
        )
    )

    assert ', "workstation");' in query
    assert "context-other" not in query


def test_canonical_events_passes_hostname_to_desktop_bucket_selection():
    query = canonicalEvents(
        DesktopQueryParams(
            hostname="workstation",
            bid_window="aw-watcher-window",
            bid_afk="aw-watcher-afk",
        )
    )

    assert 'find_bucket("aw-watcher-window", "workstation")' in query
    assert 'find_bucket("aw-watcher-afk", "workstation")' in query


def test_canonical_events_passes_hostname_to_android_bucket_selection():
    query = canonicalEvents(
        AndroidQueryParams(
            hostname="phone",
            bid_android="aw-watcher-android",
        )
    )

    assert 'find_bucket("aw-watcher-android", "phone")' in query


def test_legacy_active_time_query_needs_no_window_and_executes():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    afk = datastore.create_bucket(
        bucket_id="afk_test",
        type="afkstatus",
        client="test",
        hostname="test",
        name="afk",
    )
    afk.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=30),
            data={"status": "not-afk"},
        )
    )

    generated_queries = [
        activityQuery(["afk_test"]),
        legacyActiveTimeQuery("afk_test", hostname="test"),
    ]
    for index, generated in enumerate(generated_queries):
        result = query(
            f"legacy-active-only-{index}",
            generated,
            start,
            end,
            datastore,
        )

        assert "aw-watcher-window" not in generated
        assert len(result) == 1
        assert result[0].data["status"] == "not-afk"


def test_activity_query_serializes_bucket_ids():
    assert r'query_bucket("afk-\"quoted")' in activityQuery(['afk-"quoted'])
    with pytest.raises(ValueError, match="odd number of backslashes"):
        activityQuery(["afk\\"])


def test_active_time_expression_query_needs_no_window_or_afk_and_executes():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    activity = datastore.create_bucket(
        bucket_id="meeting_test",
        type="meeting",
        client="test",
        hostname="test",
        name="meeting",
    )
    activity.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"state": "active"},
        )
    )

    generated = activeTimeQuery(
        [ActiveTimeSource("meeting", ["meeting_test"], scope="global")],
        {
            "type": "regex",
            "source": "meeting",
            "field": "state",
            "regex": "active",
        },
    )
    result = query("active-expression-only", generated, start, end, datastore)

    assert "find_bucket" not in generated
    assert len(result) == 1
    assert result[0].duration == timedelta(seconds=20)


def test_standalone_active_time_query_can_enforce_bucket_hostname():
    generated = activeTimeQuery(
        [ActiveTimeSource("presence", ["presence-laptop"], host="laptop")],
        {"type": "none"},
        hostname="laptop",
        capabilities=["query.query_bucket_optional.expected_hostname.v1"],
    )

    assert 'query_bucket_optional("presence-laptop", "laptop")' in generated


def test_canonical_expression_needs_no_window_or_afk_and_executes():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    activity = datastore.create_bucket(
        bucket_id="focused_test",
        type="activity",
        client="test",
        hostname="test",
        name="focused",
    )
    activity.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"state": "active", "app": "Editor", "title": "Code"},
        )
    )

    generated = canonicalEvents(
        DesktopQueryParams(
            filter_afk=True,
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
                "query.active_periods_v2.v1",
            ],
            activity_sources=[
                ActivitySource("focused", ["focused_test"], scope="global")
            ],
            active_time_sources=[
                ActiveTimeSource("focused", ["focused_test"], scope="global")
            ],
            active_time_rule={
                "type": "regex",
                "source": "focused",
                "field": "state",
                "regex": "active",
            },
        )
    )
    result = query(
        "canonical-expression-no-legacy",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert "find_bucket" not in generated
    assert len(result) == 1
    assert result[0].data["app"] == "Editor"


def test_alternative_activity_source_needs_no_window_and_executes():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    meeting = datastore.create_bucket(
        bucket_id="meeting_test",
        type="meeting",
        client="test",
        hostname="test",
        name="meeting",
    )
    meeting.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"provider": "Teams", "subject": "Planning"},
        )
    )

    generated = canonicalEvents(
        DesktopQueryParams(
            filter_afk=False,
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
            ],
            activity_sources=[
                ActivitySource(
                    "meeting",
                    ["meeting_test"],
                    {"app": "provider", "title": "subject"},
                    scope="global",
                )
            ],
        )
    )
    result = query(
        "alternative-activity",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert "find_bucket" not in generated
    assert len(result) == 1
    assert result[0].data["app"] == "Teams"
    assert result[0].data["title"] == "Planning"


def test_missing_optional_activity_and_context_sources_execute_as_empty():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)

    generated = canonicalEvents(
        DesktopQueryParams(
            filter_afk=False,
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
                "query.merge_subwatcher_fields.source_namespace.v1",
            ],
            activity_sources=[
                ActivitySource("missing", ["missing-activity"], scope="global")
            ],
            context_sources=[
                ContextSource(
                    "missing",
                    ["missing-context"],
                    ["project"],
                    scope="global",
                )
            ],
        )
    )
    result = query(
        "missing-optional-sources",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert "query_bucket_optional" in generated
    assert result == []


def test_all_source_roles_respect_host_and_global_scope():
    generated = canonicalEvents(
        DesktopQueryParams(
            hostname="laptop",
            filter_afk=True,
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
                "query.merge_subwatcher_fields.source_namespace.v1",
                "query.active_periods_v2.v1",
            ],
            activity_sources=[
                ActivitySource(
                    "host-activity",
                    ["activity-laptop"],
                    host="laptop",
                ),
                ActivitySource(
                    "global-activity",
                    ["activity-global"],
                    scope="global",
                ),
            ],
            active_time_sources=[
                ActiveTimeSource(
                    "host-active",
                    ["active-other"],
                    host="desktop",
                ),
                ActiveTimeSource("global-active", ["active-global"], scope="global"),
            ],
            active_time_rule={
                "type": "regex",
                "source": "global-active",
                "field": "status",
                "regex": "active",
            },
            context_sources=[
                ContextSource(
                    "host-context",
                    ["context-other"],
                    ["project"],
                    host="desktop",
                ),
                ContextSource(
                    "global-context",
                    ["context-global"],
                    ["project"],
                    scope="global",
                ),
            ],
        )
    )

    assert "activity-laptop" in generated
    assert "activity-global" in generated
    assert "active-other" not in generated
    assert '["host-active", active_source_0]' in generated
    assert "active-global" in generated
    assert "context-other" not in generated
    assert "context-global" in generated


def test_bucket_hosts_partition_applies_to_all_source_roles():
    generated = canonicalEvents(
        DesktopQueryParams(
            hostname="laptop",
            filter_afk=True,
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
                "query.merge_subwatcher_fields.source_namespace.v1",
                "query.active_periods_v2.v1",
            ],
            activity_sources=[
                ActivitySource(
                    "activity",
                    ["activity-laptop", "activity-desktop"],
                    bucket_hosts={
                        "activity-laptop": "laptop",
                        "activity-desktop": "desktop",
                    },
                    scope="host",
                )
            ],
            active_time_sources=[
                ActiveTimeSource(
                    "active",
                    ["active-laptop", "active-desktop"],
                    bucket_hosts={
                        "active-laptop": "laptop",
                        "active-desktop": "desktop",
                    },
                    scope="host",
                )
            ],
            active_time_rule={
                "type": "regex",
                "source": "active",
                "field": "status",
                "regex": "active",
            },
            context_sources=[
                ContextSource(
                    "context",
                    ["context-laptop", "context-desktop"],
                    ["project"],
                    bucket_hosts={
                        "context-laptop": "laptop",
                        "context-desktop": "desktop",
                    },
                    scope="host",
                )
            ],
        )
    )

    assert "activity-laptop" in generated
    assert "active-laptop" in generated
    assert "context-laptop" in generated
    assert "activity-desktop" not in generated
    assert "active-desktop" not in generated
    assert "context-desktop" not in generated


def test_host_scoped_sources_emit_server_hostname_check_when_supported():
    generated = canonicalEvents(
        DesktopQueryParams(
            hostname="laptop",
            capabilities=[
                "query.merge_subwatcher_fields.source_namespace.v1",
                "query.query_bucket_optional.expected_hostname.v1",
            ],
            context_sources=[
                ContextSource(
                    "host-context",
                    ["context-laptop"],
                    ["project"],
                    host="laptop",
                ),
                ContextSource(
                    "global-context",
                    ["context-global"],
                    ["project"],
                    scope="global",
                ),
            ],
        )
    )

    assert 'query_bucket_optional("context-laptop", "laptop")' in generated
    assert 'query_bucket_optional("context-global")' in generated


@pytest.mark.parametrize(
    "source,capabilities",
    [
        (
            ActivitySource("source", ["bucket"]),
            ["query.map_event_fields.v1"],
        ),
        (
            ActiveTimeSource("source", ["bucket"]),
            ["query.active_periods_v2.v1"],
        ),
        (
            ContextSource("source", ["bucket"], ["field"]),
            ["query.merge_subwatcher_fields.source_namespace.v1"],
        ),
    ],
)
def test_source_roles_reject_missing_scope_and_ownership(source, capabilities):
    kwargs: dict = {
        "filter_afk": False,
        "category_specs": [],
        "capabilities": ["query.categorize_v2.v1", *capabilities],
    }
    if isinstance(source, ActivitySource):
        kwargs["activity_sources"] = [source]
    elif isinstance(source, ActiveTimeSource):
        kwargs["active_time_sources"] = [source]
        kwargs["active_time_rule"] = {
            "type": "regex",
            "source": "source",
            "field": "status",
            "regex": "active",
        }
    else:
        kwargs["context_sources"] = [source]

    with pytest.raises(ValueError, match="scope is required"):
        canonicalEvents(DesktopQueryParams(**kwargs))


@pytest.mark.parametrize(
    "source",
    [
        ActivitySource(
            "source",
            ["one", "two"],
            bucket_hosts={"one": "host"},
            scope="host",
        ),
        ActiveTimeSource(
            "source",
            ["one", "two"],
            bucket_hosts={"one": "host"},
            scope="host",
        ),
        ContextSource(
            "source",
            ["one", "two"],
            ["field"],
            bucket_hosts={"one": "host"},
            scope="host",
        ),
    ],
)
def test_source_roles_reject_incomplete_bucket_hosts(source):
    kwargs: dict = {
        "hostname": "host",
        "filter_afk": False,
        "category_specs": [],
        "capabilities": [
            "query.categorize_v2.v1",
            "query.map_event_fields.v1",
            "query.active_periods_v2.v1",
            "query.merge_subwatcher_fields.source_namespace.v1",
        ],
    }
    if isinstance(source, ActivitySource):
        kwargs["activity_sources"] = [source]
    elif isinstance(source, ActiveTimeSource):
        kwargs["active_time_sources"] = [source]
        kwargs["active_time_rule"] = {"type": "none"}
    else:
        kwargs["context_sources"] = [source]

    with pytest.raises(ValueError, match="map every bucket exactly once"):
        canonicalEvents(DesktopQueryParams(**kwargs))


def test_source_scope_rejects_duplicates_and_conflicting_ownership():
    with pytest.raises(ValueError, match="duplicate bucket_ids"):
        activeTimeQuery(
            [
                ActiveTimeSource(
                    "duplicate",
                    ["bucket", "bucket"],
                    scope="global",
                )
            ],
            {"type": "none"},
        )

    with pytest.raises(ValueError, match="must not define host"):
        activeTimeQuery(
            [
                ActiveTimeSource(
                    "conflict",
                    ["bucket"],
                    host="host",
                    scope="global",
                )
            ],
            {"type": "none"},
            hostname="host",
        )


def test_activity_source_priority_preserves_union_no_overlap_precedence():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    low = datastore.create_bucket(
        bucket_id="low",
        type="activity",
        client="test",
        hostname="test",
        name="low",
    )
    high = datastore.create_bucket(
        bucket_id="high",
        type="activity",
        client="test",
        hostname="test",
        name="high",
    )
    low.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=30),
            data={"priority": "low"},
        )
    )
    high.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=10),
            data={"priority": "high"},
        )
    )

    generated = canonicalEvents(
        DesktopQueryParams(
            filter_afk=False,
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
            ],
            activity_sources=[
                ActivitySource("low", ["low"], scope="global"),
                ActivitySource("high", ["high"], scope="global"),
            ],
        )
    )
    result = query(
        "activity-priority",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert result[0].data["priority"] == "high"
    assert result[0].duration == timedelta(seconds=10)
    assert result[1].data["priority"] == "low"
    assert result[1].duration == timedelta(seconds=20)


def test_activity_coverage_keeps_all_overlapping_source_data_for_categories():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    meeting = datastore.create_bucket(
        bucket_id="meeting",
        type="meeting",
        client="test",
        hostname="test",
        name="meeting",
    )
    desktop = datastore.create_bucket(
        bucket_id="desktop",
        type="vdesktop",
        client="test",
        hostname="test",
        name="desktop",
    )
    meeting.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=30),
            data={"subject": "Planning"},
        )
    )
    desktop.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=10),
            data={"vdesktop": "Work"},
        )
    )

    generated = canonicalEvents(
        DesktopQueryParams(
            filter_afk=False,
            category_specs=[
                {
                    "id": "work",
                    "name": ["Work"],
                    "rule": {
                        "type": "regex",
                        "source": "desktop",
                        "field": "vdesktop",
                        "regex": "^Work$",
                    },
                }
            ],
            capabilities=[
                "query.categorize_v2.v1",
                "query.merge_subwatcher_fields.source_namespace.v1",
            ],
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "meeting", ["meeting"], ["subject"], scope="global"
                ),
                ActivityCoverageSource(
                    "desktop", ["desktop"], ["vdesktop"], scope="global"
                ),
            ],
        )
    )
    result = query(
        "activity-coverage",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert result[0].duration == timedelta(seconds=10)
    assert result[0].data["$source.meeting.subject"] == "Planning"
    assert result[0].data["$source.desktop.vdesktop"] == "Work"
    assert result[0].data["$category"] == ["Work"]
    assert result[1].duration == timedelta(seconds=20)
    assert result[1].data["$source.meeting.subject"] == "Planning"
    assert "$source.desktop.vdesktop" not in result[1].data


def test_full_desktop_query_accepts_alternative_activity_without_window():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    activity = datastore.create_bucket(
        bucket_id="meeting_test",
        type="meeting",
        client="test",
        hostname="test",
        name="meeting",
    )
    afk = datastore.create_bucket(
        bucket_id="afk_test",
        type="afkstatus",
        client="test",
        hostname="test",
        name="afk",
    )
    activity.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"app": "Teams", "title": "Planning"},
        )
    )
    afk.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"status": "not-afk"},
        )
    )

    generated = fullDesktopQuery(
        DesktopQueryParams(
            bid_afk="afk_test",
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
            ],
            activity_sources=[
                ActivitySource("meeting", ["meeting_test"], scope="global")
            ],
        )
    )
    result = query(
        "full-alternative-activity",
        generated,
        start,
        end,
        datastore,
    )

    assert "legacy_activity" not in generated
    assert len(result["events"]) == 1
    assert result["events"][0].data["app"] == "Teams"


def test_full_desktop_query_accepts_custom_active_rule_without_afk():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    window = datastore.create_bucket(
        bucket_id="window_test",
        type="currentwindow",
        client="test",
        hostname="test",
        name="window",
    )
    signal = datastore.create_bucket(
        bucket_id="signal_test",
        type="presence",
        client="test",
        hostname="test",
        name="signal",
    )
    window.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"app": "Editor", "title": "Code"},
        )
    )
    signal.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"state": "present"},
        )
    )

    generated = fullDesktopQuery(
        DesktopQueryParams(
            bid_window="window_test",
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.active_periods_v2.v1",
            ],
            active_time_sources=[
                ActiveTimeSource("presence", ["signal_test"], scope="global")
            ],
            active_time_rule={
                "type": "regex",
                "source": "presence",
                "field": "state",
                "regex": "present",
            },
        )
    )
    result = query(
        "full-custom-active",
        generated,
        start,
        end,
        datastore,
    )

    assert "aw-watcher-afk" not in generated
    assert len(result["events"]) == 1
    assert result["events"][0].data["app"] == "Editor"
    assert len(result["window"]["active_events"]) == 1


def test_canonical_events_compiles_background_sources_after_activity_mask():
    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window="window",
            bid_afk="afk",
            always_active_pattern="meeting",
            capabilities=["query.map_event_fields.v1"],
            activity_sources=[
                ActivitySource("replacement", ["replacement"], scope="global")
            ],
            background_sources=[
                ActivitySource(
                    "background",
                    ["background-one", "background-two"],
                    {"app": "provider"},
                    scope="global",
                )
            ],
        )
    )

    replacement = "events = union_no_overlap(activity_source_0, events);"
    mask = "events = filter_period_intersect(events, not_afk);"
    fill = "events = union_no_overlap(events, background_source_0);"
    assert generated.index(replacement) < generated.index("not_treat_as_afk")
    assert generated.index(mask) < generated.index("background_source_0 = [];")
    assert generated.index("background_bucket_0_0") < generated.index(
        "background_bucket_0_1"
    )
    assert (
        "background_source_0 = filter_period_intersect(background_source_0, not_afk);"
    ) in generated
    assert 'map_event_fields(background_source_0, {"app":"provider"})' in generated
    assert generated.index(mask) < generated.index(fill)


def test_background_source_without_window_uses_active_time_rule():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    background = datastore.create_bucket(
        bucket_id="background",
        type="activity",
        client="test",
        hostname="test",
        name="background",
    )
    active = datastore.create_bucket(
        bucket_id="active",
        type="presence",
        client="test",
        hostname="test",
        name="active",
    )
    background.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=30),
            data={"provider": "Teams", "title": "Planning"},
        )
    )
    active.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=30),
            data={"state": "present"},
        )
    )

    generated = fullDesktopQuery(
        DesktopQueryParams(
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
                "query.active_periods_v2.v1",
            ],
            active_time_sources=[
                ActiveTimeSource("presence", ["active"], scope="global")
            ],
            active_time_rule={
                "type": "regex",
                "source": "presence",
                "field": "state",
                "regex": "present",
            },
            background_sources=[
                ActivitySource(
                    "meeting",
                    ["background"],
                    {"app": "provider"},
                    scope="global",
                )
            ],
        )
    )
    result = query(
        "background-no-window",
        generated,
        start,
        end,
        datastore,
    )

    assert len(result["events"]) == 1
    assert result["events"][0].duration == timedelta(seconds=30)
    assert result["events"][0].data["app"] == "Teams"


def test_window_activity_wins_overlapping_background_source():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    window = datastore.create_bucket(
        bucket_id="window",
        type="currentwindow",
        client="test",
        hostname="test",
        name="window",
    )
    afk = datastore.create_bucket(
        bucket_id="afk",
        type="afkstatus",
        client="test",
        hostname="test",
        name="afk",
    )
    background = datastore.create_bucket(
        bucket_id="background",
        type="activity",
        client="test",
        hostname="test",
        name="background",
    )
    window.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=10),
            data={"app": "Editor", "title": "Code"},
        )
    )
    afk.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"status": "not-afk"},
        )
    )
    background.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"app": "Teams", "title": "Planning"},
        )
    )

    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window="window",
            bid_afk="afk",
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
            ],
            background_sources=[
                ActivitySource("meeting", ["background"], scope="global")
            ],
        )
    )
    result = query(
        "background-window-wins",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert [(event.data["app"], event.duration) for event in result] == [
        ("Editor", timedelta(seconds=10)),
        ("Teams", timedelta(seconds=10)),
    ]


def test_background_source_is_clipped_to_active_mask():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    afk = datastore.create_bucket(
        bucket_id="afk",
        type="afkstatus",
        client="test",
        hostname="test",
        name="afk",
    )
    background = datastore.create_bucket(
        bucket_id="background",
        type="activity",
        client="test",
        hostname="test",
        name="background",
    )
    afk.insert(
        Event(
            timestamp=start + timedelta(seconds=5),
            duration=timedelta(seconds=10),
            data={"status": "not-afk"},
        )
    )
    background.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=30),
            data={"app": "Music", "title": "Album"},
        )
    )

    generated = canonicalEvents(
        DesktopQueryParams(
            bid_afk="afk",
            filter_afk=False,
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
            ],
            background_sources=[
                ActivitySource("music", ["background"], scope="global")
            ],
        )
    )
    result = query(
        "background-active-mask",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert len(result) == 1
    assert result[0].timestamp == start + timedelta(seconds=5)
    assert result[0].duration == timedelta(seconds=10)


def test_background_source_host_mismatch_is_excluded():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    active = datastore.create_bucket(
        bucket_id="active",
        type="presence",
        client="test",
        hostname="laptop",
        name="active",
    )
    active.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=30),
            data={"state": "present"},
        )
    )

    generated = canonicalEvents(
        DesktopQueryParams(
            hostname="laptop",
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
                "query.active_periods_v2.v1",
            ],
            active_time_sources=[
                ActiveTimeSource("presence", ["active"], scope="global")
            ],
            active_time_rule={
                "type": "regex",
                "source": "presence",
                "field": "state",
                "regex": "present",
            },
            background_sources=[
                ActivitySource(
                    "other-host",
                    ["background-other"],
                    host="desktop",
                )
            ],
        )
    )
    result = query(
        "background-host-mismatch",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert "background-other" not in generated
    assert result == []


def test_missing_optional_background_bucket_executes_as_empty():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    afk = datastore.create_bucket(
        bucket_id="afk",
        type="afkstatus",
        client="test",
        hostname="test",
        name="afk",
    )
    afk.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=30),
            data={"status": "not-afk"},
        )
    )

    generated = canonicalEvents(
        DesktopQueryParams(
            bid_afk="afk",
            category_specs=[],
            capabilities=[
                "query.categorize_v2.v1",
                "query.map_event_fields.v1",
            ],
            background_sources=[
                ActivitySource("missing", ["missing-background"], scope="global")
            ],
        )
    )
    result = query(
        "missing-background",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert 'query_bucket_optional("missing-background")' in generated
    assert result == []


def test_full_desktop_query_serializes_quoted_bucket_ids_without_mutating_params():
    params = DesktopQueryParams(
        bid_window='window"id',
        bid_afk='afk"id',
        bid_browsers=['aw-watcher-web-chrome_"id'],
        filter_afk=False,
    )

    generated = fullDesktopQuery(params)

    assert 'find_bucket("window\\"id")' in generated
    assert 'query_bucket("aw-watcher-web-chrome_\\"id")' in generated
    assert params.bid_window == 'window"id'
    assert params.bid_afk == 'afk"id'
    assert params.bid_browsers == ['aw-watcher-web-chrome_"id']


def test_background_sources_require_active_mask():
    with pytest.raises(ValueError, match="require bid_afk or an active-time"):
        canonicalEvents(
            DesktopQueryParams(
                filter_afk=False,
                category_specs=[],
                capabilities=[
                    "query.categorize_v2.v1",
                    "query.map_event_fields.v1",
                ],
                background_sources=[
                    ActivitySource("background", ["background"], scope="global")
                ],
            )
        )


def test_legacy_window_query_avoids_new_server_functions_without_capabilities():
    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window="window",
            bid_afk="afk",
            filter_afk=False,
        )
    )

    assert "events = legacy_activity;" in generated
    assert "merge_subwatcher_fields" not in generated
    assert "legacy_activity_period" not in generated


def test_legacy_window_projects_configured_fields():
    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window="window",
            filter_afk=False,
            capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
            legacy_window_fields=["title", "url"],
        )
    )

    assert (
        'events = merge_subwatcher_fields(events, legacy_activity, ["title","url"]);'
        in generated
    )


def test_bucket_ids_use_query2_serialization_and_reject_odd_trailing_backslashes():
    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window=r"window\bucket",
            filter_afk=False,
        )
    )

    assert r'find_bucket("window\bucket")' in generated
    with pytest.raises(ValueError, match="odd number of backslashes"):
        canonicalEvents(
            DesktopQueryParams(
                bid_window="window\\",
                filter_afk=False,
            )
        )


def test_explicit_advanced_activity_can_disable_legacy_window_loading():
    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window="window",
            bid_afk="afk",
            filter_afk=False,
            legacy_window_mode="none",
            capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "presence",
                    ["presence"],
                    ["state"],
                    scope="global",
                )
            ],
        )
    )

    assert "legacy_activity" not in generated
    assert 'query_bucket_optional("presence")' in generated


def test_android_query_preserves_source_intervals_until_report_aggregation():
    generated = canonicalEvents(
        AndroidQueryParams(
            bid_android="android",
            filter_afk=False,
        )
    )

    assert "merge_events_by_keys" not in generated
