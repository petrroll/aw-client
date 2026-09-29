"""
Lists the most common words among uncategorized events, by duration, to help in creating categories.

This might make more sense as a notebook.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
from tabulate import tabulate
from typing import Dict

from aw_core import Event
import aw_client

# set up client
awc = aw_client.ActivityWatchClient("test")


def example_categories():
    # TODO: Use tools in aw-research to load categories from toml file
    return [
        (
            ("Work", "ActivityWatch"),
            {"type": "regex", "regex": "aw-|activitywatch", "ignore_case": True},
        ),
    ]


def get_events():
    """Retrieve configured canonical events which remain Uncategorized."""

    start = datetime(2022, 1, 1, tzinfo=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    timeperiods = [(start, now)]

    profile = awc.build_profile_query_v2()
    source_id = profile.app_title_source_id
    if source_id is None:
        raise ValueError("the selected activity profile has no title presentation source")
    title_field = f"$source.{source_id}.title"
    res = awc.query(
        f"""
        {profile.query()}
        events = filter_keyvals(events, "$category", [["Uncategorized"]]);
        duration = sum_durations(events);
        RETURN = {{"events": events, "duration": duration}};
        """,
        timeperiods,
    )
    events = []
    skipped = 0
    for raw_event in res[0]["events"]:
        title = raw_event.get("data", {}).get(title_field)
        if not isinstance(title, str):
            skipped += 1
            continue
        raw_event = {**raw_event, "data": {**raw_event["data"], "title": title}}
        events.append(Event(**raw_event))
    if skipped:
        print(f"Skipped {skipped} events without a title from {source_id!r}")
    print(f"Fetched {len(events)} events")
    return events


def events2words(events):
    for e in events:
        for v in e.data.values():
            if isinstance(v, str):
                for word in v.split():
                    if len(word) >= 3:
                        # normalize
                        word = word.lower()
                        yield (word, e.duration)


def main():
    categories = example_categories()
    events = get_events()

    # find most common words, by duration
    corpus: Dict[str, timedelta] = Counter()  # type: ignore
    for word, duration in events2words(events):
        if word not in corpus:
            corpus[word] = timedelta(0)
        corpus[word] += duration

    # The top words are rarely useful for categorization, as they are usually browsers and other categories
    # of activity which are too broad for it to make sense as a rule (except as a fallback).
    print(tabulate(corpus.most_common(50), headers=["word", "duration"]))  # type: ignore


if __name__ == "__main__":
    main()
