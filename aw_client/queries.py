"""
Common queries.

Most of these are from: https://github.com/ActivityWatch/aw-webui/blob/master/src/queries.ts
"""

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
    Tuple,
    Union,
)

from typing_extensions import TypeGuard

import aw_client

from .classes import get_classes


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


@dataclass
class ActiveTimeSource:
    source_id: str
    bucket_ids: List[str]
    host: Optional[str] = None
    bucket_hosts: Optional[Dict[str, str]] = None
    scope: Optional[Literal["host", "global"]] = None


@dataclass
class ActivitySource:
    source_id: str
    bucket_ids: List[str]
    field_mappings: Dict[str, str] = field(default_factory=dict)
    host: Optional[str] = None
    bucket_hosts: Optional[Dict[str, str]] = None
    scope: Optional[Literal["host", "global"]] = None


@dataclass
class ActivityCoverageSource:
    source_id: str
    bucket_ids: List[str]
    fields: List[str]
    host: Optional[str] = None
    bucket_hosts: Optional[Dict[str, str]] = None
    scope: Optional[Literal["host", "global"]] = None


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


def resolveActivityProfile(
    params: Union[DesktopQueryParams, AndroidQueryParams],
) -> str:
    if params.hostname == "":
        raise ValueError("hostname must be non-empty")
    if not isDesktopParams(params) and params.active_time_rule:
        raise ValueError(
            "active-time expressions are only supported for desktop queries"
        )
    if not isDesktopParams(params) and params.activity_sources:
        raise ValueError(
            "replacement activity sources are only supported for desktop queries"
        )
    if not isDesktopParams(params) and params.background_sources:
        raise ValueError(
            "background activity sources are only supported for desktop queries"
        )
    if not isDesktopParams(params) and params.activity_coverage_sources:
        raise ValueError(
            "activity coverage sources are only supported for desktop queries"
        )
    if (
        params.category_specs is not None
        and "query.categorize_v2.v1" not in params.capabilities
    ):
        raise ValueError(
            "flexible categorization requires server capability query.categorize_v2.v1"
        )
    if (
        (params.context_sources or params.activity_coverage_sources)
        and "query.merge_subwatcher_fields.source_namespace.v1"
        not in params.capabilities
    ):
        raise ValueError(
            "context enrichment requires server capability "
            "query.merge_subwatcher_fields.source_namespace.v1"
        )
    if (
        isDesktopParams(params)
        and params.active_time_rule
        and "query.active_periods_v2.v1" not in params.capabilities
    ):
        raise ValueError(
            "active-time expressions require server capability "
            "query.active_periods_v2.v1"
        )
    if (
        params.activity_sources or params.background_sources
    ) and "query.map_event_fields.v1" not in params.capabilities:
        raise ValueError(
            "activity sources require server capability query.map_event_fields.v1"
        )
    if isDesktopParams(params):
        supports_source_namespace = (
            "query.merge_subwatcher_fields.source_namespace.v1" in params.capabilities
        )
        if params.legacy_window_mode not in ("activity", "context", "none"):
            raise ValueError(
                "legacy_window_mode must be 'activity', 'context', or 'none'"
            )
        if (
            params.legacy_window_mode == "context"
            and params.bid_window
            and not supports_source_namespace
        ):
            raise ValueError(
                "legacy window context requires server capability "
                "query.merge_subwatcher_fields.source_namespace.v1"
            )
        if params.bid_window == "":
            raise ValueError("bid_window must be non-empty when supplied")
        if params.bid_afk == "":
            raise ValueError("bid_afk must be non-empty when supplied")
        if params.always_active_pattern and not params.bid_afk:
            raise ValueError("always_active_pattern requires bid_afk")
        if (
            params.background_sources
            and not params.active_time_rule
            and not params.bid_afk
        ):
            raise ValueError(
                "background activity sources require bid_afk or an active-time "
                "expression"
            )
        if (
            params.filter_afk
            and (
                params.bid_window
                or params.activity_coverage_sources
                or params.activity_sources
            )
            and not params.active_time_rule
            and not params.bid_afk
        ):
            raise ValueError(
                "AFK filtering requires bid_afk or an active-time expression"
            )

    if params.category_specs is None and not params.classes:
        # if categories not explicitly set,
        # get categories from server settings
        params.classes = get_classes()

    # Query2 strings preserve raw backslashes instead of decoding JSON escapes.
    classes_str = _serialize_query_json(params.classes)
    category_specs_str = _serialize_query_json(params.category_specs or [])
    has_active_time_rule = isDesktopParams(params) and bool(params.active_time_rule)
    enforce_source_hostname = (
        "query.query_bucket_optional.expected_hostname.v1" in params.capabilities
    )

    cat_filter_str = _serialize_query_json(params.filter_classes)

    if isDesktopParams(params):
        activity_code = _legacy_activity_events(
            params.bid_window,
            params.hostname,
            params.legacy_window_mode,
            supports_source_namespace,
        )
        if has_active_time_rule:
            active_code = activeTimeEvents(params)
        elif params.bid_afk:
            active_code = _legacy_active_time_events(
                params.bid_afk,
                params.hostname,
                params.always_active_pattern,
            )
        else:
            active_code = "not_afk = [];"
        browser_code = (
            browserEvents(params)
            + (
                """
            audible_events = filter_keyvals(browser_events, "audible", [true]);
            not_afk = period_union(not_afk, audible_events);
            """
                if params.include_audible and not has_active_time_rule
                else ""
            )
            if params.bid_browsers
            else ""
        )
        platform_code = "\n".join(
            [
                activity_code,
                activityCoverageEvents(
                    params.activity_coverage_sources,
                    params.hostname,
                    enforce_source_hostname,
                ),
                (
                    "events = merge_subwatcher_fields("
                    "events, legacy_activity, "
                    f"{_serialize_query_json(params.legacy_window_fields)});"
                    if (
                        params.bid_window
                        and params.legacy_window_mode != "none"
                        and supports_source_namespace
                    )
                    else ""
                ),
                activityEvents(
                    params.activity_sources,
                    False,
                    params.hostname,
                    enforce_source_hostname,
                ),
                active_code,
                browser_code,
                (
                    "events = filter_period_intersect(events, not_afk);"
                    if params.filter_afk
                    else ""
                ),
                backgroundActivityEvents(
                    params.background_sources,
                    params.hostname,
                    enforce_source_hostname,
                ),
            ]
        )
    else:
        assert isAndroidParams(params)
        platform_code = "\n".join(
            [
                (
                    "events = flood(query_bucket(find_bucket("
                    f"{_serialize_bucket_id(params.bid_android)}"
                    + (
                        f", {_serialize_query_json(params.hostname)}"
                        if params.hostname
                        else ""
                    )
                    + ")));"
                ),
            ]
        )

    return "\n".join(
        [
            platform_code,
            contextEvents(
                params.context_sources, params.hostname, enforce_source_hostname
            ),
            (
                f"events = categorize_v2(events, {category_specs_str}"
                + (
                    f", {_serialize_query_json(params.hostname)}"
                    if params.hostname
                    else ""
                )
                + ");"
                if params.category_specs is not None
                else (
                    f"events = categorize(events, {classes_str});"
                    if params.classes
                    else ""
                )
            ),
            (
                f'events = filter_keyvals(events, "$category", {cat_filter_str});'
                if params.filter_classes
                else ""
            ),
        ]
    )


def canonicalEvents(params: Union[DesktopQueryParams, AndroidQueryParams]) -> str:
    """Compatibility alias for existing query-builder consumers."""
    return resolveActivityProfile(params)


def pretty_query(query: str) -> str:
    return "\n".join([line.strip() for line in query.split("\n") if line.strip()])


def _legacy_bucket_query(bucket_id: str, hostname: Optional[str]) -> str:
    hostname_arg = f", {_serialize_query_json(hostname)}" if hostname else ""
    return (
        f"query_bucket(find_bucket({_serialize_bucket_id(bucket_id)}"
        f"{hostname_arg}))"
    )


def _legacy_activity_events(
    bid_window: Optional[str],
    hostname: Optional[str],
    mode: Literal["activity", "context", "none"] = "activity",
    supports_source_namespace: bool = False,
) -> str:
    code = "events = [];"
    if bid_window and mode != "none":
        code += "\nlegacy_activity = flood("
        code += f"{_legacy_bucket_query(bid_window, hostname)});\n"
        if mode == "activity":
            if supports_source_namespace:
                code += (
                    "legacy_activity_period = filter_period_intersect("
                    "legacy_activity, legacy_activity);\n"
                    "events = period_union(events, legacy_activity_period);"
                )
            else:
                code += "events = legacy_activity;"
    return code


def _legacy_active_time_events(
    bid_afk: str,
    hostname: Optional[str],
    always_active_pattern: Optional[str] = None,
) -> str:
    code = (
        "not_afk = flood("
        f"{_legacy_bucket_query(bid_afk, hostname)});\n"
        'not_afk = filter_keyvals(not_afk, "status", ["not-afk"]);'
    )
    if always_active_pattern:
        pattern = _serialize_query_json(always_active_pattern)
        code += (
            f'\nnot_treat_as_afk = filter_keyvals_regex(events, "app", {pattern});'
            "\nnot_afk = period_union(not_afk, not_treat_as_afk);"
            f'\nnot_treat_as_afk = filter_keyvals_regex(events, "title", {pattern});'
            "\nnot_afk = period_union(not_afk, not_treat_as_afk);"
        )
    return code


def _optional_bucket_query(
    bucket_id: str, expected_hostname: Optional[str] = None
) -> str:
    hostname = (
        f", {_serialize_query_json(expected_hostname)}" if expected_hostname else ""
    )
    return f"query_bucket_optional({_serialize_bucket_id(bucket_id)}{hostname})"


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


def contextEvents(
    sources: List[ContextSource],
    hostname: Optional[str] = None,
    enforce_hostname: bool = False,
) -> str:
    _validate_unique_source_ids(sources, "context")
    code = ""
    for index, source in enumerate(sources):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", source.source_id):
            raise ValueError(
                "context source_id may only contain letters, numbers, '_' and '-'"
            )
        if not source.bucket_ids:
            raise ValueError("context source must contain at least one bucket_id")
        if not source.fields:
            raise ValueError("context source must contain at least one field")
        variable = f"context_{index}"
        code += f"{variable} = [];\n"
        bucket_ids = _source_bucket_ids(
            source.bucket_ids,
            source.host,
            source.bucket_hosts,
            source.scope,
            hostname,
            "context",
        )
        for bucket_id in bucket_ids:
            code += (
                f"{variable} = concat({variable}, "
                f"flood({_optional_bucket_query(bucket_id, _expected_source_hostname(source.scope, hostname, enforce_hostname))}));\n"
            )
        code += f"{variable} = filter_period_intersect({variable}, events);\n"
        options = _serialize_query_json(
            {"source_id": source.source_id, "conflict": source.conflict}
        )
        fields = _serialize_query_json(source.fields)
        fields_variable = f"context_fields_{index}"
        options_variable = f"context_options_{index}"
        code += (
            f"{fields_variable} = {fields};\n"
            f"{options_variable} = {options};\n"
            f"events = merge_subwatcher_fields("
            f"events, {variable}, {fields_variable}, {options_variable});\n"
        )
    return code


def activityEvents(
    sources: List[ActivitySource],
    filter_afk: bool,
    hostname: Optional[str] = None,
    enforce_hostname: bool = False,
) -> str:
    code = ""
    for index, source in enumerate(sources):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", source.source_id):
            raise ValueError(
                "activity source_id may only contain letters, numbers, '_' and '-'"
            )
        if not source.bucket_ids:
            raise ValueError("activity source must contain at least one bucket_id")
        if any(
            not target or not source_field
            for target, source_field in source.field_mappings.items()
        ):
            raise ValueError("activity source field mappings may not be empty")
        variable = f"activity_source_{index}"
        code += f"{variable} = [];\n"
        bucket_ids = _source_bucket_ids(
            source.bucket_ids,
            source.host,
            source.bucket_hosts,
            source.scope,
            hostname,
            "activity",
        )
        for bucket_index, bucket_id in enumerate(bucket_ids):
            code += (
                f"activity_bucket_{index}_{bucket_index} = "
                f"flood({_optional_bucket_query(bucket_id, _expected_source_hostname(source.scope, hostname, enforce_hostname))});\n"
                f"{variable} = union_no_overlap("
                f"{variable}, activity_bucket_{index}_{bucket_index});\n"
            )
        if filter_afk:
            code += f"{variable} = filter_period_intersect({variable}, not_afk);\n"
        if source.field_mappings:
            mappings = _serialize_query_json(source.field_mappings)
            code += f"{variable} = map_event_fields({variable}, {mappings});\n"
        code += f"{variable} = sort_by_timestamp({variable});\n"
        code += f"events = union_no_overlap({variable}, events);\n"
    return code


def activityCoverageEvents(
    sources: List[ActivityCoverageSource],
    hostname: Optional[str] = None,
    enforce_hostname: bool = False,
) -> str:
    _validate_unique_source_ids(sources, "activity coverage")
    code = ""
    for index, source in enumerate(sources):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", source.source_id):
            raise ValueError(
                "activity coverage source_id may only contain letters, numbers, "
                "'_' and '-'"
            )
        if not source.bucket_ids:
            raise ValueError(
                "activity coverage source must contain at least one bucket_id"
            )
        if not source.fields:
            raise ValueError("activity coverage source must contain at least one field")
        variable = f"activity_coverage_source_{index}"
        code += f"{variable} = [];\n"
        bucket_ids = _source_bucket_ids(
            source.bucket_ids,
            source.host,
            source.bucket_hosts,
            source.scope,
            hostname,
            "activity coverage",
        )
        for bucket_index, bucket_id in enumerate(bucket_ids):
            bucket_variable = f"activity_coverage_bucket_{index}_{bucket_index}"
            code += (
                f"{bucket_variable} = flood({_optional_bucket_query(bucket_id, _expected_source_hostname(source.scope, hostname, enforce_hostname))});\n"
                f"{variable} = union_no_overlap({variable}, {bucket_variable});\n"
            )
        code += (
            f"activity_coverage_period_{index} = "
            f"filter_period_intersect({variable}, {variable});\n"
        )
        code += f"events = period_union(events, activity_coverage_period_{index});\n"

    for index, source in enumerate(sources):
        variable = f"activity_coverage_source_{index}"
        fields_variable = f"activity_coverage_fields_{index}"
        options_variable = f"activity_coverage_options_{index}"
        code += f"{variable} = filter_period_intersect({variable}, events);\n"
        code += f"{fields_variable} = {_serialize_query_json(source.fields)};\n"
        code += (
            f"{options_variable} = "
            f"{_serialize_query_json({'source_id': source.source_id, 'conflict': 'base_wins'})};\n"
        )
        code += (
            f"events = merge_subwatcher_fields("
            f"events, {variable}, {fields_variable}, {options_variable});\n"
        )
    return code


def backgroundActivityEvents(
    sources: List[ActivitySource],
    hostname: Optional[str] = None,
    enforce_hostname: bool = False,
) -> str:
    code = ""
    for index, source in enumerate(sources):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", source.source_id):
            raise ValueError(
                "background activity source_id may only contain letters, numbers, "
                "'_' and '-'"
            )
        if not source.bucket_ids:
            raise ValueError(
                "background activity source must contain at least one bucket_id"
            )
        if any(
            not target or not source_field
            for target, source_field in source.field_mappings.items()
        ):
            raise ValueError(
                "background activity source field mappings may not be empty"
            )
        variable = f"background_source_{index}"
        code += f"{variable} = [];\n"
        bucket_ids = _source_bucket_ids(
            source.bucket_ids,
            source.host,
            source.bucket_hosts,
            source.scope,
            hostname,
            "background activity",
        )
        for bucket_index, bucket_id in enumerate(bucket_ids):
            bucket_variable = f"background_bucket_{index}_{bucket_index}"
            code += (
                f"{bucket_variable} = "
                f"flood({_optional_bucket_query(bucket_id, _expected_source_hostname(source.scope, hostname, enforce_hostname))});\n"
                f"{variable} = union_no_overlap("
                f"{variable}, {bucket_variable});\n"
            )
        code += f"{variable} = filter_period_intersect({variable}, not_afk);\n"
        if source.field_mappings:
            mappings = _serialize_query_json(source.field_mappings)
            code += f"{variable} = map_event_fields({variable}, {mappings});\n"
        code += f"{variable} = sort_by_timestamp({variable});\n"
        code += f"events = union_no_overlap(events, {variable});\n"
    return code


def _active_time_events(
    sources: List[ActiveTimeSource],
    rule_spec: Dict[str, Any],
    hostname: Optional[str],
    enforce_hostname: bool = False,
) -> str:
    if not sources:
        raise ValueError("active-time expressions require at least one source")

    code = ""
    named_sources = []
    for index, source in enumerate(sources):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", source.source_id):
            raise ValueError(
                "active-time source_id may only contain letters, numbers, '_' and '-'"
            )
        if not source.bucket_ids:
            raise ValueError("active-time source must contain at least one bucket_id")
        variable = f"active_source_{index}"
        code += f"{variable} = [];\n"
        bucket_ids = _source_bucket_ids(
            source.bucket_ids,
            source.host,
            source.bucket_hosts,
            source.scope,
            hostname,
            "active-time",
        )
        for bucket_id in bucket_ids:
            code += (
                f"{variable} = concat({variable}, "
                f"flood({_optional_bucket_query(bucket_id, _expected_source_hostname(source.scope, hostname, enforce_hostname))}));\n"
            )
        named_sources.append(f'["{source.source_id}", {variable}]')

    rule = _serialize_query_json(rule_spec)
    code += f"active_time_rule = {rule};\n"
    code += f"active_time_sources = [{', '.join(named_sources)}];\n"
    code += (
        "not_afk = active_periods_v2("
        "active_time_sources, active_time_rule"
        + (f", {_serialize_query_json(hostname)}" if hostname else "")
        + ");\n"
    )
    code += "not_afk = period_union(not_afk, []);\n"
    return code


def activeTimeEvents(params: DesktopQueryParams) -> str:
    if not params.active_time_rule:
        return ""
    return _active_time_events(
        params.active_time_sources,
        params.active_time_rule,
        params.hostname,
        "query.query_bucket_optional.expected_hostname.v1" in params.capabilities,
    )


def activeTimeQuery(
    active_time_sources: List[ActiveTimeSource],
    active_time_rule: Dict[str, Any],
    hostname: Optional[str] = None,
    capabilities: Optional[List[str]] = None,
) -> str:
    if hostname == "":
        raise ValueError("hostname must be non-empty")
    return (
        _active_time_events(
            active_time_sources,
            active_time_rule,
            hostname,
            "query.query_bucket_optional.expected_hostname.v1" in (capabilities or []),
        )
        + "RETURN = not_afk;"
    )


def legacyActiveTimeQuery(bid_afk: str, hostname: Optional[str] = None) -> str:
    if not bid_afk:
        raise ValueError("bid_afk must be non-empty")
    if hostname == "":
        raise ValueError("hostname must be non-empty")
    return _legacy_active_time_events(bid_afk, hostname) + "\nRETURN = not_afk;"


def activityQuery(afk_buckets: List[str]) -> str:
    code = "not_afk = [];\n"
    for index, bucket_id in enumerate(afk_buckets):
        if not bucket_id:
            raise ValueError("AFK bucket IDs must be non-empty")
        variable = f"not_afk_{index}"
        code += (
            f"{variable} = query_bucket({_serialize_bucket_id(bucket_id)});\n"
            f'{variable} = filter_keyvals({variable}, "status", ["not-afk"]);\n'
            f"not_afk = union_no_overlap(not_afk, {variable});\n"
        )
    code += 'not_afk = merge_events_by_keys(not_afk, ["status"]);\n'
    code += "RETURN = not_afk;"
    return code


def _browser_in_buckets(browser: str, browserbuckets: List[str]) -> Optional[str]:
    for bucket in browserbuckets:
        if browser in bucket:
            return bucket
    return None


def browsersWithBuckets(browserbuckets: List[str]) -> List[Tuple[str, str]]:
    """Returns a list of (browserName, bucketId) pairs for found browser buckets"""
    browsername_to_bucketid: List[Tuple[str, Optional[str]]] = [
        (browserName, _browser_in_buckets(browserName, browserbuckets))
        for browserName in browser_appnames
    ]

    # Only return browsers for which a bucket could be found
    return [t for t in browsername_to_bucketid if t[1]]  # type: ignore


def browserEvents(params: DesktopQueryParams) -> str:
    """Returns a list of active browser events (where the browser was the active window) from all browser buckets"""
    code = "browser_events = [];"

    for browserName, bucketId in browsersWithBuckets(params.bid_browsers):
        browser_appnames_str = _serialize_query_json(browser_appnames[browserName])
        bucket_id_str = _serialize_bucket_id(bucketId)
        code += f"""
          events_{browserName} = flood(query_bucket({bucket_id_str}));
          window_{browserName} = filter_keyvals(events, "app", {browser_appnames_str});
          events_{browserName} = filter_period_intersect(events_{browserName}, window_{browserName});
          events_{browserName} = split_url_events(events_{browserName});
          browser_events = concat(browser_events, events_{browserName});
          browser_events = sort_by_timestamp(browser_events);
        """
    return code


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


def _serialize_bucket_id(bucket_id: str) -> str:
    trailing_backslashes = len(bucket_id) - len(bucket_id.rstrip("\\"))
    if trailing_backslashes % 2 == 1:
        raise ValueError(
            "bucket ID cannot end with an odd number of backslashes in Query2"
        )
    return _serialize_query_json(bucket_id)


def fullDesktopQuery(
    params: DesktopQueryParams,
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
    {resolveActivityProfile(params)}
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
        browser_events = split_url_events(browser_events);
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
