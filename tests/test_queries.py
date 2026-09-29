from datetime import datetime, timedelta, timezone
import inspect

import pytest
from aw_core.models import Event
from aw_datastore import Datastore
from aw_datastore.storages import MemoryStorage
from aw_query import query

from aw_client.legacy_v1 import canonical_events as legacy_canonical_events
from aw_client.legacy_v1 import full_desktop_query as legacy_full_desktop_query

from aw_client.queries import (
    CURRENT_QUERY_CAPABILITIES,
    _serialize_query_json,
    ActiveTimeSource,
    ActivityCoverageSource,
    ActivitySource,
    AndroidQueryParams,
    CanonicalQueryParamsV2,
    ContextSource,
    DesktopQueryParams,
    activityQuery,
    canonicalEvents,
    canonicalEventsV2,
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

    assert 'query_bucket_optional_raw("aw-watcher-win-vdesktop_test")' in query
    assert '"source_id":"vdesktop"' in query
    assert (
        'merge_subwatcher_fields(events, context_source_0, ["vdesktop"], '
        '{"source_id":"vdesktop","conflict":"base_wins"});'
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


@pytest.mark.parametrize("pipeline", ["legacy", "v2"])
def test_context_enrichment_preserves_latest_overlapping_source_event(pipeline):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=120)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    coverage = datastore.create_bucket(
        bucket_id=f"coverage-overlap-{pipeline}",
        type="activity",
        client="test",
        hostname="test",
        name="coverage",
    )
    context = datastore.create_bucket(
        bucket_id=f"context-overlap-{pipeline}",
        type="context",
        client="test",
        hostname="test",
        name="context",
    )
    coverage.insert(
        Event(
            timestamp=start + timedelta(seconds=30),
            duration=timedelta(seconds=10),
            data={"label": "base"},
        )
    )
    context.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=100),
            data={"name": "older-long"},
        )
    )
    context.insert(
        Event(
            timestamp=start + timedelta(seconds=20),
            duration=timedelta(seconds=40),
            data={"name": "newer-short"},
        )
    )

    coverage_source = ActivityCoverageSource(
        "coverage",
        [f"coverage-overlap-{pipeline}"],
        ["label"],
        scope="global",
    )
    context_source = ContextSource(
        "context",
        [f"context-overlap-{pipeline}"],
        ["name"],
        scope="global",
    )
    capabilities = ["query.merge_subwatcher_fields.source_namespace.v1"]
    if pipeline == "legacy":
        generated = canonicalEvents(
            DesktopQueryParams(
                filter_afk=False,
                activity_coverage_sources=[coverage_source],
                context_sources=[context_source],
                capabilities=capabilities,
            )
        )
    else:
        generated = canonicalEventsV2(
            CanonicalQueryParamsV2(
                activity_coverage_sources=[coverage_source],
                context_sources=[context_source],
                capabilities=capabilities,
                filter_afk=False,
            )
        )

    result = query(
        f"context-overlap-{pipeline}",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert "context_0 = filter_period_intersect(context_0, events);" not in generated
    assert len(result) == 1
    assert result[0].timestamp == start + timedelta(seconds=30)
    assert result[0].duration == timedelta(seconds=10)
    assert result[0].data["$source.coverage.label"] == "base"
    assert result[0].data["$source.context.name"] == "newer-short"


def test_combining_browser_family_buckets_before_projection_preserves_latest_event():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=120)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    buckets = {
        "coverage": [(30, 10, {"app": "Chrome"})],
        "browser-single": [
            (0, 100, {"url": "https://old.example"}),
            (20, 40, {"url": "https://new.example"}),
        ],
        "browser-old": [(0, 100, {"url": "https://old.example"})],
        "browser-new": [(20, 40, {"url": "https://new.example"})],
    }
    for bucket_id, events in buckets.items():
        bucket = datastore.create_bucket(
            bucket_id=bucket_id,
            type="test",
            client="test",
            hostname="test",
            name=bucket_id,
        )
        for offset, duration, data in events:
            bucket.insert(
                Event(
                    timestamp=start + timedelta(seconds=offset),
                    duration=timedelta(seconds=duration),
                    data=data,
                )
            )

    def run(bucket_ids):
        loads = "\n".join(
            f'browser = concat(browser, flood(query_bucket("{bucket_id}")));'
            for bucket_id in bucket_ids
        )
        generated = f'''events = query_bucket("coverage");
browser = [];
{loads}
browser = sort_by_timestamp(browser);
events = merge_subwatcher_fields(events, browser, ["url"]);
RETURN = events;'''
        return query("browser-family-precedence", generated, start, end, datastore)

    results = [
        run(["browser-single"]),
        run(["browser-old", "browser-new"]),
        run(["browser-new", "browser-old"]),
    ]
    for result in results:
        assert len(result) == 1
        assert result[0].timestamp == start + timedelta(seconds=30)
        assert result[0].duration == timedelta(seconds=10)
        assert result[0].data["url"] == "https://new.example"
    assert results[0] == results[1] == results[2]


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

    assert "context_source_0_raw = [];" in query
    assert "context_source_1_raw = [];" in query


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

    assert 'query_bucket_optional_raw("browser_laptop", "laptop")' in query_code
    assert 'query_bucket_optional_raw("browser_desktop")' not in query_code


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


def test_canonical_events_targets_current_v2_without_capability_hints():
    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window="aw-watcher-window_test",
            bid_afk="aw-watcher-afk_test",
            category_specs=[{"name": ["Work"], "rule": {"type": "none"}}],
        )
    )
    assert "query_bucket_optional_raw" in generated
    assert "events = categorize_v2" in generated


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
    query = legacy_canonical_events(
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

    assert "coverage = period_union(coverage, coverage_period_0)" in generated
    assert "coverage = period_union(coverage, coverage_period_1)" in generated
    assert '"source_id":"meeting"' in generated
    assert '"source_id":"desktop"' in generated
    assert "events = union_no_overlap(activity_coverage_source_" not in generated


def test_activity_coverage_rejects_duplicate_source_ids():
    with pytest.raises(ValueError, match="canonical fact source ids must be unique"):
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
    query = legacy_canonical_events(
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
    query = legacy_canonical_events(
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


@pytest.mark.parametrize(
    "rule",
    [
        {"type": "none"},
        {"type": "any", "rules": [{"type": "none"}]},
    ],
)
def test_source_free_none_rules_build_and_execute_across_native_entrypoints(rule):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    coverage = datastore.create_bucket(
        bucket_id="source-free-coverage",
        type="activity",
        client="test",
        hostname="test",
        name="coverage",
    )
    coverage.insert(
        Event(timestamp=start, duration=timedelta(seconds=20), data={"label": "base"})
    )
    coverage_source = ActivityCoverageSource(
        "coverage", ["source-free-coverage"], ["label"], scope="global"
    )
    capabilities = [
        "query.active_periods_v2.v1",
        "query.merge_subwatcher_fields.source_namespace.v1",
    ]
    generated_queries = [
        canonicalEventsV2(
            CanonicalQueryParamsV2(
                activity_coverage_sources=[coverage_source],
                active_time_rule=rule,
                capabilities=capabilities,
            )
        )
        + "\nRETURN = events;",
        canonicalEvents(
            DesktopQueryParams(
                filter_afk=True,
                activity_coverage_sources=[coverage_source],
                active_time_rule=rule,
                capabilities=capabilities,
            )
        )
        + "\nRETURN = events;",
    ]

    for index, generated in enumerate(generated_queries):
        assert "active_time_sources = [];" in generated
        assert query(
            f"source-free-none-{index}", generated, start, end, datastore
        ) == []


@pytest.mark.parametrize(
    "rule",
    [
        {
            "type": "any",
            "children": [
                {
                    "type": "regex",
                    "source": "missing",
                    "field": "status",
                    "regex": "active",
                    "host": "B",
                }
            ],
        },
        {
            "source": "missing",
            "field": "status",
            "regex": "active",
            "host": "B",
        },
    ],
)
def test_active_time_aliases_reject_unknown_sources_across_native_entrypoints(rule):
    capabilities = ["query.active_periods_v2.v1"]
    builders = [
        lambda: canonicalEventsV2(
            CanonicalQueryParamsV2(
                hostname="A",
                active_time_rule=rule,
                capabilities=capabilities,
            )
        ),
        lambda: canonicalEvents(
            DesktopQueryParams(
                hostname="A",
                active_time_rule=rule,
                capabilities=capabilities,
            )
        ),
    ]

    for builder in builders:
        with pytest.raises(ValueError, match="unknown source.*missing"):
            builder()


def test_legacy_expression_needs_no_window_or_afk_and_executes():
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

    generated = legacy_canonical_events(
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

    generated = legacy_canonical_events(
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

    generated = legacy_canonical_events(
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
    generated = legacy_canonical_events(
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
    generated = legacy_canonical_events(
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

    assert 'query_bucket_optional_raw("context-laptop", "laptop")' in generated
    assert 'query_bucket_optional_raw("context-global")' in generated


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
        legacy_canonical_events(DesktopQueryParams(**kwargs))


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
        legacy_canonical_events(DesktopQueryParams(**kwargs))


def test_source_scope_rejects_duplicates_and_conflicting_ownership():
    capabilities = sorted(CURRENT_QUERY_CAPABILITIES)
    with pytest.raises(ValueError, match="duplicate bucket_ids"):
        canonicalEventsV2(
            CanonicalQueryParamsV2(
                filter_afk=False,
                capabilities=capabilities,
                active_time_sources=[
                    ActiveTimeSource("duplicate", ["bucket", "bucket"], scope="global")
                ],
                active_time_rule={"type": "none"},
            )
        )

    with pytest.raises(ValueError, match="must not define host"):
        canonicalEventsV2(
            CanonicalQueryParamsV2(
                hostname="host",
                filter_afk=False,
                capabilities=capabilities,
                active_time_sources=[
                    ActiveTimeSource("conflict", ["bucket"], host="host", scope="global")
                ],
                active_time_rule={"type": "none"},
            )
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

    generated = legacy_canonical_events(
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


def test_canonical_events_v2_meeting_only_needs_no_window_or_afk():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    meeting = datastore.create_bucket(
        bucket_id="meeting-v2",
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

    generated = canonicalEventsV2(
        CanonicalQueryParamsV2(
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "meeting",
                    ["meeting-v2"],
                    ["provider", "subject"],
                    scope="global",
                )
            ],
            capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
            filter_afk=False,
        )
    )
    result = query(
        "canonical-v2-meeting-only",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert "find_bucket" not in generated
    assert len(result) == 1
    assert result[0].data == {
        "$source.meeting.provider": "Teams",
        "$source.meeting.subject": "Planning",
    }


def test_canonical_events_v2_stopwatch_label_creates_namespaced_coverage():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    stopwatch = datastore.create_bucket(
        bucket_id="stopwatch-v2",
        type="stopwatch",
        client="test",
        hostname="test",
        name="stopwatch",
    )
    stopwatch.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=15),
            data={"label": "Write release notes"},
        )
    )

    generated = canonicalEventsV2(
        CanonicalQueryParamsV2(
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "stopwatch",
                    ["stopwatch-v2"],
                    ["label"],
                    scope="global",
                )
            ],
            capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
            filter_afk=False,
        )
    )
    result = query(
        "canonical-v2-stopwatch",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert len(result) == 1
    assert result[0].data == {
        "$source.stopwatch.label": "Write release notes",
    }


def test_canonical_events_v2_keeps_configured_coverage_active():
    generated = canonicalEventsV2(
        CanonicalQueryParamsV2(
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "manual",
                    ["manual"],
                    ["label"],
                    scope="global",
                    keeps_active=True,
                )
            ],
            active_time_sources=[
                ActiveTimeSource("afk", ["afk"], scope="global")
            ],
            active_time_rule={
                "type": "regex",
                "source": "afk",
                "field": "status",
                "regex": "not-afk",
            },
            capabilities=[
                "query.merge_subwatcher_fields.source_namespace.v1",
                "query.active_periods_v2.v1",
            ],
        )
    )

    assert (
        "not_afk = period_union(not_afk, coverage_period_0);"
        in generated
    )
    override_index = generated.index(
        "not_afk = period_union(not_afk, coverage_period_0);"
    )
    normalize_index = generated.index("not_afk = period_union(not_afk, []);")
    mask_index = generated.index("events = filter_period_intersect(events, not_afk);")
    assert override_index < normalize_index < mask_index


def test_canonical_events_v2_ignores_unreferenced_currentwindow_bucket():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    window = datastore.create_bucket(
        bucket_id="unreferenced-window-v2",
        type="currentwindow",
        client="test",
        hostname="test",
        name="window",
    )
    meeting = datastore.create_bucket(
        bucket_id="referenced-meeting-v2",
        type="meeting",
        client="test",
        hostname="test",
        name="meeting",
    )
    window.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=40),
            data={"app": "Editor", "title": "Secret window title"},
        )
    )
    meeting.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=10),
            data={"subject": "Planning"},
        )
    )

    generated = canonicalEventsV2(
        CanonicalQueryParamsV2(
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "meeting",
                    ["referenced-meeting-v2"],
                    ["subject"],
                    scope="global",
                )
            ],
            capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
            filter_afk=False,
        )
    )
    result = query(
        "canonical-v2-unreferenced-window",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert "unreferenced-window-v2" not in generated
    assert len(result) == 1
    assert result[0].duration == timedelta(seconds=10)
    assert result[0].data == {"$source.meeting.subject": "Planning"}


def test_canonical_events_v2_explicit_window_source_is_namespaced_only():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    window = datastore.create_bucket(
        bucket_id="explicit-window-v2",
        type="currentwindow",
        client="test",
        hostname="test",
        name="window",
    )
    window.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"app": "Editor", "title": "Code"},
        )
    )

    generated = canonicalEventsV2(
        CanonicalQueryParamsV2(
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "window",
                    ["explicit-window-v2"],
                    ["app", "title"],
                    scope="global",
                )
            ],
            capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
            filter_afk=False,
        )
    )
    result = query(
        "canonical-v2-explicit-window",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert len(result) == 1
    assert result[0].data == {
        "$source.window.app": "Editor",
        "$source.window.title": "Code",
    }


@pytest.mark.parametrize("source_role", ["context", "active"])
def test_canonical_events_v2_non_coverage_sources_cannot_create_coverage(source_role):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    source = datastore.create_bucket(
        bucket_id=f"{source_role}-only-v2",
        type=source_role,
        client="test",
        hostname="test",
        name=source_role,
    )
    source.insert(
        Event(
            timestamp=start,
            duration=timedelta(seconds=20),
            data={"state": "present"},
        )
    )

    if source_role == "context":
        params = CanonicalQueryParamsV2(
            context_sources=[
                ContextSource(
                    "context",
                    ["context-only-v2"],
                    ["state"],
                    scope="global",
                )
            ],
            capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
            filter_afk=False,
        )
    else:
        params = CanonicalQueryParamsV2(
            active_time_sources=[
                ActiveTimeSource(
                    "active",
                    ["active-only-v2"],
                    scope="global",
                )
            ],
            active_time_rule={
                "type": "regex",
                "source": "active",
                "field": "state",
                "regex": "present",
            },
            capabilities=["query.active_periods_v2.v1"],
        )

    generated = canonicalEventsV2(params)
    result = query(
        f"canonical-v2-{source_role}-only",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert result == []


@pytest.mark.parametrize(
    "legacy_field",
    [
        "bid_window",
        "bid_afk",
        "bid_browsers",
        "bid_stopwatch",
        "always_active_pattern",
        "legacy_window_mode",
        "legacy_window_fields",
        "activity_sources",
        "background_sources",
    ],
)
def test_canonical_query_params_v2_rejects_legacy_fields(legacy_field):
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        CanonicalQueryParamsV2(**{legacy_field: object()})


def test_canonical_events_v2_does_not_accept_root_field_injection():
    parameter_names = set(inspect.signature(CanonicalQueryParamsV2).parameters)

    assert parameter_names == {
        "activity_coverage_sources",
        "active_time_sources",
        "active_time_rule",
        "context_sources",
        "category_specs",
        "hostname",
        "capabilities",
        "filter_afk",
        "filter_categories",
        "explain_categories",
    }
    with pytest.raises(TypeError, match="unexpected keyword argument 'bid_window'"):
        canonicalEventsV2(CanonicalQueryParamsV2(), bid_window="unrelated")


def test_canonical_events_v2_rejects_duplicate_fact_source_ids_across_roles():
    with pytest.raises(ValueError, match="unique across coverage and context"):
        canonicalEventsV2(
            CanonicalQueryParamsV2(
                activity_coverage_sources=[
                    ActivityCoverageSource(
                        "shared",
                        ["coverage"],
                        ["title"],
                        scope="global",
                    )
                ],
                context_sources=[
                    ContextSource(
                        "shared",
                        ["context"],
                        ["project"],
                        scope="global",
                    )
                ],
                capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
            )
        )


def test_canonical_events_v2_rejects_duplicate_active_time_source_ids():
    with pytest.raises(ValueError, match="active-time source ids must be unique"):
        canonicalEventsV2(
            CanonicalQueryParamsV2(
                active_time_sources=[
                    ActiveTimeSource("duplicate", ["one"], scope="global"),
                    ActiveTimeSource("duplicate", ["two"], scope="global"),
                ],
                active_time_rule={"type": "none"},
                capabilities=["query.active_periods_v2.v1"],
            )
        )


def test_canonical_events_v2_normalizes_empty_bucket_host_map():
    query_text = canonicalEventsV2(
        CanonicalQueryParamsV2(
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "meeting",
                    ["meeting"],
                    ["subject"],
                    bucket_hosts={},
                    scope="global",
                )
            ],
            capabilities=["query.merge_subwatcher_fields.source_namespace.v1"],
            filter_afk=False,
        )
    )

    assert 'query_bucket_optional_raw("meeting")' in query_text


def test_current_legacy_adapter_reuses_focused_browser_for_audible_activity():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=20)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    fixtures = [
        (
            "aw-watcher-window_imported",
            "currentwindow",
            {"app": "Google Chrome", "title": "Focused"},
        ),
        ("aw-watcher-afk_imported", "afkstatus", {"status": "afk"}),
        (
            "aw-watcher-web-chrome_imported",
            "web.tab.current",
            {"url": "https://example.test", "title": "Page", "audible": True},
        ),
    ]
    for bucket_id, bucket_type, data in fixtures:
        bucket = datastore.create_bucket(
            bucket_id=bucket_id,
            type=bucket_type,
            client="test",
            hostname="host-a",
            name=bucket_id,
        )
        bucket.insert(Event(timestamp=start, duration=end - start, data=data))

    capabilities = [*CURRENT_QUERY_CAPABILITIES, "query.categorize_v2.v1"]

    def run(include_audible):
        generated = canonicalEvents(
            DesktopQueryParams(
                bid_window="aw-watcher-window_",
                bid_afk="aw-watcher-afk_",
                bid_browsers=["aw-watcher-web-chrome_imported"],
                hostname="host-a",
                capabilities=capabilities,
                include_audible=include_audible,
            )
        )
        result = query(
            f"current-browser-audible-{include_audible}",
            generated + '\nRETURN = {"events": events, "browser": browser_events};',
            start,
            end,
            datastore,
        )
        return generated, result

    generated, included = run(True)
    _, excluded = run(False)

    assert "browser_resolved_chrome = merge_subwatcher_fields" in generated
    assert "active_source_1 = filter_period_intersect" not in generated
    assert generated.count('query_bucket_optional_raw("aw-watcher-web-chrome_imported"') == 1
    assert len(included["events"]) == 1
    assert included["events"][0].data["app"] == "Google Chrome"
    assert len(included["browser"]) == 1
    assert included["browser"][0].data["url"] == "https://example.test"
    assert excluded["events"] == []
    assert excluded["browser"] == []

    custom = canonicalEvents(
        DesktopQueryParams(
            bid_window="aw-watcher-window_",
            bid_browsers=["aw-watcher-web-chrome_imported"],
            hostname="host-a",
            capabilities=capabilities,
            active_time_rule={"type": "none"},
        )
    )
    assert '"field":"audible"' not in custom
    assert "browser_chrome = split_url_events(browser_resolved_chrome);" in custom


def test_current_legacy_browser_projection_is_bounded_to_query_period():
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    for bucket_id, bucket_type, data in [
        ("window", "currentwindow", {"app": "Google Chrome", "title": "Focused"}),
        ("afk", "afkstatus", {"status": "not-afk"}),
        ("aw-watcher-web-chrome", "web.tab.current", {"url": "https://example.test"}),
    ]:
        bucket = datastore.create_bucket(
            bucket_id=bucket_id,
            type=bucket_type,
            client="test",
            hostname="host-a",
            name=bucket_id,
        )
        bucket.insert(Event(timestamp=origin, duration=100, data=data))
    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window="window",
            bid_afk="afk",
            bid_browsers=["aw-watcher-web-chrome"],
            hostname="host-a",
            capabilities=[*CURRENT_QUERY_CAPABILITIES, "query.categorize_v2.v1"],
        )
    )
    result = query(
        "bounded-browser-projection",
        generated + "\nRETURN = [events, not_afk, browser_events];",
        origin + timedelta(seconds=20),
        origin + timedelta(seconds=30),
        datastore,
    )
    for stream in result:
        assert sum((event.duration for event in stream), timedelta()) == timedelta(seconds=10)
        assert stream[0].timestamp == origin + timedelta(seconds=20)


def test_current_legacy_adapter_rejects_always_active_without_window_projection():
    with pytest.raises(ValueError, match="always_active_pattern requires"):
        canonicalEvents(
            DesktopQueryParams(
                bid_window="window",
                bid_afk="afk",
                legacy_window_mode="none",
                always_active_pattern="meeting",
                capabilities=[*CURRENT_QUERY_CAPABILITIES, "query.categorize_v2.v1"],
            )
        )


@pytest.mark.parametrize(
    ("browser_events", "expected_duration"),
    [([], timedelta(0)), ([(5, 2)], timedelta(seconds=2)), (None, timedelta(0))],
)
def test_current_browser_projection_requires_browser_presence(browser_events, expected_duration):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=10)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    window = datastore.create_bucket(
        bucket_id="presence-window",
        type="currentwindow",
        client="test",
        hostname="host",
        name="window",
    )
    afk = datastore.create_bucket(
        bucket_id="presence-afk",
        type="afkstatus",
        client="test",
        hostname="host",
        name="afk",
    )
    window.insert(Event(timestamp=start, duration=10, data={"app": "Google Chrome", "title": "Focused"}))
    afk.insert(Event(timestamp=start, duration=10, data={"status": "not-afk"}))
    if browser_events is not None:
        browser = datastore.create_bucket(
            bucket_id="aw-watcher-web-chrome-presence",
            type="web.tab.current",
            client="test",
            hostname="host",
            name="browser",
        )
        for offset, duration in browser_events:
            browser.insert(
                Event(
                    timestamp=start + timedelta(seconds=offset),
                    duration=duration,
                    data={"url": "https://example.test/page", "title": "Page"},
                )
            )
    params = DesktopQueryParams(
        bid_window="presence-window",
        bid_afk="presence-afk",
        bid_browsers=["aw-watcher-web-chrome-presence"],
        hostname="host",
        include_audible=False,
    )
    generated = canonicalEvents(params)
    browser_result = query(
        "browser-presence-canonical",
        generated + '\nRETURN = {"events": browser_events, "duration": sum_durations(browser_events)};',
        start,
        end,
        datastore,
    )
    full_result = query(
        "browser-presence-full", fullDesktopQuery(params), start, end, datastore
    )["browser"]

    assert browser_result["duration"] == expected_duration
    assert full_result["duration"] == expected_duration
    if expected_duration:
        assert [event.data["url"] for event in full_result["urls"]] == [
            "https://example.test/page"
        ]
        assert all(event.data for event in full_result["urls"])
    else:
        assert browser_result["events"] == []
        assert full_result["urls"] == []
        assert full_result["domains"] == []


@pytest.mark.parametrize("meeting_present", [True, False])
def test_current_browser_projection_is_bounded_by_context_mode_activity_coverage(
    meeting_present,
):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=30)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    for bucket_id, bucket_type, duration, offset, data in [
        ("context-window", "currentwindow", 30, 0, {"app": "Google Chrome", "title": "Focused"}),
        ("context-afk", "afkstatus", 30, 0, {"status": "not-afk"}),
        ("aw-watcher-web-chrome-context", "web.tab.current", 30, 0, {"url": "https://example.test", "title": "Page"}),
    ]:
        bucket = datastore.create_bucket(
            bucket_id=bucket_id,
            type=bucket_type,
            client="test",
            hostname="host",
            name=bucket_id,
        )
        bucket.insert(
            Event(
                timestamp=start + timedelta(seconds=offset),
                duration=duration,
                data=data,
            )
        )
    if meeting_present:
        meeting = datastore.create_bucket(
            bucket_id="meeting",
            type="meeting",
            client="test",
            hostname="host",
            name="meeting",
        )
        meeting.insert(
            Event(
                timestamp=start + timedelta(seconds=10),
                duration=10,
                data={"subject": "Planning"},
            )
        )
    params = DesktopQueryParams(
        bid_window="context-window",
        bid_afk="context-afk",
        bid_browsers=["aw-watcher-web-chrome-context"],
        hostname="host",
        legacy_window_mode="context",
        activity_coverage_sources=[
            ActivityCoverageSource(
                "meeting",
                ["meeting"],
                ["subject"],
                scope="global",
                keeps_active=True,
            )
        ],
    )
    generated = canonicalEvents(params)
    result = query(
        "context-browser-coverage",
        generated
        + '\nRETURN = {"events": events, "active": not_afk, "browser": browser_events};',
        start,
        end,
        datastore,
    )
    full = query("context-browser-full", fullDesktopQuery(params), start, end, datastore)
    expected = timedelta(seconds=10 if meeting_present else 0)

    assert sum((event.duration for event in result["events"]), timedelta()) == expected
    assert sum((event.duration for event in result["active"]), timedelta()) == expected
    assert sum((event.duration for event in result["browser"]), timedelta()) == expected
    assert full["window"]["duration"] == expected
    assert full["browser"]["duration"] == expected


def test_current_adapter_groups_all_selected_buckets_in_a_browser_family():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=30)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    for bucket_id, bucket_type, data in [
        ("family-window", "currentwindow", {"app": "Google Chrome", "title": "Focused"}),
        ("family-afk", "afkstatus", {"status": "afk"}),
        ("aw-watcher-web-chrome-a", "web.tab.current", {"audible": True}),
        ("aw-watcher-web-chrome-z", "web.tab.current", {"audible": False}),
    ]:
        bucket = datastore.create_bucket(
            bucket_id=bucket_id,
            type=bucket_type,
            client="test",
            hostname="host",
            name=bucket_id,
        )
        bucket.insert(Event(timestamp=start, duration=30, data=data))
    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window="family-window",
            bid_afk="family-afk",
            bid_browsers=["aw-watcher-web-chrome-a", "aw-watcher-web-chrome-z"],
            hostname="host",
            include_audible=True,
        )
    )
    result = query(
        "browser-family-order",
        generated + '\nRETURN = {"events": events, "active": not_afk};',
        start,
        end,
        datastore,
    )

    assert generated.count("active_source_1_raw = concat") == 2
    assert result == {"events": [], "active": []}


def test_current_legacy_adapter_classifies_windowless_generic_coverage():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=20)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    bucket = datastore.create_bucket(
        bucket_id="meeting-imported",
        type="meeting",
        client="test",
        hostname="host-a",
        name="meeting",
    )
    bucket.insert(
        Event(
            timestamp=start,
            duration=end - start,
            data={"project": "Alpha", "subject": "Planning"},
        )
    )
    generated = canonicalEvents(
        DesktopQueryParams(
            hostname="host-a",
            filter_afk=False,
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "meeting",
                    ["meeting-imported"],
                    ["project", "subject"],
                    host="host-a",
                )
            ],
            category_specs=[
                {
                    "id": "work",
                    "name": ["Work"],
                    "rule": {
                        "type": "regex",
                        "source": "meeting",
                        "field": "project",
                        "regex": "^Alpha$",
                    },
                }
            ],
            filter_classes=[["Work"]],
            capabilities=[*CURRENT_QUERY_CAPABILITIES, "query.categorize_v2.v1"],
        )
    )
    result = query(
        "current-windowless-category",
        generated + "\nRETURN = events;",
        start,
        end,
        datastore,
    )

    assert len(result) == 1
    assert result[0].data["$category"] == ["Work"]
    assert result[0].data["$source.meeting.project"] == "Alpha"
    assert generated.index("categorize_v2") < generated.index("filter_keyvals")


def test_current_legacy_window_projection_restores_other_coverage_namespaces():
    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window="aw-watcher-window_",
            bid_afk="aw-watcher-afk_",
            hostname="host-a",
            filter_afk=False,
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "meeting",
                    ["meeting-imported"],
                    ["project"],
                    host="host-a",
                )
            ],
            capabilities=[*CURRENT_QUERY_CAPABILITIES, "query.categorize_v2.v1"],
        )
    )

    reset = generated.index("events = period_union([], events)")
    restored = generated.index(
        'merge_subwatcher_fields(events, coverage_source_1, ["project"], '
        '{"source_id":"meeting","conflict":"base_wins"})',
        reset,
    )
    assert reset < restored

    always_active = canonicalEvents(
        DesktopQueryParams(
            bid_window="aw-watcher-window_",
            bid_afk="aw-watcher-afk_",
            hostname="host-a",
            always_active_pattern="meeting",
            capabilities=[*CURRENT_QUERY_CAPABILITIES, "query.categorize_v2.v1"],
        )
    )
    assert always_active.count('find_bucket("aw-watcher-window_", "host-a")') == 1
    assert "active_source_1 = coverage_source_0;" in always_active


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

    generated = legacy_full_desktop_query(
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
    generated = legacy_canonical_events(
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

    generated = legacy_full_desktop_query(
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

    generated = legacy_canonical_events(
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

    generated = legacy_canonical_events(
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

    generated = legacy_canonical_events(
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

    generated = legacy_canonical_events(
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
    assert 'query_bucket_optional_raw("aw-watcher-web-chrome_\\"id"' in generated
    assert params.bid_window == 'window"id'
    assert params.bid_afk == 'afk"id'
    assert params.bid_browsers == ['aw-watcher-web-chrome_"id']


def test_background_sources_require_active_mask():
    with pytest.raises(ValueError, match="require bid_afk or an active-time"):
        legacy_canonical_events(
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


def test_desktop_builder_targets_current_server_without_capabilities():
    generated = canonicalEvents(
        DesktopQueryParams(
            bid_window="window",
            bid_afk="afk",
            filter_afk=False,
        )
    )

    assert "query_period()" in generated
    assert 'query_bucket_optional_raw(find_bucket("window")' in generated
    assert "events = legacy_activity;" not in generated


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
        'events = merge_subwatcher_fields(events, coverage_source_0, ["title","url"]);'
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
    assert 'query_bucket_optional_raw("presence")' in generated


def test_android_query_preserves_source_intervals_until_report_aggregation():
    generated = legacy_canonical_events(
        AndroidQueryParams(
            bid_android="android",
            filter_afk=False,
        )
    )

    assert "merge_events_by_keys" not in generated
