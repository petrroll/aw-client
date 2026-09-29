"""
Common queries.

Most of these are from: https://github.com/ActivityWatch/aw-webui/blob/master/src/queries.ts
"""

import copy
import dataclasses
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    Union,
)

from typing_extensions import TypeGuard

import aw_client

from .classes import default_classes


class EnhancedJSONEncoder(json.JSONEncoder):
    """For encoding dataclasses into JSON"""

    def default(self, o):
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)  # type: ignore
        return super().default(o)


"""
Do these dataclasses look confusing?
Read up on dataclass inheritance: https://stackoverflow.com/a/53085935/965332
"""


@dataclass
class ContextSource:
    source_id: str
    bucket_ids: List[str]
    fields: List[str]
    conflict: str = "base_wins"
    host: Optional[str] = None
    bucket_hosts: Optional[Dict[str, str]] = None
    scope: Optional[Literal["host", "global"]] = None
    interval_policy: Literal["exact", "heartbeat"] = "exact"


@dataclass
class ActiveTimeSource:
    source_id: str
    bucket_ids: List[str]
    host: Optional[str] = None
    bucket_hosts: Optional[Dict[str, str]] = None
    scope: Optional[Literal["host", "global"]] = None
    interval_policy: Literal["exact", "heartbeat"] = "exact"


@dataclass
class ActivitySource:
    source_id: str
    bucket_ids: List[str]
    field_mappings: Dict[str, str] = field(default_factory=dict)
    host: Optional[str] = None
    bucket_hosts: Optional[Dict[str, str]] = None
    scope: Optional[Literal["host", "global"]] = None
    interval_policy: Literal["exact", "heartbeat"] = "exact"


@dataclass
class ActivityCoverageSource:
    source_id: str
    bucket_ids: List[str]
    fields: List[str]
    host: Optional[str] = None
    bucket_hosts: Optional[Dict[str, str]] = None
    scope: Optional[Literal["host", "global"]] = None
    keeps_active: bool = False
    interval_policy: Literal["exact", "heartbeat"] = "exact"


@dataclass
class CanonicalQueryParamsV2:
    """Inputs for the source-only canonical query pipeline."""

    activity_coverage_sources: List[ActivityCoverageSource] = field(
        default_factory=list
    )
    active_time_sources: List[ActiveTimeSource] = field(default_factory=list)
    active_time_rule: Optional[Dict[str, Any]] = None
    context_sources: List[ContextSource] = field(default_factory=list)
    category_specs: Optional[List[Dict[str, Any]]] = None
    hostname: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    filter_afk: bool = True
    filter_categories: Optional[List[List[str]]] = None
    explain_categories: bool = False


@dataclass
class _CurrentPipelineInternals:
    """Private compatibility hooks which never widen the public raw-source API."""

    bucket_expressions: Dict[str, List[str]] = field(default_factory=dict)
    active_aliases: Dict[str, str] = field(default_factory=dict)
    auxiliary_sources: List[ActiveTimeSource] = field(default_factory=list)


@dataclass
class _QueryParamsDefaultsBase:
    bid_browsers: List[str] = field(default_factory=list)
    classes: List[Tuple[List[str], dict]] = field(default_factory=list)
    filter_classes: List[List[str]] = field(default_factory=list)
    filter_afk: bool = True
    include_audible: bool = True
    # Keep all advanced fields after the legacy positional parameters.
    hostname: Optional[str] = None
    category_specs: Optional[List[Dict[str, Any]]] = None
    context_sources: List[ContextSource] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    active_time_rule: Optional[Dict[str, Any]] = None
    active_time_sources: List[ActiveTimeSource] = field(default_factory=list)
    # Deprecated replacement/gap-filling inputs retained for compatibility.
    activity_sources: List[ActivitySource] = field(default_factory=list)
    background_sources: List[ActivitySource] = field(default_factory=list)
    activity_coverage_sources: List[ActivityCoverageSource] = field(
        default_factory=list
    )
    legacy_window_mode: Literal["activity", "context", "none"] = "activity"
    legacy_window_fields: List[str] = field(default_factory=lambda: ["app", "title"])


@dataclass
class QueryParams(_QueryParamsDefaultsBase):
    pass


@dataclass
class _DesktopQueryParamsBase:
    bid_window: Optional[str] = None
    bid_afk: Optional[str] = None
    always_active_pattern: Optional[str] = None


@dataclass
class DesktopQueryParams(QueryParams, _DesktopQueryParamsBase):
    pass


@dataclass
class _AndroidQueryParamsBase:
    bid_android: str


@dataclass
class AndroidQueryParams(QueryParams, _AndroidQueryParamsBase):
    pass


def isDesktopParams(params: QueryParams) -> TypeGuard[DesktopQueryParams]:
    return isinstance(params, DesktopQueryParams)


def isAndroidParams(params: QueryParams) -> TypeGuard[AndroidQueryParams]:
    return isinstance(params, AndroidQueryParams)


def _legacy_selector_expression(bucket_id: str, hostname: Optional[str]) -> str:
    arguments = [_serialize_bucket_id(bucket_id)]
    if hostname:
        arguments.append(_serialize_query_json(hostname))
    return f"find_bucket({', '.join(arguments)})"


def _adapt_desktop_to_current_v2(params: DesktopQueryParams) -> str:
    if params.activity_sources or params.background_sources:
        raise ValueError(
            "replacement/background source semantics are legacy-v1 only; use explicit "
            "coverage/context roles on current servers"
        )
    coverage = list(params.activity_coverage_sources)
    context = list(params.context_sources)
    internals = _CurrentPipelineInternals()
    projection_source = None
    projection_variable = "events"
    if params.legacy_window_mode == "none" and params.always_active_pattern:
        raise ValueError(
            "always_active_pattern requires a legacy window projection; "
            "legacy_window_mode='none' is unsupported"
        )
    if (
        params.category_specs is not None
        and "query.categorize_v2.v1" not in params.capabilities
    ):
        raise ValueError(
            "flexible categorization requires server capability query.categorize_v2.v1"
        )
    if params.bid_window and params.legacy_window_mode != "none":
        projection_source = "legacy_window"
        common = {
            "source_id": projection_source,
            "bucket_ids": [params.bid_window],
            "fields": list(params.legacy_window_fields),
            "host": params.hostname,
            "scope": "host" if params.hostname else "global",
            "interval_policy": "heartbeat",
        }
        internals.bucket_expressions[projection_source] = [
            _legacy_selector_expression(params.bid_window, params.hostname)
        ]
        if params.legacy_window_mode == "activity":
            coverage.insert(0, ActivityCoverageSource(**common))
            projection_variable = "coverage_source_0"
        else:
            context.insert(0, ContextSource(**common))
            projection_variable = "context_source_0"

    has_custom_active_rule = params.active_time_rule is not None
    active_rule = params.active_time_rule
    active_sources = list(params.active_time_sources)
    if active_rule is None and params.bid_afk:
        active_sources = [
            ActiveTimeSource(
                "legacy_afk",
                [params.bid_afk],
                host=params.hostname,
                scope="host" if params.hostname else "global",
                interval_policy="heartbeat",
            )
        ]
        internals.bucket_expressions["legacy_afk"] = [
            _legacy_selector_expression(params.bid_afk, params.hostname)
        ]
        branches: List[Dict[str, Any]] = [
            {
                "type": "regex",
                "source": "legacy_afk",
                "field": "status",
                "regex": "^not-afk$",
            }
        ]
        if params.always_active_pattern and projection_source and params.bid_window:
            active_sources.append(
                ActiveTimeSource(
                    projection_source,
                    [params.bid_window],
                    host=params.hostname,
                    scope="host" if params.hostname else "global",
                    interval_policy="heartbeat",
                )
            )
            # Reuse the already-loaded window facts for always-active matching.
            internals.active_aliases[projection_source] = projection_variable
            window_rules = [
                {
                    "type": "regex",
                    "source": projection_source,
                    "field": field,
                    "regex": params.always_active_pattern,
                }
                for field in params.legacy_window_fields
                if field in ("app", "title")
            ]
            if window_rules:
                branches.append(
                    window_rules[0]
                    if len(window_rules) == 1
                    else {"type": "any", "rules": window_rules}
                )
        active_rule = (
            branches[0] if len(branches) == 1 else {"type": "any", "rules": branches}
        )

    browser_streams: List[Tuple[str, str]] = []
    browser_sources = current_browser_families(params.bid_browsers)
    audible_enabled = (
        params.include_audible
        and not has_custom_active_rule
        and active_rule is not None
        and projection_source is not None
    )
    audible_rules: List[Dict[str, Any]] = []
    for browser_name, family_bucket_ids in browser_sources:
        source_id = f"legacy_browser_{browser_name}"
        source = ActiveTimeSource(
            source_id,
            family_bucket_ids,
            scope="global",
            interval_policy="heartbeat",
        )
        if audible_enabled:
            active_index = len(active_sources)
            active_sources.append(source)
            browser_streams.append((browser_name, f"active_source_{active_index}"))
            focus_rule = current_browser_focus_rule(projection_source, browser_name)
            audible_rules.append(
                {
                    "type": "all",
                    "rules": [
                        {
                            "type": "regex",
                            "source": source_id,
                            "field": "audible",
                            "regex": "^true$",
                            "value_mode": "scalar",
                        },
                        focus_rule,
                    ],
                }
            )
        else:
            auxiliary_index = len(internals.auxiliary_sources)
            internals.auxiliary_sources.append(source)
            browser_streams.append((browser_name, f"auxiliary_source_{auxiliary_index}"))

    if audible_rules:
        assert projection_source is not None
        if all(source.source_id != projection_source for source in active_sources):
            active_sources.append(
                ActiveTimeSource(
                    projection_source,
                    [params.bid_window],
                    host=params.hostname,
                    scope="host" if params.hostname else "global",
                    interval_policy="heartbeat",
                )
            )
            internals.active_aliases[projection_source] = projection_variable
        active_rule = {"type": "any", "rules": [active_rule, *audible_rules]}

    query = _canonical_source_pipeline_v2(
        CanonicalQueryParamsV2(
            activity_coverage_sources=coverage,
            active_time_sources=active_sources,
            active_time_rule=active_rule,
            context_sources=context,
            category_specs=None,
            hostname=params.hostname,
            capabilities=params.capabilities,
            filter_afk=params.filter_afk and bool(coverage),
            filter_categories=None,
        ),
        internals,
    )
    if projection_source:
        source_variable = projection_variable
        # Project the old root-field shape from the already loaded source. The
        # period reset removes v2 namespace fields without a second bucket read.
        query += (
            "\nevents = period_union([], events);"
            "\nevents = merge_subwatcher_fields(events, "
            f"{source_variable}, {_serialize_query_json(params.legacy_window_fields)});"
        )
        coverage_offset = 1 if params.legacy_window_mode == "activity" else 0
        for index, source in enumerate(
            params.activity_coverage_sources, start=coverage_offset
        ):
            query += (
                "\nevents = merge_subwatcher_fields(events, "
                f"coverage_source_{index}, {_serialize_query_json(source.fields)}, "
                f"{_serialize_query_json({'source_id': source.source_id, 'conflict': 'base_wins'})});"
            )
        context_offset = 1 if params.legacy_window_mode == "context" else 0
        for index, source in enumerate(params.context_sources, start=context_offset):
            query += (
                "\nevents = merge_subwatcher_fields(events, "
                f"context_source_{index}, {_serialize_query_json(source.fields)}, "
                f"{_serialize_query_json({'source_id': source.source_id, 'conflict': source.conflict})});"
            )
        if params.category_specs is not None:
            query += (
                "\nevents = merge_subwatcher_fields(events, "
                f"{source_variable}, {_serialize_query_json(params.legacy_window_fields)}, "
                f"{_serialize_query_json({'source_id': projection_source, 'conflict': 'base_wins'})});"
            )

    if params.category_specs is not None:
        query += (
            "\nevents = categorize_v2(events, "
            f"{_serialize_query_json(semantic_category_specs(params.category_specs))}"
            + (f", {_serialize_query_json(params.hostname)}" if params.hostname else "")
            + ");"
        )
    elif params.classes:
        query += f"\nevents = categorize(events, {_serialize_query_json(params.classes)});"
    if params.filter_classes:
        query += (
            '\nevents = filter_keyvals(events, "$category", '
            f"{_serialize_query_json(params.filter_classes)});"
        )

    query += "\nbrowser_events = [];"
    for browser_name, variable in browser_streams:
        if projection_source is None:
            continue
        focus_rule = current_browser_focus_rule(projection_source, browser_name)
        browser_fields = ["url", "title", "audible", "incognito", "tabCount"]
        query += (
            f"\nbrowser_focus_{browser_name} = active_periods_v2("
            f"[[{_serialize_query_json(projection_source)}, {projection_variable}]], "
            f"{_serialize_query_json(focus_rule)});"
            f"\nbrowser_facts_{browser_name} = merge_subwatcher_fields("
            f"{variable}, [], {_serialize_query_json(browser_fields)});"
            f"\nbrowser_presence_{browser_name} = period_union({variable}, []);"
            f"\nbrowser_presence_{browser_name} = filter_period_intersect("
            f"browser_presence_{browser_name}, browser_focus_{browser_name});"
            f"\nbrowser_resolved_{browser_name} = merge_subwatcher_fields("
            f"browser_presence_{browser_name}, browser_facts_{browser_name}, "
            f"{_serialize_query_json(browser_fields)});"
            f"\nbrowser_{browser_name} = split_url_events(browser_resolved_{browser_name});"
            f"\nbrowser_events = concat(browser_events, browser_{browser_name});"
        )
    if browser_streams:
        query += (
            "\nbrowser_events = sort_by_timestamp(browser_events);"
            "\nbrowser_events = filter_period_intersect(browser_events, query_window);"
            # Derived report output cannot escape selected canonical activity.
            "\nbrowser_events = filter_period_intersect(browser_events, events);"
        )
    return query


def resolveActivityProfile(
    params: Union[DesktopQueryParams, AndroidQueryParams],
) -> str:
    """Translate a legacy desktop signature into the current source pipeline."""
    if not isDesktopParams(params):
        raise ValueError(
            "Android grammar is legacy-v1 only; use aw_client.legacy_v1 explicitly"
        )
    params = dataclasses.replace(params)
    params.capabilities = sorted(
        CURRENT_QUERY_CAPABILITIES.union(params.capabilities)
    )
    if params.category_specs is None and not params.classes:
        params.classes = copy.deepcopy(default_classes)
    return _adapt_desktop_to_current_v2(params)


def canonicalEvents(params: Union[DesktopQueryParams, AndroidQueryParams]) -> str:
    """Compatibility alias for existing query-builder consumers."""
    return resolveActivityProfile(params)


CURRENT_QUERY_CAPABILITIES = {
    "query.query_bucket_optional_raw.v1",
    "query.query_period.v1",
    "query.flood_v2.v1",
    "query.merge_subwatcher_fields.source_namespace.v1",
    "query.active_periods_v2.v1",
    "query.categorize_v2.v1",
}


def _validate_interval_policy(source: Any, source_kind: str) -> None:
    if source.interval_policy not in ("exact", "heartbeat"):
        raise ValueError(f"{source_kind} interval_policy must be 'exact' or 'heartbeat'")


def _raw_source_events(
    variable: str,
    source: Any,
    hostname: Optional[str],
    source_kind: str,
    bucket_expressions: Optional[List[str]] = None,
) -> str:
    """Load competing source facts without clipping them to the query interval."""
    _validate_interval_policy(source, source_kind)
    bucket_ids = _source_bucket_ids(
        source.bucket_ids,
        source.host,
        source.bucket_hosts,
        source.scope,
        hostname,
        source_kind,
    )
    if bucket_expressions is not None and len(bucket_expressions) != len(source.bucket_ids):
        raise ValueError("internal bucket selector count does not match source bucket ids")
    expression_by_id = dict(zip(source.bucket_ids, bucket_expressions or []))
    expected_hostname = _expected_source_hostname(source.scope, hostname, True)
    raw = f"{variable}_raw"
    lines = [f"{raw} = [];"]
    for bucket_id in bucket_ids:
        arguments = [expression_by_id.get(bucket_id, _serialize_bucket_id(bucket_id))]
        if expected_hostname:
            arguments.append(_serialize_query_json(expected_hostname))
        elif source.interval_policy == "heartbeat":
            arguments.append("null")
        if source.interval_policy == "heartbeat":
            arguments.append("5")
        lines.append(
            f"{raw} = concat({raw}, query_bucket_optional_raw("
            f"{', '.join(arguments)}));"
        )
    if source.interval_policy == "heartbeat":
        lines.append(f"{variable} = flood_v2({raw});")
    else:
        lines.append(f"{variable} = {raw};")
    return "\n".join(lines)


def _canonical_source_pipeline_v2(
    params: CanonicalQueryParamsV2,
    internals: Optional[_CurrentPipelineInternals] = None,
) -> str:
    """The sole current-server pipeline used by native Python query builders."""
    internals = internals or _CurrentPipelineInternals()
    if params.hostname == "":
        raise ValueError("hostname must be non-empty")
    if params.active_time_rule is None and params.active_time_sources:
        raise ValueError("active-time sources require an active-time rule")
    if params.category_specs is not None and "query.categorize_v2.v1" not in params.capabilities:
        raise ValueError(
            "flexible categorization requires server capability query.categorize_v2.v1"
        )
    if params.explain_categories and "query.categorize_v2_explain.v1" not in params.capabilities:
        raise ValueError(
            "category explanations require server capability "
            "query.categorize_v2_explain.v1"
        )

    fact_sources = [*params.activity_coverage_sources, *params.context_sources]
    fact_source_ids = [source.source_id for source in fact_sources]
    if len(fact_source_ids) != len(set(fact_source_ids)):
        raise ValueError("canonical fact source ids must be unique across coverage and context")
    _validate_unique_source_ids(params.active_time_sources, "active-time")
    missing_active_sources = _active_time_rule_source_ids(params.active_time_rule) - {
        source.source_id for source in params.active_time_sources
    }
    if missing_active_sources:
        raise ValueError(
            "active-time rule references unknown source(s): "
            + ", ".join(sorted(missing_active_sources))
        )
    has_keeps_active = any(source.keeps_active for source in params.activity_coverage_sources)
    if params.filter_afk and params.active_time_rule is None and not has_keeps_active:
        raise ValueError(
            "active filtering requires an active-time rule or keeps_active coverage"
        )

    code = ["query_window = query_period();", "coverage = [];"]
    # Load all source facts first. The raw primitive intentionally retains the
    # original event bounds around the interval; only derived coverage is clipped.
    for index, source in enumerate(params.activity_coverage_sources):
        if not source.fields:
            raise ValueError("activity coverage source must contain at least one field")
        variable = f"coverage_source_{index}"
        code.append(
            _raw_source_events(
                variable,
                source,
                params.hostname,
                "activity coverage",
                internals.bucket_expressions.get(source.source_id),
            )
        )
        code.append(
            f"coverage_period_{index} = filter_period_intersect({variable}, query_window);"
        )
        code.append(f"coverage = period_union(coverage, coverage_period_{index});")
    for index, source in enumerate(params.context_sources):
        if not source.fields:
            raise ValueError("context source must contain at least one field")
        code.append(
            _raw_source_events(
                f"context_source_{index}",
                source,
                params.hostname,
                "context",
                internals.bucket_expressions.get(source.source_id),
            )
        )
    for index, source in enumerate(params.active_time_sources):
        variable = f"active_source_{index}"
        alias = internals.active_aliases.get(source.source_id)
        code.append(
            f"{variable} = {alias};"
            if alias
            else _raw_source_events(
                variable,
                source,
                params.hostname,
                "active-time",
                internals.bucket_expressions.get(source.source_id),
            )
        )
    for index, source in enumerate(internals.auxiliary_sources):
        code.append(
            _raw_source_events(
                f"auxiliary_source_{index}",
                source,
                params.hostname,
                "legacy auxiliary",
                internals.bucket_expressions.get(source.source_id),
            )
        )

    code.extend(["coverage = filter_period_intersect(coverage, query_window);", "events = coverage;"])
    # Enrich from competing source originals (or their deterministic heartbeat
    # extension), never from a source union that discarded overlaps.
    for index, source in enumerate(params.activity_coverage_sources):
        code.append(
            "events = merge_subwatcher_fields(events, "
            f"coverage_source_{index}, {_serialize_query_json(source.fields)}, "
            f"{_serialize_query_json({'source_id': source.source_id, 'conflict': 'base_wins'})});"
        )
    for index, source in enumerate(params.context_sources):
        code.append(
            "events = merge_subwatcher_fields(events, "
            f"context_source_{index}, {_serialize_query_json(source.fields)}, "
            f"{_serialize_query_json({'source_id': source.source_id, 'conflict': source.conflict})});"
        )

    if params.active_time_rule is not None:
        named = ", ".join(
            f"[{_serialize_query_json(source.source_id)}, active_source_{index}]"
            for index, source in enumerate(params.active_time_sources)
        )
        code.extend(
            [
                f"active_time_sources = [{named}];",
                f"active_time_rule = {_serialize_query_json(semantic_rule_expression(params.active_time_rule))};",
                "not_afk = active_periods_v2(active_time_sources, active_time_rule"
                + (f", {_serialize_query_json(params.hostname)}" if params.hostname else "")
                + ");",
            ]
        )
    else:
        code.append("not_afk = [];")
    for index, source in enumerate(params.activity_coverage_sources):
        if source.keeps_active:
            code.append(f"not_afk = period_union(not_afk, coverage_period_{index});")
    # Normalize after every active contribution so adjacent winning-fact slices
    # have one representation before they clip canonical events.
    code.append("not_afk = period_union(not_afk, []);")
    # The effective mask exists even for an unfiltered output and can never
    # escape real source coverage or the requested query period.
    code.append("not_afk = filter_period_intersect(not_afk, coverage);")
    if params.filter_afk:
        code.append("events = filter_period_intersect(events, not_afk);")

    if params.category_specs is not None:
        function = "categorize_v2_explain" if params.explain_categories else "categorize_v2"
        host = f", {_serialize_query_json(params.hostname)}" if params.hostname else ""
        code.append(
            f"events = {function}(events, {_serialize_query_json(semantic_category_specs(params.category_specs))}{host});"
        )
    if params.filter_categories:
        code.append(
            'events = filter_keyvals(events, "$category", '
            f"{_serialize_query_json(params.filter_categories)});"
        )
    return "\n".join(code)


def canonicalEventsV2(params: CanonicalQueryParamsV2) -> str:
    """Build current-server canonical events from explicit source roles."""
    return _canonical_source_pipeline_v2(params)


def pretty_query(query: str) -> str:
    return "\n".join([line.strip() for line in query.split("\n") if line.strip()])


def semantic_rule_expression(expression: Dict[str, Any]) -> Dict[str, Any]:
    """Project a persisted expression to fields consumed by Query2 evaluators."""
    kind = expression.get("type")
    if kind is None and "regex" in expression:
        kind = "regex"
    if kind == "none":
        return {"type": "none"}
    if kind in ("all", "any"):
        children = expression.get("rules", expression.get("children", []))
        return {
            "type": kind,
            "rules": [semantic_rule_expression(child) for child in children],
        }
    if kind == "regex":
        result = {"type": "regex", "regex": expression["regex"]}
        for key in (
            "source",
            "host",
            "ignore_case",
            "negate",
            "weight",
            "value_mode",
        ):
            if key in expression:
                result[key] = copy.deepcopy(expression[key])
        for key in ("fields", "field", "select_keys"):
            if key in expression:
                result[key] = copy.deepcopy(expression[key])
                break
        return result
    raise ValueError(f"unsupported rule expression type {kind!r}")


def semantic_category_specs(
    categories: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Project category documents to fields consumed by Query2 evaluators."""
    result = []
    for category in categories:
        item = {
            "name": copy.deepcopy(category["name"]),
            "rule": semantic_rule_expression(category["rule"]),
        }
        if "id" in category:
            item["id"] = category["id"]
        for key in ("priority", "set_priority", "requires"):
            if key in category:
                item[key] = copy.deepcopy(category[key])
        result.append(item)
    return result


# Frozen Query2 primitives used only by aw_client.legacy_v1 and raw compatibility APIs.
# Frozen public helpers delegate to the explicit legacy target module.
def contextEvents(
    sources: List[ContextSource],
    hostname: Optional[str] = None,
    enforce_hostname: bool = False,
) -> str:
    from .legacy_v1 import contextEvents as build

    return build(sources, hostname, enforce_hostname)


def activityEvents(
    sources: List[ActivitySource],
    filter_afk: bool,
    hostname: Optional[str] = None,
    enforce_hostname: bool = False,
) -> str:
    from .legacy_v1 import activityEvents as build

    return build(sources, filter_afk, hostname, enforce_hostname)


def activityCoverageEvents(
    sources: List[ActivityCoverageSource],
    hostname: Optional[str] = None,
    enforce_hostname: bool = False,
) -> str:
    from .legacy_v1 import activityCoverageEvents as build

    return build(sources, hostname, enforce_hostname)


def activityCoverageActiveOverrides(
    sources: List[ActivityCoverageSource],
) -> str:
    from .legacy_v1 import activityCoverageActiveOverrides as build

    return build(sources)


def backgroundActivityEvents(
    sources: List[ActivitySource],
    hostname: Optional[str] = None,
    enforce_hostname: bool = False,
) -> str:
    from .legacy_v1 import backgroundActivityEvents as build

    return build(sources, hostname, enforce_hostname)


def activeTimeEvents(params: DesktopQueryParams) -> str:
    from .legacy_v1 import activeTimeEvents as build

    return build(params)


def legacyActiveTimeQuery(bid_afk: str, hostname: Optional[str] = None) -> str:
    from .legacy_v1 import legacyActiveTimeQuery as build

    return build(bid_afk, hostname)


def activityQuery(afk_buckets: List[str]) -> str:
    from .legacy_v1 import activityQuery as build

    return build(afk_buckets)


def browserEvents(params: DesktopQueryParams) -> str:
    from .legacy_v1 import browserEvents as build

    return build(params)


def _expected_source_hostname(
    scope: Optional[Literal["host", "global"]],
    hostname: Optional[str],
    enforce_hostname: bool,
) -> Optional[str]:
    return hostname if enforce_hostname and scope != "global" else None


def _source_bucket_ids(
    bucket_ids: List[str],
    host: Optional[str],
    bucket_hosts: Optional[Dict[str, str]],
    scope: Optional[Literal["host", "global"]],
    hostname: Optional[str],
    source_kind: str,
) -> List[str]:
    if bucket_hosts == {}:
        bucket_hosts = None
    if any(not isinstance(bucket_id, str) or not bucket_id for bucket_id in bucket_ids):
        raise ValueError(f"{source_kind} source bucket_ids must be non-empty strings")
    if len(bucket_ids) != len(set(bucket_ids)):
        raise ValueError(f"{source_kind} source contains duplicate bucket_ids")
    if host is not None and (not isinstance(host, str) or not host):
        raise ValueError(f"{source_kind} source host must be non-empty")
    if scope not in (None, "host", "global"):
        raise ValueError(f"{source_kind} source scope must be 'host' or 'global'")

    has_host = host is not None
    has_bucket_hosts = bucket_hosts is not None
    if has_host and has_bucket_hosts:
        raise ValueError(
            f"{source_kind} source ownership must use host or bucket_hosts, not both"
        )
    if bucket_hosts is not None:
        if not isinstance(bucket_hosts, dict):
            raise ValueError(f"{source_kind} source bucket_hosts must be a mapping")
        if set(bucket_hosts) != set(bucket_ids) or any(
            not isinstance(mapped_host, str) or not mapped_host
            for mapped_host in bucket_hosts.values()
        ):
            raise ValueError(
                f"{source_kind} source bucket_hosts must map every bucket exactly once"
            )

    resolved_scope = scope
    if resolved_scope is None:
        if has_host or has_bucket_hosts:
            resolved_scope = "host"
        else:
            raise ValueError(
                f"{source_kind} source scope is required without ownership metadata"
            )

    if resolved_scope == "global":
        if has_host or has_bucket_hosts:
            raise ValueError(
                f"{source_kind} global source must not define host or bucket_hosts"
            )
        return bucket_ids

    if not has_host and not has_bucket_hosts:
        raise ValueError(
            f"{source_kind} host source requires host or complete bucket_hosts"
        )
    if not hostname:
        raise ValueError(f"{source_kind} host source requires a query hostname")
    if host is not None:
        return bucket_ids if host == hostname else []
    assert bucket_hosts is not None
    return [
        bucket_id for bucket_id in bucket_ids if bucket_hosts[bucket_id] == hostname
    ]


def _validate_unique_source_ids(sources: List[Any], source_kind: str) -> None:
    source_ids = [source.source_id for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError(f"{source_kind} source ids must be unique")


def _active_time_rule_source_ids(rule: Any) -> Set[str]:
    if not isinstance(rule, dict):
        return set()
    source_ids = set()
    rule_type = rule.get("type")
    if rule_type is None and "regex" in rule:
        rule_type = "regex"
    if rule_type == "regex" and isinstance(rule.get("source"), str):
        source_ids.add(rule["source"])
    if rule_type in ("all", "any"):
        children = rule.get("rules") if "rules" in rule else rule.get("children")
        if isinstance(children, list):
            for child in children:
                source_ids.update(_active_time_rule_source_ids(child))
    return source_ids


CURRENT_BROWSER_FAMILIES: Dict[str, Dict[str, Any]] = {
    "chrome": {
        "names": ["com.google.Chrome", "com.google.ChromeDev", "org.chromium.Chromium"],
        "regex": r"(?i)^(google[-_ ]?chrome|chrome|chromium)",
    },
    "firefox": {
        "names": [
            "org.mozilla.firefox",
            "io.gitlab.librewolf-community",
            "net.waterfox.waterfox",
        ],
        "regex": r"(?i)(firefox|librewolf|waterfox|nightly)",
    },
    "opera": {"names": ["com.opera.Opera"], "regex": r"(?i)(opera)"},
    "brave": {"names": ["com.brave.Browser"], "regex": r"(?i)(brave)"},
    "edge": {
        "names": ["com.microsoft.Edge", "com.microsoft.EdgeDev"],
        "regex": r"(?i)^(microsoft[-_ ]?edge|msedge)",
    },
    "arc": {"names": [], "regex": r"(?i)^arc(\.exe)?$"},
    "vivaldi": {"names": ["com.vivaldi.Vivaldi"], "regex": r"(?i)(vivaldi)"},
    "orion": {"names": ["Orion"], "regex": r"(?i)(orion)"},
    "yandex": {"names": ["ru.yandex.Browser"], "regex": r"(?i)(yandex)"},
    "zen": {"names": ["app.zen_browser.zen"], "regex": r"(?i)(zen)"},
    "floorp": {"names": ["one.ablaze.floorp"], "regex": r"(?i)(floorp)"},
    "helium": {"names": ["net.imput.helium"], "regex": r"(?i)(helium)"},
}


def current_browser_focus_rule(source_id: str, family: str) -> Dict[str, Any]:
    family_match = CURRENT_BROWSER_FAMILIES[family]
    rules = (
        [
            {
                "type": "regex",
                "source": source_id,
                "field": "app",
                "regex": "^(?:"
                + "|".join(re.escape(name) for name in family_match["names"])
                + ")$",
            }
        ]
        if family_match["names"]
        else []
    )
    rules.append(
        {
            "type": "regex",
            "source": source_id,
            "field": "app",
            "regex": family_match["regex"],
        }
    )
    return rules[0] if len(rules) == 1 else {"type": "any", "rules": rules}


def _browser_in_buckets(browser: str, browserbuckets: List[str]) -> Optional[str]:
    for bucket in browserbuckets:
        if browser in bucket:
            return bucket
    return None


def current_browser_families(
    browser_buckets: List[str],
) -> List[Tuple[str, List[str]]]:
    """Group selected buckets by current browser family, preserving bucket order."""
    return [
        (browser_name, family_buckets)
        for browser_name in CURRENT_BROWSER_FAMILIES
        if (
            family_buckets := [
                bucket_id for bucket_id in browser_buckets if browser_name in bucket_id
            ]
        )
    ]


def browsersWithBuckets(browserbuckets: List[str]) -> List[Tuple[str, str]]:
    """Return first-family buckets for the explicit legacy target."""
    browsername_to_bucketid: List[Tuple[str, Optional[str]]] = [
        (browserName, _browser_in_buckets(browserName, browserbuckets))
        for browserName in browser_appnames
    ]

    # Only return browsers for which a bucket could be found
    return [t for t in browsername_to_bucketid if t[1]]  # type: ignore


browser_appnames = {
    "chrome": [
        # Chrome
        "Google Chrome",
        "Google-chrome",
        "chrome.exe",
        "google-chrome-stable",
        # Chromium
        "Chromium",
        "Chromium-browser",
        "Chromium-browser-chromium",
        "chromium.exe",
        # Pre-releases
        "Google-chrome-beta",
        "Google-chrome-unstable",
        # Brave (should this be merged with the brave entry?)
        "Brave-browser",
    ],
    "firefox": [
        "Firefox",
        "Firefox.exe",
        "firefox",
        "firefox.exe",
        "Firefox Developer Edition",
        "firefoxdeveloperedition",
        "Firefox-esr",
        "Firefox Beta",
        "Nightly",
        "org.mozilla.firefox",
    ],
    "opera": ["opera.exe", "Opera"],
    "brave": ["brave.exe"],
    "edge": [
        "msedge.exe",  # Windows
        "Microsoft Edge",  # macOS
    ],
    "vivaldi": ["Vivaldi-stable", "Vivaldi-snapshot", "vivaldi.exe"],
}

default_limit = 100


def querystr_to_array(querystr: str) -> List[str]:
    return [line + ";" for line in querystr.split(";") if line]


def escape_doublequote(s: str) -> str:
    return s.replace('"', '\\"')


def _serialize_query_json(value: Any) -> str:
    def validate(item: Any, path: str = "value") -> None:
        if isinstance(item, str):
            trailing_backslashes = len(item) - len(item.rstrip("\\"))
            if trailing_backslashes % 2 == 1:
                raise ValueError(
                    f"{path} cannot end with an odd number of backslashes in Query2"
                )
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                validate(child, f"{path}[{index}]")
        elif isinstance(item, dict):
            for key, child in item.items():
                validate(key, f"{path} key")
                validate(child, f"{path}.{key}")

    validate(value)
    serialized = json.dumps(
        value, cls=EnhancedJSONEncoder, separators=(",", ":"), ensure_ascii=False
    )

    def collapse_even_backslashes(match: re.Match) -> str:
        count = len(match.group(0))
        return "\\" * (count // 2 if count % 2 == 0 else count)

    return re.sub(r"\\+", collapse_even_backslashes, serialized)


def serialize_query2_literal(value: Any) -> str:
    """Serialize a Python value for Query2's raw-backslash literal grammar."""
    return _serialize_query_json(value)


def _serialize_bucket_id(bucket_id: str) -> str:
    trailing_backslashes = len(bucket_id) - len(bucket_id.rstrip("\\"))
    if trailing_backslashes % 2 == 1:
        raise ValueError(
            "bucket ID cannot end with an odd number of backslashes in Query2"
        )
    return _serialize_query_json(bucket_id)


def _full_desktop_query_from_canonical(
    params: DesktopQueryParams, canonical_query: str
) -> str:
    if (
        not params.bid_window
        and not params.activity_coverage_sources
        and not params.activity_sources
        and not params.background_sources
    ):
        raise ValueError("fullDesktopQuery requires bid_window or an activity source")
    # Build the base query
    query = f"""
    {canonical_query}
    title_events = sort_by_duration(merge_events_by_keys(events, ["app", "title"]));
    app_events   = sort_by_duration(merge_events_by_keys(title_events, ["app"]));
    cat_events   = sort_by_duration(merge_events_by_keys(events, ["$category"]));
    app_events  = limit_events(app_events, {default_limit});
    title_events  = limit_events(title_events, {default_limit});
    duration = sum_durations(events);
    """

    # Add browser-related query parts if browser buckets exist
    if params.bid_browsers:
        query += f"""
        browser_urls = merge_events_by_keys(browser_events, ["url"]);
        browser_urls = sort_by_duration(browser_urls);
        browser_urls = limit_events(browser_urls, {default_limit});
        browser_domains = merge_events_by_keys(browser_events, ["$domain"]);
        browser_domains = sort_by_duration(browser_domains);
        browser_domains = limit_events(browser_domains, {default_limit});
        browser_duration = sum_durations(browser_events);
        """
    else:
        query += """
        browser_events = [];
        browser_urls = [];
        browser_domains = [];
        browser_duration = 0;
        """

    # Add the return statement
    query += """
        RETURN = {
            "events": events,
            "window": {
                "app_events": app_events,
                "title_events": title_events,
                "cat_events": cat_events,
                "active_events": not_afk,
                "duration": duration
            },
            "browser": {
                "domains": browser_domains,
                "urls": browser_urls,
                "duration": browser_duration
            }
        };
    """
    return query


def fullDesktopQuery(params: DesktopQueryParams) -> str:
    """Project the current canonical desktop stream into the legacy report shape."""
    return _full_desktop_query_from_canonical(params, resolveActivityProfile(params))


def test_fullDesktopQuery():
    params = DesktopQueryParams(
        bid_window="aw-watcher-window_",
        bid_afk="aw-watcher-afk_",
    )
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(days=7)
    end = now
    timeperiods = [(start, end)]
    query = fullDesktopQuery(params)

    awc = aw_client.ActivityWatchClient("test")
    res = awc.query(query, timeperiods)[0]
    events = res["events"]
    print(len(events))


if __name__ == "__main__":
    test_fullDesktopQuery()
