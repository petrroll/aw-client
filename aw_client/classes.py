"""
Default classes

Taken from default classes in aw-webui
"""

import logging
import random
from typing import (
    Any,
    Dict,
    List,
    Tuple,
)

import aw_client

logger = logging.getLogger(__name__)

CategoryId = List[str]
CategorySpec = Dict[str, Any]

default_category_settings: List[Dict[str, Any]] = [
    {
        "name": ["Work"],
        "rule": {"type": "regex", "regex": "Google Docs|libreoffice|ReText"},
        "data": {"color": "#0F0", "score": 10},
    },
    {
        "name": ["Work", "Programming"],
        "rule": {
            "type": "regex",
            "regex": "GitHub|Stack Overflow|BitBucket|Gitlab|vim|Spyder|kate|Ghidra|Scite",
        },
    },
    {
        "name": ["Work", "Programming", "ActivityWatch"],
        "rule": {"type": "regex", "regex": "ActivityWatch|aw-", "ignore_case": True},
    },
    {"name": ["Work", "Image"], "rule": {"type": "regex", "regex": "GIMP|Inkscape"}},
    {"name": ["Work", "Video"], "rule": {"type": "regex", "regex": "Kdenlive"}},
    {"name": ["Work", "Audio"], "rule": {"type": "regex", "regex": "Audacity"}},
    {"name": ["Work", "3D"], "rule": {"type": "regex", "regex": "Blender"}},
    {"name": ["Media"], "rule": {"type": "none"}, "data": {"color": "#F33"}},
    {
        "name": ["Media", "Games"],
        "rule": {"type": "regex", "regex": "Minecraft|RimWorld"},
        "data": {"color": "#F80"},
    },
    {
        "name": ["Media", "Video"],
        "rule": {"type": "regex", "regex": "YouTube|Plex|VLC"},
        "data": {"color": "#F33"},
    },
    {
        "name": ["Media", "Social Media"],
        "rule": {
            "type": "regex",
            "regex": "reddit|Facebook|Twitter|Instagram|devRant",
            "ignore_case": True,
        },
        "data": {"color": "#FCC400"},
    },
    {
        "name": ["Media", "Music"],
        "rule": {"type": "regex", "regex": "Spotify|Deezer", "ignore_case": True},
        "data": {"color": "#A8FC00"},
    },
    {"name": ["Comms"], "rule": {"type": "none"}, "data": {"color": "#9FF"}},
    {
        "name": ["Comms", "IM"],
        "rule": {
            "type": "regex",
            "regex": "Messenger|Telegram|Signal|WhatsApp|Rambox|Slack|Riot|Element|Discord|Nheko|NeoChat|Mattermost",
        },
    },
    {
        "name": ["Comms", "Email"],
        "rule": {"type": "regex", "regex": "Gmail|Thunderbird|mutt|alpine"},
    },
    {"name": ["Uncategorized"], "rule": {"type": "none"}, "data": {"color": "#CCC"}},
]

# Frozen query-builder callers use the historical tuple representation.
default_classes: List[Tuple[CategoryId, CategorySpec]] = [
    (item["name"], item["rule"]) for item in default_category_settings
]


def get_classes() -> List[Tuple[List[str], dict]]:
    """
    Get classes from server-side settings.
    Might throw a 404 if not set yet, in which case we use the default classes as a fallback.
    """
    # NOTE: Always tries to fetch from prod server,
    #       which is potentially wrong if testing server is being used.
    awc = aw_client.ActivityWatchClient(f"get-setting-{random.randint(0, 10000)}")
    try:
        classes = awc.get_setting("classes")
    except Exception:
        logger.warning(
            "Failed to get classes from server, using default classes as fallback"
        )
        return default_classes
    if not classes:
        logger.warning(
            "Classes setting is empty/unset, using default classes as fallback"
        )
        return default_classes
    # map into list of tuples
    return [(v["name"], v["rule"]) for v in classes]
