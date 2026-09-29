"""Settings-aware compiler for the canonical flexible-rules query pipeline.

This module deliberately depends only on the native client and query builder.  It
loads settings, capabilities, and buckets from the *same* ActivityWatchClient;
it never consults the WebUI runtime or the default localhost server.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import quote

from .classes import default_category_settings

from .queries import (
    ActiveTimeSource,
    ActivityCoverageSource,
    CanonicalQueryParamsV2,
    ContextSource,
    canonicalEventsV2,
    current_browser_families,
    current_browser_focus_rule,
    semantic_category_specs,
    semantic_rule_expression,
)

BUILTIN_WINDOW_SOURCE_ID = "builtin_window"
BUILTIN_BROWSER_SOURCE_ID = "browser"
BUILTIN_STOPWATCH_SOURCE_ID = "stopwatch"
AFK_SOURCE_ID = "afk"
RULES_SCHEMA_VERSION = 2
MAX_CATEGORY_RULES = 1000
MAX_EXPRESSION_NODES = 4096
MAX_EXPRESSION_DEPTH = 32
MAX_RULE_SOURCES = 128
MAX_REGEX_LENGTH = 4096
MAX_RANKING_INTEGER = 1_000_000
MAX_SAFE_INTEGER = 2**53 - 1
SOURCE_DEFAULTS_VERSION = 4

@dataclass
class MaterializedProfileV2:
    profile_id: str
    params: CanonicalQueryParamsV2
    app_title_source_id: Optional[str] = None
    browser_focus_source_id: Optional[str] = None

    def query(self) -> str:
        return canonicalEventsV2(self.params)


def flattened_rule_id(set_id: str, rule_id: str) -> str:
    """Collision-free stable ID shared by the native implementations."""
    return f"set:{len(set_id.encode('utf-8'))}:{set_id}:rule:{rule_id}"


def flatten_category_sets(
    profile: Dict[str, Any], category_sets: Iterable[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    by_id = {item.get("id"): item for item in category_sets}
    selected = profile.get("category_set_ids")
    if not isinstance(selected, list) or not selected:
        raise ValueError("activity profile must select at least one category set")
    result: List[Dict[str, Any]] = []
    for set_index, set_id in enumerate(selected):
        category_set = by_id.get(set_id)
        if category_set is None:
            raise ValueError(f"activity profile references unavailable category set {set_id!r}")
        categories = category_set.get("categories")
        if not isinstance(categories, list):
            raise ValueError(f"category set {set_id!r} has invalid categories")
        local_ids = {
            item.get("id")
            for item in categories
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for category in categories:
            if not isinstance(category, dict):
                raise ValueError(f"category set {set_id!r} contains a non-object rule")
            rule_id = category.get("id")
            if not isinstance(rule_id, str) or not rule_id:
                raise ValueError(f"category set {set_id!r} contains a rule without an id")
            item = copy.deepcopy(category)
            item["id"] = flattened_rule_id(set_id, rule_id)
            requirements = item.get("requires", [])
            if not isinstance(requirements, list) or any(req not in local_ids for req in requirements):
                raise ValueError(f"category rule {rule_id!r} has an invalid prerequisite")
            if requirements:
                item["requires"] = [flattened_rule_id(set_id, req) for req in requirements]
            # First selected set wins a tie. Engines which predate set_priority
            # reject current documents by capability/version rather than silently
            # dropping this ordering dimension.
            set_priority = category_set.get("priority", len(selected) - set_index)
            _validate_ranking_integer(
                set_priority, f"category set {set_id!r} priority"
            )
            item["set_priority"] = set_priority
            result.append(item)
    return result


def _source_ids(expression: Any) -> Set[str]:
    if not isinstance(expression, dict):
        return set()
    kind = expression.get("type", "regex" if "regex" in expression else None)
    if kind == "regex":
        source = expression.get("source")
        return {source} if isinstance(source, str) and source else set()
    if kind in ("all", "any"):
        children = expression.get("rules", expression.get("children", []))
        result: Set[str] = set()
        if isinstance(children, list):
            for child in children:
                result.update(_source_ids(child))
        return result
    return set()


def _qualify_legacy_expression(expression: Any, source_id: str) -> Dict[str, Any]:
    if not isinstance(expression, dict):
        return {"type": "none"}
    result = copy.deepcopy(expression)
    kind = result.get("type", "regex" if "regex" in result else None)
    if kind is None:
        return {"type": "none"}
    if kind == "regex":
        result.setdefault("type", "regex")
        result.setdefault("source", source_id)
        keys = result.pop("select_keys", None)
        if keys and not result.get("field") and not result.get("fields"):
            result["field" if len(keys) == 1 else "fields"] = keys[0] if len(keys) == 1 else keys
    elif kind in ("all", "any"):
        key = "rules" if "rules" in result else "children"
        result["rules"] = [
            _qualify_legacy_expression(child, source_id) for child in result.pop(key, [])
        ]
    return result


def _default_sources() -> List[Dict[str, Any]]:
    return [
        {
            "id": BUILTIN_WINDOW_SOURCE_ID,
            "label": "App & window",
            "bucket_ids": [],
            "builtin": "window",
            "fields": ["app", "title"],
            "creates_activity": True,
            "interval_policy": "heartbeat",
        },
        {
            "id": BUILTIN_BROWSER_SOURCE_ID,
            "label": "Browser tabs",
            "bucket_ids": [],
            "builtin": "browser",
            "fields": ["title", "url", "audible", "incognito", "tabCount"],
            "interval_policy": "heartbeat",
        },
        {
            "id": BUILTIN_STOPWATCH_SOURCE_ID,
            "label": "Stopwatch",
            "bucket_ids": [],
            "builtin": "stopwatch",
            "fields": ["label"],
            "creates_activity": True,
            "keeps_active": True,
            "interval_policy": "exact",
        },
    ]


def _stable_category_id(set_id: str, name: List[str]) -> str:
    encoded = "/".join(quote(part, safe="~()*!.'-_") for part in name)
    return f"{set_id}:{encoded}"


def _migrate_category_set(categories: Any, set_id: str) -> Dict[str, Any]:
    if not isinstance(categories, list):
        raise ValueError(f"legacy category set {set_id!r} categories must be an array")
    migrated = []
    for item in categories:
        if not isinstance(item, dict):
            raise ValueError(f"legacy category set {set_id!r} contains a non-object category")
        name = copy.deepcopy(item.get("name"))
        if not isinstance(name, list) or not name or any(
            not isinstance(part, str) or not part for part in name
        ):
            raise ValueError(f"legacy category set {set_id!r} contains an invalid category name")
        rule = _qualify_legacy_expression(item.get("rule"), BUILTIN_WINDOW_SOURCE_ID)
        if rule.get("type") == "regex":
            rule.setdefault("weight", 0)
        category = {
            "id": _stable_category_id(set_id, name),
            "name": name,
            "rule": rule,
            "simple_ui": True,
        }
        if item.get("data") is not None:
            category["data"] = copy.deepcopy(item["data"])
        migrated.append(category)
    return {"schema_version": RULES_SCHEMA_VERSION, "id": set_id, "categories": migrated}


def _migrate_legacy_category_sets(
    settings: Dict[str, Any]
) -> Optional[tuple[List[Dict[str, Any]], List[str]]]:
    raw_sets = settings.get("category_sets")
    if not isinstance(raw_sets, list) or not raw_sets:
        return None
    legacy_sets = []
    for item in raw_sets:
        if not isinstance(item, dict):
            continue
        set_id = item.get("id")
        if isinstance(set_id, str) and set_id and isinstance(item.get("categories"), list):
            legacy_sets.append((set_id, item["categories"]))
    if not legacy_sets:
        return None
    known = {set_id for set_id, _ in legacy_sets}
    requested = settings.get("active_set_ids")
    active_ids = []
    if isinstance(requested, list):
        for item in requested:
            if isinstance(item, str) and item in known and item not in active_ids:
                active_ids.append(item)
    if not active_ids:
        active_ids = [legacy_sets[0][0]]
    originals = [_migrate_category_set(categories, set_id) for set_id, categories in legacy_sets]
    return originals, active_ids


def _initialize_predecessor_profiles(profiles: Any) -> Any:
    if not isinstance(profiles, list):
        return profiles
    result = copy.deepcopy(profiles)
    for profile in result:
        if not isinstance(profile, dict):
            continue
        version = profile.get("source_defaults_version", 0)
        if isinstance(version, bool) or not isinstance(version, int):
            continue
        sources = profile.get("sources")
        if not isinstance(sources, list) or version >= SOURCE_DEFAULTS_VERSION:
            continue
        if version < 2:
            defaults = _default_sources()
            if not any(isinstance(source, dict) and source.get("builtin") == "window" for source in sources):
                sources.insert(0, defaults[0])
            if not any(isinstance(source, dict) and source.get("id") == BUILTIN_BROWSER_SOURCE_ID for source in sources):
                sources.append(defaults[1])
            if not any(isinstance(source, dict) and source.get("id") == BUILTIN_STOPWATCH_SOURCE_ID for source in sources):
                sources.append(defaults[2])
        if version < 3:
            stopwatch = next(
                (
                    source
                    for source in sources
                    if isinstance(source, dict)
                    and (source.get("builtin") == "stopwatch" or source.get("id") == BUILTIN_STOPWATCH_SOURCE_ID)
                ),
                None,
            )
            if stopwatch and stopwatch.get("creates_activity") and "keeps_active" not in stopwatch:
                stopwatch["keeps_active"] = True
        if version < 4:
            for source in sources:
                if isinstance(source, dict):
                    source.setdefault(
                        "interval_policy",
                        "heartbeat" if source.get("builtin") in ("window", "browser") else "exact",
                    )
        profile["source_defaults_version"] = SOURCE_DEFAULTS_VERSION
        profile.setdefault("app_title_source_id", BUILTIN_WINDOW_SOURCE_ID)
        profile.setdefault("browser_focus_source_id", BUILTIN_WINDOW_SOURCE_ID)
    return result


def migrate_legacy_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the predecessor ladder in memory without writing settings."""
    profiles = settings.get("activity_profiles_v2")
    sets = settings.get("category_sets_v2")
    if isinstance(profiles, list) and isinstance(sets, list):
        document = {
            "revision": 0,
            "activity_profiles_v2": _initialize_predecessor_profiles(profiles),
            "category_sets_v2": copy.deepcopy(sets),
        }
        _validate_rules_document(document)
        return document

    migrated_sets = _migrate_legacy_category_sets(settings)
    if migrated_sets is not None:
        category_sets, selected_set_ids = migrated_sets
    else:
        legacy_classes = (
            settings["classes"]
            if "classes" in settings and isinstance(settings["classes"], list)
            else default_category_settings
        )
        selected_set_ids = ["default"]
        category_sets = [_migrate_category_set(legacy_classes, selected_set_ids[0])]

    profile = {
        "schema_version": RULES_SCHEMA_VERSION,
        "source_defaults_version": SOURCE_DEFAULTS_VERSION,
        "id": "default",
        "category_set_ids": selected_set_ids,
        "sources": _default_sources(),
        "app_title_source_id": BUILTIN_WINDOW_SOURCE_ID,
        "browser_focus_source_id": BUILTIN_WINDOW_SOURCE_ID,
        "active_time": {
            "type": "legacy",
            "use_afk": True,
            "include_audible": True,
            "always_active_pattern": settings.get("always_active_pattern", ""),
        },
    }
    return {
        "revision": 0,
        "activity_profiles_v2": [profile],
        "category_sets_v2": category_sets,
    }


def _validate_ranking_integer(value: Any, path: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not -MAX_RANKING_INTEGER <= value <= MAX_RANKING_INTEGER
    ):
        raise ValueError(
            f"{path} must be an integer between {-MAX_RANKING_INTEGER} and {MAX_RANKING_INTEGER}"
        )


def _validate_expression_shape(
    expression: Any,
    path: str,
    state: Dict[str, Any],
    depth: int = 1,
) -> None:
    if not isinstance(expression, dict):
        raise ValueError(f"{path} must be an object")
    if "weight" in expression:
        _validate_ranking_integer(expression["weight"], f"{path}.weight")
    if depth > MAX_EXPRESSION_DEPTH:
        raise ValueError(f"{path} exceeds maximum depth of {MAX_EXPRESSION_DEPTH}")
    state["nodes"] += 1
    if state["nodes"] > MAX_EXPRESSION_NODES:
        raise ValueError(f"{path} exceeds maximum node count of {MAX_EXPRESSION_NODES}")
    kind = expression.get("type")
    if kind == "none":
        return
    if kind == "regex":
        pattern = expression.get("regex")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{path}.regex must be a non-empty string")
        if len(pattern) > MAX_REGEX_LENGTH:
            raise ValueError(f"{path}.regex exceeds maximum length of {MAX_REGEX_LENGTH}")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(f"{path}.regex is invalid: {error}") from error
        source = expression.get("source")
        if source is not None:
            if not isinstance(source, str) or not source:
                raise ValueError(f"{path}.source must be a non-empty string")
            state["sources"].add(source)
            if len(state["sources"]) > MAX_RULE_SOURCES:
                raise ValueError(f"{path} exceeds maximum source count of {MAX_RULE_SOURCES}")
        return
    if kind in ("all", "any"):
        rules = expression.get("rules", expression.get("children"))
        if not isinstance(rules, list) or not rules:
            raise ValueError(f"{path}.rules must be a non-empty array")
        for index, child in enumerate(rules):
            _validate_expression_shape(child, f"{path}.rules[{index}]", state, depth + 1)
        return
    raise ValueError(f"{path}.type is unsupported")


def _validate_rules_document(document: Any) -> Dict[str, Any]:
    """Validate canonical/pre-atomic v2 data before materialization.

    Servers deliberately expose malformed historical/forward data for recovery,
    so native clients must not deserialize or execute it as the current schema.
    """
    if not isinstance(document, dict):
        raise ValueError("rules_v2 must be an object")
    revision = document.get("revision")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 0 <= revision <= MAX_SAFE_INTEGER
    ):
        raise ValueError("rules_v2.revision must be a non-negative safe integer")
    profiles = document.get("activity_profiles_v2")
    sets = document.get("category_sets_v2")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("rules_v2.activity_profiles_v2 must be a non-empty array")
    if not isinstance(sets, list) or not sets:
        raise ValueError("rules_v2.category_sets_v2 must be a non-empty array")

    sets_by_id: Dict[str, Dict[str, Any]] = {}
    for set_index, category_set in enumerate(sets):
        path = f"rules_v2.category_sets_v2[{set_index}]"
        if not isinstance(category_set, dict):
            raise ValueError(f"{path} must be an object")
        if category_set.get("schema_version") != RULES_SCHEMA_VERSION:
            raise ValueError(f"{path}.schema_version must be {RULES_SCHEMA_VERSION}")
        set_id = category_set.get("id")
        if not isinstance(set_id, str) or not set_id:
            raise ValueError(f"{path}.id must be a non-empty string")
        if set_id in sets_by_id:
            raise ValueError(f"{path}.id is duplicated")
        if "priority" in category_set:
            _validate_ranking_integer(category_set["priority"], f"{path}.priority")
        categories = category_set.get("categories")
        if not isinstance(categories, list):
            raise ValueError(f"{path}.categories must be an array")
        if len(categories) > MAX_CATEGORY_RULES:
            raise ValueError(f"{path}.categories exceeds maximum count of {MAX_CATEGORY_RULES}")
        ids: Set[str] = set()
        state: Dict[str, Any] = {"nodes": 0, "sources": set()}
        for category_index, category in enumerate(categories):
            category_path = f"{path}.categories[{category_index}]"
            if not isinstance(category, dict):
                raise ValueError(f"{category_path} must be an object")
            category_id = category.get("id")
            if not isinstance(category_id, str) or not category_id:
                raise ValueError(f"{category_path}.id must be a non-empty string")
            if category_id in ids:
                raise ValueError(f"{category_path}.id is duplicated")
            ids.add(category_id)
            for ranking_field in ("priority", "set_priority"):
                if ranking_field in category:
                    _validate_ranking_integer(
                        category[ranking_field], f"{category_path}.{ranking_field}"
                    )
            name = category.get("name")
            if not isinstance(name, list) or not name or any(
                not isinstance(segment, str) or not segment for segment in name
            ):
                raise ValueError(f"{category_path}.name must contain non-empty strings")
            requirements = category.get("requires", [])
            if not isinstance(requirements, list) or any(
                not isinstance(item, str) or not item for item in requirements
            ):
                raise ValueError(f"{category_path}.requires must be an array of rule ids")
            _validate_expression_shape(category.get("rule"), f"{category_path}.rule", state)
        for category_index, category in enumerate(categories):
            for requirement in category.get("requires", []):
                if requirement not in ids:
                    raise ValueError(
                        f"{path}.categories[{category_index}].requires references unknown id {requirement}"
                    )
        sets_by_id[set_id] = category_set

    profile_ids: Set[str] = set()
    for profile_index, profile in enumerate(profiles):
        path = f"rules_v2.activity_profiles_v2[{profile_index}]"
        if not isinstance(profile, dict):
            raise ValueError(f"{path} must be an object")
        if profile.get("schema_version") != RULES_SCHEMA_VERSION:
            raise ValueError(f"{path}.schema_version must be {RULES_SCHEMA_VERSION}")
        profile_id = profile.get("id")
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError(f"{path}.id must be a non-empty string")
        if profile_id in profile_ids:
            raise ValueError(f"{path}.id is duplicated")
        profile_ids.add(profile_id)
        selected = profile.get("category_set_ids")
        if not isinstance(selected, list) or not selected or any(
            not isinstance(item, str) or not item for item in selected
        ):
            raise ValueError(f"{path}.category_set_ids must be a non-empty string array")
        if len(set(selected)) != len(selected):
            raise ValueError(f"{path}.category_set_ids must be unique")
        missing = next((item for item in selected if item not in sets_by_id), None)
        if missing is not None:
            raise ValueError(f"{path}.category_set_ids references unknown set {missing}")
        if "source_defaults_version" in profile:
            defaults_version = profile["source_defaults_version"]
            if (
                isinstance(defaults_version, bool)
                or not isinstance(defaults_version, int)
                or defaults_version < 0
            ):
                raise ValueError(f"{path}.source_defaults_version must be a non-negative integer")
        sources = profile.get("sources")
        if not isinstance(sources, list):
            raise ValueError(f"{path}.sources must be an array")
        if len(sources) > MAX_RULE_SOURCES:
            raise ValueError(f"{path}.sources exceeds maximum count of {MAX_RULE_SOURCES}")
        source_ids: Set[str] = set()
        for source_index, source in enumerate(sources):
            source_path = f"{path}.sources[{source_index}]"
            if not isinstance(source, dict):
                raise ValueError(f"{source_path} must be an object")
            source_id = source.get("id")
            if not isinstance(source_id, str) or not source_id:
                raise ValueError(f"{source_path}.id must be a non-empty string")
            if re.fullmatch(r"[A-Za-z0-9_-]+", source_id) is None:
                raise ValueError(f"{source_path}.id contains unsupported characters")
            if not isinstance(source.get("label"), str) or not source.get("label"):
                raise ValueError(f"{source_path}.label must be a non-empty string")
            if source_id in source_ids:
                raise ValueError(f"{source_path}.id is duplicated")
            source_ids.add(source_id)
            bucket_ids = source.get("bucket_ids")
            if not isinstance(bucket_ids, list) or any(
                not isinstance(bucket_id, str) or not bucket_id for bucket_id in bucket_ids
            ):
                raise ValueError(f"{source_path}.bucket_ids must be a string array")
            if len(set(bucket_ids)) != len(bucket_ids):
                raise ValueError(f"{source_path}.bucket_ids must be unique")
            fields = source.get("fields")
            if not isinstance(fields, list) or not fields or any(
                not isinstance(field, str) or not field for field in fields
            ):
                raise ValueError(f"{source_path}.fields must be a non-empty string array")
            if len(set(fields)) != len(fields):
                raise ValueError(f"{source_path}.fields must be unique")
            builtin = source.get("builtin")
            expected_builtin_ids = {
                "window": BUILTIN_WINDOW_SOURCE_ID,
                "browser": BUILTIN_BROWSER_SOURCE_ID,
                "stopwatch": BUILTIN_STOPWATCH_SOURCE_ID,
            }
            if "builtin" in source and (
                not isinstance(builtin, str)
                or builtin not in expected_builtin_ids
                or source_id != expected_builtin_ids[builtin]
            ):
                raise ValueError(f"{source_path}.builtin is unsupported for this source id")
            if builtin is None and not bucket_ids:
                raise ValueError(f"{source_path}.bucket_ids must be non-empty for a custom source")
            if "interval_policy" in source and source["interval_policy"] not in (
                "exact",
                "heartbeat",
            ):
                raise ValueError(f"{source_path}.interval_policy must be exact or heartbeat")
            field_types = source.get("field_types")
            if "field_types" in source:
                if not isinstance(field_types, dict):
                    raise ValueError(f"{source_path}.field_types must be an object")
                if not set(field_types).issubset(fields):
                    raise ValueError(
                        f"{source_path}.field_types may only describe configured fields"
                    )
                if any(value not in ("string", "scalar") for value in field_types.values()):
                    raise ValueError(f"{source_path}.field_types values must be string or scalar")
            for flag in ("creates_activity", "keeps_active", "auto_generated"):
                if flag in source and not isinstance(source[flag], bool):
                    raise ValueError(f"{source_path}.{flag} must be boolean")
            if source.get("keeps_active") and not source.get("creates_activity"):
                raise ValueError(f"{source_path}.keeps_active requires creates_activity")
            if "host" in source and (
                not isinstance(source["host"], str) or not source["host"]
            ):
                raise ValueError(f"{source_path}.host must be a non-empty string")
            bucket_hosts = source.get("bucket_hosts")
            if "bucket_hosts" in source:
                if not isinstance(bucket_hosts, dict):
                    raise ValueError(f"{source_path}.bucket_hosts must be an object")
                if any(not isinstance(host, str) or not host for host in bucket_hosts.values()):
                    raise ValueError(f"{source_path}.bucket_hosts values must be non-empty strings")
            scope = source.get("scope")
            if "scope" in source and scope not in ("host", "global"):
                raise ValueError(f"{source_path}.scope must be host or global")
            if scope is None and ("host" in source or bucket_hosts is not None):
                scope = "host"
            if scope == "global" and ("host" in source or bucket_hosts is not None):
                raise ValueError(f"{source_path} global scope cannot define host ownership")
            if scope == "host" and "host" in source and bucket_hosts is not None:
                raise ValueError(
                    f"{source_path} host scope must use either host or bucket_hosts"
                )
            # Discovery fills bucket ownership for empty builtin declarations.
            if builtin is not None and not bucket_ids:
                continue
            if bucket_hosts is not None and set(bucket_hosts) != set(bucket_ids):
                raise ValueError(
                    f"{source_path}.bucket_hosts must map every and only configured bucket id"
                )
            if scope not in ("host", "global"):
                raise ValueError(f"{source_path}.scope must be host or global")
            if scope == "host" and "host" not in source and bucket_hosts is None:
                raise ValueError(f"{source_path} host scope requires host ownership")
        for selector in ("app_title_source_id", "browser_focus_source_id"):
            if selector in profile:
                value = profile[selector]
                if not isinstance(value, str) or not value:
                    raise ValueError(f"{path}.{selector} must be a non-empty string")
                if value not in source_ids:
                    raise ValueError(f"{path}.{selector} references an unknown source")
        active_time = profile.get("active_time")
        if not isinstance(active_time, dict):
            raise ValueError(f"{path}.active_time must be an object")
        active_type = active_time.get("type")
        if active_type == "expression":
            state = {"nodes": 0, "sources": set()}
            _validate_expression_shape(active_time.get("rule"), f"{path}.active_time.rule", state)
            unknown = state["sources"] - source_ids
            if unknown:
                raise ValueError(f"{path}.active_time.rule references unknown source {sorted(unknown)[0]}")
        elif active_type == "legacy":
            for key in ("use_afk", "include_audible"):
                if not isinstance(active_time.get(key), bool):
                    raise ValueError(f"{path}.active_time.{key} must be boolean")
            if not isinstance(active_time.get("always_active_pattern"), str):
                raise ValueError(
                    f"{path}.active_time.always_active_pattern must be a string"
                )
        else:
            raise ValueError(f"{path}.active_time.type must be legacy or expression")

        aggregate_count = sum(len(sets_by_id[set_id]["categories"]) for set_id in selected)
        if aggregate_count > MAX_CATEGORY_RULES:
            raise ValueError(
                f"{path}.category_set_ids select {aggregate_count} category rules; maximum is {MAX_CATEGORY_RULES}"
            )
        aggregate_state = {"nodes": 0, "sources": set()}
        for set_id in selected:
            for category_index, category in enumerate(sets_by_id[set_id]["categories"]):
                _validate_expression_shape(
                    category.get("rule"),
                    f"{path}.category_set_ids[{set_id}].categories[{category_index}].rule",
                    aggregate_state,
                )
        if not aggregate_state["sources"].issubset(source_ids):
            missing_source = sorted(aggregate_state["sources"] - source_ids)[0]
            raise ValueError(f"{path}.category_set_ids reference unknown source {missing_source}")
    return copy.deepcopy(document)


def load_rules_document(
    client: Any, canonical_supported: bool = True
) -> Dict[str, Any]:
    if canonical_supported:
        try:
            document = client.get_setting("rules_v2")
        except Exception as error:
            response = getattr(error, "response", None)
            if response is None or getattr(response, "status_code", None) != 404:
                raise
            document = migrate_legacy_settings(client.get_setting())
    else:
        document = migrate_legacy_settings(client.get_setting())
    return _validate_rules_document(document)


def _bucket_items(buckets: Any) -> List[Dict[str, Any]]:
    if not isinstance(buckets, dict):
        raise ValueError("bucket listing must be an object")
    result = []
    for bucket_id, value in buckets.items():
        item = dict(value) if isinstance(value, dict) else dict(vars(value))
        item.setdefault("id", bucket_id)
        result.append(item)
    return result


def _bucket_host(bucket: Dict[str, Any]) -> Optional[str]:
    data = bucket.get("data")
    return bucket.get("hostname") or (data.get("hostname") if isinstance(data, dict) else None)


def _discover_builtin(kind: str, buckets: List[Dict[str, Any]], hostname: str) -> tuple[List[str], str]:
    event_type = {
        "window": "currentwindow",
        "browser": "web.tab.current",
        "stopwatch": "general.stopwatch",
    }[kind]
    candidates = [
        bucket for bucket in buckets
        if bucket.get("type") == event_type
        and not (kind == "window" and str(bucket.get("id", "")).startswith("aw-watcher-android"))
    ]
    owned = [bucket for bucket in candidates if _bucket_host(bucket) == hostname]
    selected = owned
    if kind in ("browser", "stopwatch") and not selected:
        selected = [bucket for bucket in candidates if _bucket_host(bucket) == "unknown"]
    # Server bucket maps do not promise insertion order. Stable lexical order is
    # shared with the Rust/WebUI current-profile discoverers so overlap
    # precedence cannot vary between processes.
    return sorted(str(bucket["id"]) for bucket in selected), ("host" if owned else "global")


def _materialize_source(
    source: Dict[str, Any], buckets: List[Dict[str, Any]], hostname: str
) -> Optional[Dict[str, Any]]:
    item = copy.deepcopy(source)
    builtin = item.get("builtin")
    bucket_ids = item.get("bucket_ids")
    if not isinstance(bucket_ids, list):
        raise ValueError(f"source {item.get('id')!r} bucket_ids must be a list")
    if builtin and not bucket_ids:
        bucket_ids, scope = _discover_builtin(builtin, buckets, hostname)
        item["bucket_ids"] = bucket_ids
        item["scope"] = scope
        item.pop("bucket_hosts", None)
        if scope == "host":
            item["host"] = hostname
        else:
            item.pop("host", None)
    if not item["bucket_ids"]:
        return None
    item.setdefault(
        "interval_policy",
        "heartbeat" if builtin in ("window", "browser") else "exact",
    )
    return item


def _allocate_generated_source_id(used_ids: Set[str], base: str) -> str:
    candidate = base
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def _append_active_source(
    active_sources: List[ActiveTimeSource], source: ActiveTimeSource
) -> None:
    existing = next(
        (candidate for candidate in active_sources if candidate.source_id == source.source_id),
        None,
    )
    if existing is None:
        active_sources.append(source)
    elif existing != source:
        raise ValueError(f"distinct active sources use id {source.source_id!r}")


def _active_source(source: Dict[str, Any]) -> ActiveTimeSource:
    return ActiveTimeSource(
        source_id=source["id"],
        bucket_ids=list(source["bucket_ids"]),
        host=source.get("host"),
        bucket_hosts=copy.deepcopy(source.get("bucket_hosts")),
        scope=source.get("scope"),
        interval_policy=source.get("interval_policy", "exact"),
    )


def compile_profile_v2(
    document: Dict[str, Any],
    buckets: Any,
    capabilities: List[str],
    *,
    hostname: str,
    profile_id: Optional[str] = None,
    filter_afk: bool = True,
    filter_categories: Optional[List[List[str]]] = None,
    explain_categories: bool = False,
) -> MaterializedProfileV2:
    document = _validate_rules_document(document)
    profiles = document["activity_profiles_v2"]
    profile = next(
        (item for item in profiles if profile_id is None or item.get("id") == profile_id), None
    )
    if profile is None:
        raise ValueError(f"activity profile {profile_id!r} is unavailable")
    required = {
        "query.query_bucket_optional_raw.v1",
        "query.query_period.v1",
        "query.flood_v2.v1",
        "query.merge_subwatcher_fields.source_namespace.v1",
        "query.active_periods_v2.v1",
        "query.categorize_v2.v1",
    }
    missing = required.difference(capabilities)
    if missing:
        raise ValueError(
            "server cannot execute settings-aware profile; missing capabilities: "
            + ", ".join(sorted(missing))
        )
    profile = copy.deepcopy(profile)
    declared_sources = {
        source["id"]: copy.deepcopy(source) for source in profile.get("sources", [])
    }
    materialized = [
        item
        for source in profile.get("sources", [])
        if (item := _materialize_source(source, _bucket_items(buckets), hostname)) is not None
    ]
    source_by_id = {source.get("id"): source for source in materialized}
    if len(source_by_id) != len(materialized):
        raise ValueError("activity profile contains duplicate source ids")

    coverage = []
    context = []
    for source in materialized:
        common = dict(
            source_id=source["id"],
            bucket_ids=list(source["bucket_ids"]),
            fields=list(source.get("fields", [])),
            host=source.get("host"),
            bucket_hosts=copy.deepcopy(source.get("bucket_hosts")),
            scope=source.get("scope"),
            interval_policy=source.get("interval_policy", "exact"),
        )
        if source.get("creates_activity"):
            coverage.append(
                ActivityCoverageSource(**common, keeps_active=bool(source.get("keeps_active")))
            )
        else:
            context.append(ContextSource(**common, conflict="base_wins"))

    active = profile.get("active_time", {})
    generated_source_ids = set(declared_sources)
    afk_source_id = (
        _allocate_generated_source_id(generated_source_ids, AFK_SOURCE_ID)
        if active.get("type") == "legacy" and active.get("use_afk")
        else None
    )
    active_rule: Optional[Dict[str, Any]] = None
    active_sources: List[ActiveTimeSource] = []
    if active.get("type") == "expression":
        active_rule = copy.deepcopy(active.get("rule"))
        has_keeps_active = any(source.keeps_active for source in coverage)
        for source_id in sorted(_source_ids(active_rule)):
            source = source_by_id.get(source_id)
            if source is None:
                declared = declared_sources.get(source_id)
                if (
                    not isinstance(declared, dict)
                    or declared.get("builtin") is None
                    or (filter_afk and not has_keeps_active)
                ):
                    raise ValueError(
                        f"active-time rule references unavailable source {source_id!r}"
                    )
                source = copy.deepcopy(declared)
                source["bucket_ids"] = []
                source["scope"] = "global"
                source.pop("host", None)
                source.pop("bucket_hosts", None)
            _append_active_source(active_sources, _active_source(source))
    elif active.get("type") == "legacy":
        branches: List[Dict[str, Any]] = []
        if active.get("use_afk"):
            afk_ids = sorted(
                bucket["id"]
                for bucket in _bucket_items(buckets)
                if bucket.get("type") == "afkstatus" and _bucket_host(bucket) == hostname
            )
            if afk_ids:
                assert afk_source_id is not None
                _append_active_source(
                    active_sources,
                    ActiveTimeSource(
                        afk_source_id,
                        afk_ids,
                        host=hostname,
                        scope="host",
                        interval_policy="heartbeat",
                    )
                )
                branches.append(
                    {"type": "regex", "source": afk_source_id, "field": "status", "regex": "^not-afk$"}
                )
        pattern = active.get("always_active_pattern")
        window = source_by_id.get(BUILTIN_WINDOW_SOURCE_ID)
        if pattern and window:
            _append_active_source(active_sources, _active_source(window))
            fields = [field for field in window.get("fields", []) if field in ("app", "title")]
            rules = [
                {"type": "regex", "source": window["id"], "field": field, "regex": pattern}
                for field in fields
            ]
            if rules:
                branches.append(rules[0] if len(rules) == 1 else {"type": "any", "rules": rules})
        # Preserve legacy audible semantics per browser family. A Firefox tab
        # must not become active merely because Chrome is focused. The optional
        # selector falls back only to the configured builtin window source.
        browser = source_by_id.get(BUILTIN_BROWSER_SOURCE_ID)
        explicit_focus_id = profile.get("browser_focus_source_id")
        focus = (
            source_by_id.get(explicit_focus_id)
            if explicit_focus_id is not None
            else next(
                (source for source in materialized if source.get("builtin") == "window"),
                None,
            )
        )
        if (
            active.get("include_audible")
            and browser
            and focus
            and "audible" in browser.get("fields", [])
            and "app" in focus.get("fields", [])
        ):
            audible_branches: List[Dict[str, Any]] = []
            for index, (family, family_bucket_ids) in enumerate(
                current_browser_families(list(browser["bucket_ids"]))
            ):
                audible_source_id = _allocate_generated_source_id(
                    generated_source_ids, f"browser_audible_{index}"
                )
                bucket_hosts = browser.get("bucket_hosts")
                family_source = copy.deepcopy(browser)
                family_source["id"] = audible_source_id
                family_source["bucket_ids"] = family_bucket_ids
                if isinstance(bucket_hosts, dict):
                    family_source["bucket_hosts"] = {
                        bucket_id: bucket_hosts[bucket_id]
                        for bucket_id in family_bucket_ids
                    }
                _append_active_source(active_sources, _active_source(family_source))
                focus_rule = current_browser_focus_rule(focus["id"], family)
                audible_branches.append(
                    {
                        "type": "all",
                        "rules": [
                            {
                                "type": "regex",
                                "source": audible_source_id,
                                "field": "audible",
                                "regex": "^true$",
                                "value_mode": "scalar",
                            },
                            focus_rule,
                        ],
                    }
                )
            if audible_branches:
                _append_active_source(active_sources, _active_source(focus))
                branches.append(
                    audible_branches[0]
                    if len(audible_branches) == 1
                    else {"type": "any", "rules": audible_branches}
                )
        if branches:
            active_rule = branches[0] if len(branches) == 1 else {"type": "any", "rules": branches}
    else:
        raise ValueError("activity profile has an unsupported active_time type")

    category_specs = flatten_category_sets(profile, document["category_sets_v2"])
    # Migrated unsourced rules are always made explicit; canonical stored v2
    # rules are expected to already identify their source.
    for category in category_specs:
        category["rule"] = _qualify_legacy_expression(category.get("rule"), BUILTIN_WINDOW_SOURCE_ID)
    if active_rule is not None:
        active_rule = semantic_rule_expression(active_rule)
    category_specs = semantic_category_specs(category_specs)

    params = CanonicalQueryParamsV2(
        activity_coverage_sources=coverage,
        context_sources=context,
        active_time_rule=active_rule,
        active_time_sources=active_sources,
        category_specs=category_specs,
        hostname=hostname,
        capabilities=capabilities,
        filter_afk=filter_afk,
        filter_categories=filter_categories,
        explain_categories=explain_categories,
    )
    return MaterializedProfileV2(
        profile_id=profile["id"],
        params=params,
        app_title_source_id=profile.get("app_title_source_id"),
        browser_focus_source_id=profile.get("browser_focus_source_id"),
    )
