import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aw_core import Event
from aw_datastore import Datastore
from aw_datastore.storages import MemoryStorage
from aw_query import query as execute_query

from aw_client.queries import (
    CURRENT_QUERY_CAPABILITIES,
    ActivityCoverageSource,
    CanonicalQueryParamsV2,
    canonicalEventsV2,
)


EXAMPLES = Path(__file__).parents[1] / "examples"


def _load_example(name):
    spec = importlib.util.spec_from_file_location(f"test_example_{name}", EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Profile:
    def __init__(self, query, source_id="presentation"):
        self._query = query
        self.app_title_source_id = source_id

    def query(self):
        return self._query


class _FixtureClient:
    def __init__(self, datastore, profile):
        self.datastore = datastore
        self.profile = profile

    def build_profile_query_v2(self, **_kwargs):
        return self.profile

    def query(self, program, periods):
        return [
            execute_query("example-regression", program, start, end, self.datastore)
            for start, end in periods
        ]


def _fixture(category_specs=None):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    bucket = datastore.create_bucket(
        bucket_id="presentation",
        type="currentwindow",
        client="test",
        hostname="host",
        name="presentation",
    )
    bucket.insert(
        Event(
            timestamp=start,
            duration=60,
            data={"app": "Editor", "title": 'ticket 123 "quoted"'},
        )
    )
    canonical = canonicalEventsV2(
        CanonicalQueryParamsV2(
            activity_coverage_sources=[
                ActivityCoverageSource(
                    "presentation",
                    ["presentation"],
                    ["app", "title"],
                    scope="global",
                    interval_policy="exact",
                )
            ],
            category_specs=category_specs,
            capabilities=sorted(CURRENT_QUERY_CAPABILITIES),
            filter_afk=False,
        )
    )
    return start, datastore, _Profile(canonical)


def test_working_hours_executes_escaped_query2_regex(monkeypatch):
    working_hours = _load_example("working_hours")
    start, datastore, profile = _fixture()
    client = _FixtureClient(datastore, profile)
    monkeypatch.setattr(
        working_hours.aw_client,
        "ActivityWatchClient",
        lambda **_kwargs: client,
    )

    result = working_hours.query(
        r'\d+\s+"quoted"',
        [(start, start + timedelta(minutes=1))],
        "host",
    )[0]

    assert result["duration"] == timedelta(minutes=1)
    assert result["events"][0].data["$source.presentation.title"] == 'ticket 123 "quoted"'


def test_category_suggestion_requires_a_title_presentation_source(monkeypatch):
    suggest = _load_example("suggest_categories")
    _start, datastore, profile = _fixture(category_specs=[])
    profile.app_title_source_id = None
    monkeypatch.setattr(suggest, "awc", _FixtureClient(datastore, profile))

    with pytest.raises(ValueError, match="no title presentation source"):
        suggest.get_events()


def test_category_suggestion_projects_custom_presentation_title(monkeypatch):
    suggest = _load_example("suggest_categories")
    start, datastore, profile = _fixture(category_specs=[])
    client = _FixtureClient(datastore, profile)
    monkeypatch.setattr(suggest, "awc", client)

    events = suggest.get_events()
    assert len(events) == 1
    assert events[0].data["title"] == 'ticket 123 "quoted"'

    monkeypatch.setitem(sys.modules, "suggest_categories", suggest)
    gpt = _load_example("suggest_categories_gpt")

    class _Completion:
        @staticmethod
        def create(**kwargs):
            assert 'ticket 123 "quoted"' in kwargs["prompt"]
            return {"choices": [{"text": "Skip:"}]}

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(api_key=None, Completion=_Completion),
    )
    categories = gpt.example_categories()
    suggested = gpt.gpt_suggest(events[0], categories)
    assert suggested
