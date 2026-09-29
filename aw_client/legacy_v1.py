"""Query2 builders for explicitly selected old-server and Android targets."""

import re
from typing import Any, Dict, List, Literal, Optional, Union

from .classes import get_classes
from .queries import (
    ActiveTimeSource,
    ActivityCoverageSource,
    ActivitySource,
    AndroidQueryParams,
    ContextSource,
    DesktopQueryParams,
    _active_time_rule_source_ids,
    _expected_source_hostname,
    _full_desktop_query_from_canonical,
    _serialize_bucket_id,
    _serialize_query_json,
    _source_bucket_ids,
    _validate_unique_source_ids,
    browser_appnames,
    browsersWithBuckets,
    isAndroidParams,
    isDesktopParams,
)


def full_desktop_query(params: DesktopQueryParams) -> str:
    """Build the legacy report projection for an explicit old-server target."""
    return _full_desktop_query_from_canonical(params, canonical_events(params))


def canonical_events(params: Union[DesktopQueryParams, AndroidQueryParams]) -> str:
    """Build the frozen legacy Query2 grammar for an explicit old target."""
    if params.hostname == "":
        raise ValueError("hostname must be non-empty")
    if not isDesktopParams(params) and params.active_time_rule:
        raise ValueError("active-time expressions are only supported for desktop queries")
    if not isDesktopParams(params) and params.activity_sources:
        raise ValueError("replacement activity sources are only supported for desktop queries")
    if not isDesktopParams(params) and params.background_sources:
        raise ValueError("background activity sources are only supported for desktop queries")
    if not isDesktopParams(params) and params.activity_coverage_sources:
        raise ValueError("activity coverage sources are only supported for desktop queries")
    if params.category_specs is not None and "query.categorize_v2.v1" not in params.capabilities:
        raise ValueError("flexible categorization requires server capability query.categorize_v2.v1")
    if (
        (params.context_sources or params.activity_coverage_sources)
        and "query.merge_subwatcher_fields.source_namespace.v1" not in params.capabilities
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
            "active-time expressions require server capability query.active_periods_v2.v1"
        )
    if (
        params.activity_sources or params.background_sources
    ) and "query.map_event_fields.v1" not in params.capabilities:
        raise ValueError("activity sources require server capability query.map_event_fields.v1")

    if isDesktopParams(params):
        supports_source_namespace = (
            "query.merge_subwatcher_fields.source_namespace.v1" in params.capabilities
        )
        if params.legacy_window_mode not in ("activity", "context", "none"):
            raise ValueError("legacy_window_mode must be 'activity', 'context', or 'none'")
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
        if params.background_sources and not params.active_time_rule and not params.bid_afk:
            raise ValueError(
                "background activity sources require bid_afk or an active-time expression"
            )
        if (
            params.filter_afk
            and (params.bid_window or params.activity_coverage_sources or params.activity_sources)
            and not params.active_time_rule
            and not params.bid_afk
        ):
            raise ValueError("AFK filtering requires bid_afk or an active-time expression")

    if params.category_specs is None and not params.classes:
        params.classes = get_classes()

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
                params.bid_afk, params.hostname, params.always_active_pattern
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
                    params.activity_sources, False, params.hostname, enforce_source_hostname
                ),
                active_code,
                browser_code,
                (
                    activityCoverageActiveOverrides(params.activity_coverage_sources)
                    if params.filter_afk
                    else ""
                ),
                (
                    "events = filter_period_intersect(events, not_afk);"
                    if params.filter_afk
                    else ""
                ),
                backgroundActivityEvents(
                    params.background_sources, params.hostname, enforce_source_hostname
                ),
            ]
        )
    else:
        assert isAndroidParams(params)
        platform_code = (
            "events = flood(query_bucket(find_bucket("
            f"{_serialize_bucket_id(params.bid_android)}"
            + (f", {_serialize_query_json(params.hostname)}" if params.hostname else "")
            + ")));"
        )

    return "\n".join(
        [
            platform_code,
            contextEvents(params.context_sources, params.hostname, enforce_source_hostname),
            (
                f"events = categorize_v2(events, {category_specs_str}"
                + (f", {_serialize_query_json(params.hostname)}" if params.hostname else "")
                + ");"
                if params.category_specs is not None
                else (f"events = categorize(events, {classes_str});" if params.classes else "")
            ),
            (
                f'events = filter_keyvals(events, "$category", {cat_filter_str});'
                if params.filter_classes
                else ""
            ),
        ]
    )


# Legacy source loaders and raw Query2 compatibility helpers.

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

def activityCoverageActiveOverrides(
    sources: List[ActivityCoverageSource],
) -> str:
    return "\n".join(
        f"not_afk = period_union(not_afk, activity_coverage_period_{index});"
        for index, source in enumerate(sources)
        if source.keeps_active
    )

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
    _validate_unique_source_ids(sources, "active-time")
    missing_sources = _active_time_rule_source_ids(rule_spec) - {
        source.source_id for source in sources
    }
    if missing_sources:
        raise ValueError(
            "active-time rule references unknown source(s): "
            + ", ".join(sorted(missing_sources))
        )

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
