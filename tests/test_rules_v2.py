from datetime import datetime, timedelta, timezone

import pytest

from aw_core.models import Event
from aw_datastore import Datastore
from aw_datastore.storages import MemoryStorage
from aw_query import query

from aw_client.queries import CURRENT_QUERY_CAPABILITIES
from aw_client.rules_v2 import (
    compile_profile_v2,
    flatten_category_sets,
    flattened_rule_id,
    load_rules_document,
    migrate_legacy_settings,
)


CAPABILITIES = sorted(
    CURRENT_QUERY_CAPABILITIES
    | {
        "query.categorize_v2.v1",
        "query.categorize_v2_explain.v1",
        "settings.rules_v2.v1",
    }
)


def _bucket(bucket_id, event_type, host="host"):
    return {
        "id": bucket_id,
        "type": event_type,
        "client": "test",
        "hostname": host,
    }


def test_multiset_flattening_has_stable_ids_priority_and_prerequisites():
    profile = {"category_set_ids": ["one", "two"]}
    sets = [
        {
            "id": "one",
            "categories": [
                {
                    "id": "child",
                    "name": ["Child"],
                    "requires": ["parent"],
                    "rule": {"type": "none"},
                },
                {"id": "parent", "name": ["Parent"], "rule": {"type": "none"}},
            ],
        },
        {
            "id": "two",
            "categories": [
                {"id": "parent", "name": ["Other"], "rule": {"type": "none"}}
            ],
        },
    ]
    flattened = flatten_category_sets(profile, sets)
    assert flattened[0]["id"] == flattened_rule_id("one", "child")
    assert flattened[0]["requires"] == [flattened_rule_id("one", "parent")]
    assert flattened[0]["set_priority"] == 2
    assert flattened[2]["id"] == flattened_rule_id("two", "parent")
    assert flattened[2]["set_priority"] == 1


def test_absent_settings_use_webui_defaults_but_explicit_empty_classes_stay_empty():
    defaults = migrate_legacy_settings({})
    names = [item["name"] for item in defaults["category_sets_v2"][0]["categories"]]
    assert ["Work", "Programming"] in names
    assert ["Media"] in names
    assert ["Comms"] in names
    assert ["Uncategorized"] in names
    assert next(
        item
        for item in defaults["category_sets_v2"][0]["categories"]
        if item["name"] == ["Work"]
    )["data"] == {"color": "#0F0", "score": 10}

    empty = migrate_legacy_settings({"classes": []})
    assert empty["category_sets_v2"][0]["categories"] == []


def test_legacy_category_sets_preserve_inactive_sets_selection_and_null_rules():
    migrated = migrate_legacy_settings(
        {
            "classes": [{"name": ["Stale"], "rule": {"type": "regex", "regex": "x"}}],
            "category_sets": [
                {
                    "id": "work",
                    "categories": [
                        {"name": ["Work"], "rule": {"type": None}, "data": {"color": "green"}}
                    ],
                },
                {
                    "id": "personal",
                    "categories": [
                        {"name": ["Personal"], "rule": {"type": "regex", "regex": "game"}}
                    ],
                },
            ],
            "active_set_ids": ["personal"],
        }
    )
    assert [item["id"] for item in migrated["category_sets_v2"]] == ["work", "personal"]
    assert migrated["activity_profiles_v2"][0]["category_set_ids"] == ["personal"]
    assert migrated["category_sets_v2"][0]["categories"][0]["rule"] == {"type": "none"}
    assert migrated["category_sets_v2"][0]["categories"][0]["data"] == {"color": "green"}


def test_multiple_legacy_active_sets_preserve_sets_and_deduplicate_selection():
    migrated = migrate_legacy_settings(
        {
            "category_sets": [
                {"id": "one", "categories": [{"name": ["Shared"], "rule": {"type": "none"}}]},
                {
                    "id": "two",
                    "categories": [
                        {"name": ["Shared"], "rule": {"type": "regex", "regex": "later"}},
                        {"name": ["Two"], "rule": {"type": "none"}},
                    ],
                },
            ],
            "active_set_ids": ["one", "two", "one", "missing"],
        }
    )
    assert [item["id"] for item in migrated["category_sets_v2"]] == ["one", "two"]
    assert migrated["activity_profiles_v2"][0]["category_set_ids"] == ["one", "two"]
    assert migrated["category_sets_v2"][0]["categories"][0]["rule"] == {"type": "none"}
    assert migrated["category_sets_v2"][1]["categories"][0]["rule"]["regex"] == "later"


def test_legacy_null_rule_discards_a_stale_regex():
    migrated = migrate_legacy_settings(
        {"classes": [{"name": ["Disabled"], "rule": {"type": None, "regex": "vim"}}]}
    )
    assert migrated["category_sets_v2"][0]["categories"][0]["rule"] == {"type": "none"}


def test_malformed_pre_atomic_v2_does_not_fall_back_to_lossy_classes():
    try:
        migrate_legacy_settings(
            {
                "activity_profiles_v2": [{"id": "advanced"}],
                "category_sets_v2": [{"id": "advanced-set"}],
                "classes": [{"id": "lossy", "name": ["Wrong"], "rule": {"type": "none"}}],
            }
        )
    except ValueError as error:
        assert "schema_version" in str(error)
    else:
        raise AssertionError("present malformed v2 arrays must not fall back to classes")


def test_stored_meeting_only_profile_is_source_only_exact_and_keeps_active():
    document = {
        "revision": 4,
        "activity_profiles_v2": [
            {
                "schema_version": 2,
                "id": "meeting-only",
                "category_set_ids": ["empty"],
                "sources": [
                    {
                        "id": "meeting",
                        "label": "Meetings",
                        "bucket_ids": ["meeting-bucket"],
                        "scope": "global",
                        "fields": ["subject"],
                        "creates_activity": True,
                        "keeps_active": True,
                    }
                ],
                "active_time": {"type": "expression", "rule": {"type": "none"}},
            }
        ],
        "category_sets_v2": [{"schema_version": 2, "id": "empty", "categories": []}],
    }
    materialized = compile_profile_v2(
        document,
        {"meeting-bucket": _bucket("meeting-bucket", "meeting", "other")},
        CAPABILITIES,
        hostname="host",
        profile_id="meeting-only",
    )
    query = materialized.query()
    assert 'query_bucket_optional_raw("meeting-bucket")' in query
    assert "flood_v2(coverage_source_0_raw)" not in query
    assert "not_afk = period_union(not_afk, coverage_period_0)" in query
    assert "not_afk = filter_period_intersect(not_afk, coverage)" in query
    assert "aw-watcher-window" not in query


def test_builtin_policies_and_none_alias_masks_are_explicit():
    document = {
        "revision": 1,
        "activity_profiles_v2": [
            {
                "schema_version": 2,
                "id": "default",
                "category_set_ids": ["empty"],
                "sources": [
                    {
                        "id": "builtin_window",
                        "label": "Window",
                        "builtin": "window",
                        "bucket_ids": [],
                        "fields": ["app", "title"],
                        "creates_activity": True,
                    },
                    {
                        "id": "stopwatch",
                        "label": "Stopwatch",
                        "builtin": "stopwatch",
                        "bucket_ids": [],
                        "fields": ["label"],
                        "creates_activity": True,
                        "keeps_active": True,
                    },
                ],
                "active_time": {
                    "type": "expression",
                    "rule": {"type": "any", "children": [{"type": "none"}]},
                },
            }
        ],
        "category_sets_v2": [{"schema_version": 2, "id": "empty", "categories": []}],
    }
    materialized = compile_profile_v2(
        document,
        {
            "window": _bucket("window", "currentwindow"),
            "timer": _bucket("timer", "general.stopwatch"),
        },
        CAPABILITIES,
        hostname="host",
    )
    query = materialized.query()
    assert "coverage_source_0 = flood_v2(coverage_source_0_raw)" in query
    assert "coverage_source_1 = coverage_source_1_raw" in query
    assert 'active_time_sources = []' in query
    assert "events = filter_period_intersect(events, not_afk)" in query


class FakeClient:
    def __init__(self):
        self.queries = []

    def get_info(self):
        return {"hostname": "actual", "capabilities": CAPABILITIES}

    def get_setting(self, key=None):
        assert key == "rules_v2"
        return {
            "revision": 2,
            "activity_profiles_v2": [
                {
                    "schema_version": 2,
                    "id": "p",
                    "category_set_ids": ["empty"],
                    "sources": [
                        {
                            "id": "meeting",
                            "label": "Meeting",
                            "bucket_ids": ["m"],
                            "scope": "global",
                            "fields": ["subject"],
                            "creates_activity": True,
                            "keeps_active": True,
                        }
                    ],
                    "active_time": {"type": "expression", "rule": {"type": "none"}},
                }
            ],
            "category_sets_v2": [{"schema_version": 2, "id": "empty", "categories": []}],
        }

    def get_buckets(self):
        return {"m": _bucket("m", "meeting")}


def test_client_high_level_path_uses_its_own_server(monkeypatch):
    # Call the real unbound method on a deliberately tiny fake. If the method
    # constructs a default/local client this test fails immediately.
    from aw_client.client import ActivityWatchClient

    materialized = ActivityWatchClient.build_profile_query_v2(FakeClient())
    assert materialized.params.hostname == "actual"
    assert materialized.profile_id == "p"


def _legacy_audible_document(browser_focus_source_id="builtin_window"):
    profile = {
        "schema_version": 2,
        "id": "audible",
        "category_set_ids": ["empty"],
        "sources": [
            {
                "id": "builtin_window",
                "label": "Window",
                "builtin": "window",
                "bucket_ids": [],
                "fields": ["app", "title"],
                "creates_activity": True,
                "interval_policy": "heartbeat",
            },
            {
                "id": "browser",
                "label": "Browser",
                "builtin": "browser",
                "bucket_ids": [],
                "fields": ["audible", "url"],
                "interval_policy": "heartbeat",
            },
        ],
        "active_time": {
            "type": "legacy",
            "use_afk": True,
            "include_audible": True,
            "always_active_pattern": "",
        },
    }
    if browser_focus_source_id is not None:
        profile["browser_focus_source_id"] = browser_focus_source_id
    return {
        "revision": 1,
        "activity_profiles_v2": [profile],
        "category_sets_v2": [{"schema_version": 2, "id": "empty", "categories": []}],
    }


def test_profile_audible_requires_the_same_browser_family_and_window_fallback():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    buckets = {
        "window": _bucket("window", "currentwindow"),
        "aw-watcher-web-firefox": _bucket("aw-watcher-web-firefox", "web.tab.current"),
        "aw-watcher-web-chrome": _bucket("aw-watcher-web-chrome", "web.tab.current"),
        "afk": _bucket("afk", "afkstatus"),
    }

    def execute(document, browser_bucket, browser_app):
        datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
        for bucket_id, metadata in buckets.items():
            bucket = datastore.create_bucket(
                bucket_id=bucket_id,
                type=metadata["type"],
                client="test",
                hostname=metadata["hostname"],
                name=bucket_id,
            )
            if bucket_id == "window":
                bucket.insert(Event(timestamp=start, duration=60, data={"app": browser_app}))
            elif bucket_id == "afk":
                bucket.insert(Event(timestamp=start, duration=60, data={"status": "afk"}))
            elif bucket_id == browser_bucket:
                bucket.insert(Event(timestamp=start, duration=60, data={"audible": True, "url": "x"}))
        materialized = compile_profile_v2(document, buckets, CAPABILITIES, hostname="host")
        return query(
            "profile-audible-family",
            materialized.query() + "\nRETURN = not_afk;",
            start,
            start + timedelta(seconds=60),
            datastore,
        )

    assert execute(
        _legacy_audible_document(), "aw-watcher-web-firefox", "Google Chrome"
    ) == []
    fallback = execute(
        _legacy_audible_document(None), "aw-watcher-web-chrome", "Google Chrome"
    )
    assert sum((event.duration for event in fallback), timedelta()) == timedelta(seconds=60)


def test_profile_audible_uses_all_same_family_buckets_and_keeps_brave_separate():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def execute(browser_app, browser_events):
        buckets = {
            "window": _bucket("window", "currentwindow"),
            "afk": _bucket("afk", "afkstatus"),
            **{
                bucket_id: _bucket(bucket_id, "web.tab.current")
                for bucket_id in browser_events
            },
        }
        datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
        for bucket_id, metadata in buckets.items():
            bucket = datastore.create_bucket(
                bucket_id=bucket_id,
                type=metadata["type"],
                client="test",
                hostname=metadata["hostname"],
                name=bucket_id,
            )
            if bucket_id == "window":
                bucket.insert(Event(timestamp=start, duration=10, data={"app": browser_app}))
            elif bucket_id == "afk":
                bucket.insert(Event(timestamp=start, duration=10, data={"status": "afk"}))
            else:
                bucket.insert(
                    Event(
                        timestamp=start,
                        duration=10,
                        data={"audible": browser_events[bucket_id], "url": bucket_id},
                    )
                )
        materialized = compile_profile_v2(
            _legacy_audible_document(), buckets, CAPABILITIES, hostname="host"
        )
        return query(
            "profile-audible-current-families",
            materialized.query() + "\nRETURN = not_afk;",
            start,
            start + timedelta(seconds=10),
            datastore,
        )

    all_chrome = execute(
        "Google Chrome",
        {
            "aw-watcher-web-chrome-1": False,
            "aw-watcher-web-chrome-2": True,
        },
    )
    assert sum((event.duration for event in all_chrome), timedelta()) == timedelta(seconds=10)
    assert execute(
        "Google Chrome",
        {
            "aw-watcher-web-chrome-1": True,
            "aw-watcher-web-chrome-2": False,
        },
    ) == []
    assert execute("Brave-browser", {"aw-watcher-web-chrome-1": True}) == []

    reverse_listing = {
        "aw-watcher-web-chrome-z": _bucket(
            "aw-watcher-web-chrome-z", "web.tab.current"
        ),
        "aw-watcher-web-chrome-a": _bucket(
            "aw-watcher-web-chrome-a", "web.tab.current"
        ),
        "window": _bucket("window", "currentwindow"),
    }
    materialized = compile_profile_v2(
        _legacy_audible_document(), reverse_listing, CAPABILITIES, hostname="host"
    )
    browser_source = next(
        source
        for source in materialized.params.context_sources
        if source.source_id == "browser"
    )
    assert browser_source.bucket_ids == [
        "aw-watcher-web-chrome-a",
        "aw-watcher-web-chrome-z",
    ]
    audible_source = next(
        source
        for source in materialized.params.active_time_sources
        if source.source_id.startswith("browser_audible_")
    )
    assert audible_source.bucket_ids == browser_source.bucket_ids


def _valid_source_document(source_count=1):
    sources = [
        {
            "id": f"source_{index}",
            "label": f"Source {index}",
            "bucket_ids": [f"bucket-{index}"],
            "scope": "global",
            "fields": ["value"],
        }
        for index in range(source_count)
    ]
    return {
        "revision": 0,
        "activity_profiles_v2": [
            {
                "schema_version": 2,
                "id": "profile",
                "category_set_ids": ["empty"],
                "sources": sources,
                "active_time": {"type": "expression", "rule": {"type": "none"}},
            }
        ],
        "category_sets_v2": [
            {"schema_version": 2, "id": "empty", "categories": []}
        ],
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("creates_activity_string", "creates_activity must be boolean"),
        ("keeps_active_string", "keeps_active must be boolean"),
        ("missing_bucket_ids", "bucket_ids must be a string array"),
        ("missing_fields", "fields must be a non-empty string array"),
        ("invalid_builtin", "builtin is unsupported"),
        ("invalid_builtin_id", "builtin is unsupported"),
        ("invalid_scope", "scope must be host or global"),
        ("invalid_policy", "interval_policy must be exact or heartbeat"),
        ("null_field_types", "field_types must be an object"),
        ("invalid_field_types", "field_types values must be string or scalar"),
        ("global_host", "global scope cannot define host ownership"),
        ("host_without_owner", "host scope requires host ownership"),
    ],
)
def test_native_profile_boundary_rejects_complete_source_schema(mutation, message):
    document = _valid_source_document()
    source = document["activity_profiles_v2"][0]["sources"][0]
    if mutation == "creates_activity_string":
        source["creates_activity"] = "false"
    elif mutation == "keeps_active_string":
        source["keeps_active"] = "true"
    elif mutation == "missing_bucket_ids":
        source.pop("bucket_ids")
    elif mutation == "missing_fields":
        source.pop("fields")
    elif mutation == "invalid_builtin":
        source["builtin"] = "bogus"
    elif mutation == "invalid_builtin_id":
        source["builtin"] = "window"
    elif mutation == "invalid_scope":
        source["scope"] = "machine"
    elif mutation == "invalid_policy":
        source["interval_policy"] = "approximate"
    elif mutation == "null_field_types":
        source["field_types"] = None
    elif mutation == "invalid_field_types":
        source["field_types"] = {"value": "number"}
    elif mutation == "global_host":
        source["host"] = "host"
    elif mutation == "host_without_owner":
        source["scope"] = "host"

    with pytest.raises(ValueError, match=message):
        compile_profile_v2(document, {}, CAPABILITIES, hostname="host")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("scope", "bogus", "scope must be host or global"),
        ("scope", None, "scope must be host or global"),
        ("bucket_hosts", 42, "bucket_hosts must be an object"),
    ],
)
def test_empty_builtin_validates_every_present_known_field(field, value, message):
    document = _valid_source_document()
    document["activity_profiles_v2"][0]["sources"][0] = {
        "id": "builtin_window",
        "label": "Window",
        "bucket_ids": [],
        "builtin": "window",
        "fields": ["app", "title"],
        field: value,
    }
    with pytest.raises(ValueError, match=message):
        compile_profile_v2(document, {}, CAPABILITIES, hostname="host")


def test_empty_builtin_defers_discovered_ownership_completeness():
    document = _valid_source_document()
    document["activity_profiles_v2"][0]["sources"][0] = {
        "id": "builtin_window",
        "label": "Window",
        "bucket_ids": [],
        "builtin": "window",
        "fields": ["app", "title"],
        "creates_activity": True,
        "bucket_hosts": {"future-window": "host"},
    }
    materialized = compile_profile_v2(
        document,
        {"window": _bucket("window", "currentwindow")},
        CAPABILITIES,
        hostname="host",
        filter_afk=False,
    )
    assert materialized.params.activity_coverage_sources[0].bucket_ids == ["window"]
    assert materialized.params.activity_coverage_sources[0].bucket_hosts is None


def test_regex_ignores_irrelevant_rules_metadata():
    document = _valid_source_document()
    document["category_sets_v2"][0]["categories"] = [
        {
            "id": "metadata",
            "name": ["Metadata"],
            "rule": {
                "type": "regex",
                "source": "source_0",
                "regex": "x",
                "rules": 42,
            },
        }
    ]
    compile_profile_v2(document, {}, CAPABILITIES, hostname="host")


def test_native_profile_source_count_boundary_and_optional_defaults():
    # Optional source behavior fields default legitimately; required arrays do not.
    document = _valid_source_document(128)
    assert all(
        "field_types" not in source
        for source in document["activity_profiles_v2"][0]["sources"]
    )
    compile_profile_v2(document, {}, CAPABILITIES, hostname="host")
    with pytest.raises(ValueError, match="sources exceeds maximum count of 128"):
        compile_profile_v2(_valid_source_document(129), {}, CAPABILITIES, hostname="host")


def test_rules_loader_only_treats_an_http_404_as_missing():
    failure = RuntimeError("connection failed")

    class BrokenClient:
        def get_setting(self, key=None):
            raise failure

    try:
        load_rules_document(BrokenClient())
    except RuntimeError as error:
        assert error is failure
    else:
        raise AssertionError("connection errors must not fall back to legacy settings")


def test_native_profile_boundary_rejects_forward_and_aggregate_documents():
    forward = _legacy_audible_document()
    forward["activity_profiles_v2"][0]["schema_version"] = 99
    try:
        compile_profile_v2(forward, {}, CAPABILITIES, hostname="host")
    except ValueError as error:
        assert "schema_version must be 2" in str(error)
    else:
        raise AssertionError("forward profile versions must be rejected")

    forward_set = _legacy_audible_document()
    forward_set["category_sets_v2"][0]["schema_version"] = 99
    try:
        compile_profile_v2(forward_set, {}, CAPABILITIES, hostname="host")
    except ValueError as error:
        assert "schema_version must be 2" in str(error)
    else:
        raise AssertionError("forward category-set versions must be rejected")

    categories = lambda prefix: [
        {"id": f"{prefix}-{index}", "name": [prefix, str(index)], "rule": {"type": "none"}}
        for index in range(501)
    ]
    aggregate = {
        "revision": 0,
        "activity_profiles_v2": [
            {
                "schema_version": 2,
                "id": "p",
                "category_set_ids": ["one", "two"],
                "sources": [],
                "active_time": {"type": "expression", "rule": {"type": "none"}},
            }
        ],
        "category_sets_v2": [
            {"schema_version": 2, "id": "one", "categories": categories("one")},
            {"schema_version": 2, "id": "two", "categories": categories("two")},
        ],
    }
    try:
        compile_profile_v2(aggregate, {}, CAPABILITIES, hostname="host")
    except ValueError as error:
        assert "select 1002 category rules" in str(error)
    else:
        raise AssertionError("aggregate category budgets must be rejected")


@pytest.mark.parametrize("field", ["weight", "priority", "set_priority"])
@pytest.mark.parametrize("value", [True, 1.5, -1_000_001, 1_000_001])
def test_ranking_integers_use_the_shared_domain(field, value):
    document = _valid_source_document()
    category = {
        "id": "ranked",
        "name": ["Ranked"],
        "rule": {"type": "regex", "source": "source_0", "regex": "x"},
    }
    if field == "weight":
        category["rule"][field] = value
    else:
        category[field] = value
    document["category_sets_v2"][0]["categories"] = [category]
    with pytest.raises(ValueError, match="must be an integer between"):
        compile_profile_v2(document, {}, CAPABILITIES, hostname="host")


def test_revision_must_be_a_non_negative_safe_integer():
    document = _valid_source_document()
    document["revision"] = 2**53
    with pytest.raises(ValueError, match="safe integer"):
        compile_profile_v2(document, {}, CAPABILITIES, hostname="host")


def test_generated_active_ids_avoid_declared_sources_and_afk_is_sorted():
    document = _legacy_audible_document("browser_audible_0")
    profile = document["activity_profiles_v2"][0]
    profile["sources"].extend(
        [
            {
                "id": "afk",
                "label": "Reserved AFK",
                "bucket_ids": ["reserved-afk"],
                "scope": "global",
                "fields": ["value"],
            },
            {
                "id": "browser_audible_0",
                "label": "Focus",
                "bucket_ids": ["focus"],
                "scope": "global",
                "fields": ["app"],
                "creates_activity": True,
            },
        ]
    )
    buckets = {
        "window": _bucket("window", "currentwindow"),
        "browser": _bucket("aw-watcher-web-chrome", "web.tab.current"),
        "afk-z": _bucket("afk-z", "afkstatus"),
        "afk-a": _bucket("afk-a", "afkstatus"),
        "reserved-afk": _bucket("reserved-afk", "custom"),
        "focus": _bucket("focus", "custom"),
    }
    materialized = compile_profile_v2(
        document, buckets, CAPABILITIES, hostname="host", filter_afk=True
    )
    sources = {source.source_id: source for source in materialized.params.active_time_sources}
    assert sources["afk_2"].bucket_ids == ["afk-a", "afk-z"]
    assert sources["browser_audible_0_2"].bucket_ids == ["aw-watcher-web-chrome"]
    assert sources["browser_audible_0"].bucket_ids == ["focus"]
    assert len(sources) == len(materialized.params.active_time_sources)
    assert '"source":"afk_2"' in materialized.query()
    assert '"source":"browser_audible_0_2"' in materialized.query()


def test_execution_specs_drop_irrelevant_metadata():
    document = _valid_source_document()
    profile = document["activity_profiles_v2"][0]
    profile["active_time"] = {
        "type": "expression",
        "rule": {
            "type": "regex",
            "source": "source_0",
            "regex": "x",
            "rules": "\\",
            "presentation": {"label": "\\"},
        },
    }
    document["category_sets_v2"][0]["categories"] = [
        {
            "id": "metadata",
            "name": ["Metadata"],
            "data": {"label": "\\"},
            "simple_ui": True,
            "rule": {
                "type": "regex",
                "source": "source_0",
                "regex": "x",
                "rules": "\\",
            },
        }
    ]
    materialized = compile_profile_v2(
        document,
        {"bucket-0": _bucket("bucket-0", "custom")},
        CAPABILITIES,
        hostname="host",
        filter_afk=False,
    )
    generated = materialized.query()
    assert '"regex":"x"' in generated
    assert "presentation" not in generated
    assert "simple_ui" not in generated
    assert '"rules":"' not in generated
    assert '"data"' not in generated


def test_unavailable_declared_active_builtin_is_empty_only_when_safe():
    document = _valid_source_document()
    profile = document["activity_profiles_v2"][0]
    profile["sources"].append(
        {
            "id": "browser",
            "label": "Browser",
            "bucket_ids": [],
            "builtin": "browser",
            "fields": ["audible"],
            "interval_policy": "heartbeat",
        }
    )
    profile["active_time"] = {
        "type": "expression",
        "rule": {
            "type": "regex",
            "source": "browser",
            "field": "audible",
            "regex": "^true$",
            "value_mode": "scalar",
        },
    }

    unfiltered = compile_profile_v2(
        document, {}, CAPABILITIES, hostname="host", filter_afk=False
    )
    assert unfiltered.params.active_time_sources[0].bucket_ids == []
    assert "active_source_0_raw = [];" in unfiltered.query()

    with pytest.raises(ValueError, match="unavailable source 'browser'"):
        compile_profile_v2(document, {}, CAPABILITIES, hostname="host", filter_afk=True)

    profile["sources"][0]["creates_activity"] = True
    profile["sources"][0]["keeps_active"] = True
    filtered = compile_profile_v2(document, {}, CAPABILITIES, hostname="host", filter_afk=True)
    assert filtered.params.active_time_sources[0].bucket_ids == []


def test_rules_loader_rejects_a_malformed_present_document_without_fallback():
    class NullCanonicalClient:
        def get_setting(self, key=None):
            if key == "rules_v2":
                return None
            raise AssertionError("a present canonical response must not trigger legacy loading")

    try:
        load_rules_document(NullCanonicalClient())
    except ValueError as error:
        assert "must be an object" in str(error)
    else:
        raise AssertionError("a malformed canonical document must fail clearly")
