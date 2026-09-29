#!/usr/bin/env python3

import json
import logging
import textwrap
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

import click
from aw_core import Event
from tabulate import tabulate

import aw_client

from . import queries

now = datetime.now(timezone.utc)
td1day = timedelta(days=1)
td1yr = timedelta(days=365)

logger = logging.getLogger(__name__)

class _Context:
    client: aw_client.ActivityWatchClient


@click.group(
    help="CLI utility for aw-client to aid in interacting with the ActivityWatch server"
)
@click.option(
    "--host",
    default="127.0.0.1",
    help="Address of host",
)
@click.option(
    "--port",
    default=5600,
    help="Port to use",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Verbosity",
)
@click.option("--testing", is_flag=True, help="Set to use testing ports by default")
@click.pass_context
def main(ctx, testing: bool, verbose: bool, host: str, port: int):
    ctx.obj = _Context()
    ctx.obj.client = aw_client.ActivityWatchClient(
        host=host,
        port=port if port != 5600 else (5666 if testing else 5600),
        testing=testing,
    )
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)


@main.command(help="Send a heartbeat to bucket with ID `bucket_id` with JSON `data`")
@click.argument("bucket_id")
@click.argument("data")
@click.option("--pulsetime", default=60, help="pulsetime to use for merging heartbeats")
@click.pass_obj
def heartbeat(obj: _Context, bucket_id: str, data: str, pulsetime: int):
    now = datetime.now(timezone.utc)
    e = Event(duration=0, data=json.loads(data), timestamp=now)
    print(e)
    obj.client.heartbeat(bucket_id, e, pulsetime)


@main.command(help="List all buckets")
@click.pass_obj
def buckets(obj: _Context):
    buckets = obj.client.get_buckets()
    print("Buckets:")
    for bucket in buckets:
        print(f" - {bucket}")


@main.command(help="Query events from bucket with ID `bucket_id`")
@click.argument("bucket_id")
@click.pass_obj
def events(obj: _Context, bucket_id: str):
    events = obj.client.get_events(bucket_id)
    print("events:")
    for e in events:
        print(
            " - {} ({}) {}".format(
                e.timestamp.replace(tzinfo=None, microsecond=0),
                str(e.duration).split(".")[0],
                e.data,
            )
        )


@main.command(help="Run a query in file at `path` on the server")
@click.argument("path")
@click.option("--name")
@click.option("--cache", is_flag=True)
@click.option("--json", "_json", is_flag=True)
@click.option("--start", default=now - td1day, type=click.DateTime())
@click.option("--stop", default=now + td1yr, type=click.DateTime())
@click.option(
    "--timezone",
    help="Time zone for start and stop options."
    " Must be a valid IANA identifier like e.g. 'Europe/Warsaw'.",
)
@click.pass_obj
def query(
    obj: _Context,
    path: str,
    cache: bool,
    _json: bool,
    start: datetime,
    stop: datetime,
    timezone: str,
    name: Optional[str] = None,
):
    with open(path) as f:
        query = f.read()

    if timezone:
        zone_info = ZoneInfo(timezone)
        start = start.replace(tzinfo=zone_info)
        stop = stop.replace(tzinfo=zone_info)

    result = obj.client.query(query, [(start, stop)], cache=cache, name=name)
    if _json:
        print(json.dumps(result))
    else:
        for period in result:
            print(f"Showing 10 out of {len(period)} events:")
            for event in period[:10]:
                event.pop("id")
                event.pop("timestamp")
                print(
                    " - Duration: {} \tData: {}".format(
                        str(timedelta(seconds=event["duration"])).split(".")[0],
                        event["data"],
                    )
                )
            print(
                "Total duration:\t",
                timedelta(seconds=sum(e["duration"] for e in period)),
            )


def _report_query(
    canonical_query: str,
    app_field: Optional[str],
    title_field: Optional[str],
) -> str:
    presentation = (
        f'''title_events = sort_by_duration(merge_events_by_keys(
            events, ["{app_field}", "{title_field}"]));
    app_events = sort_by_duration(merge_events_by_keys(
            title_events, ["{app_field}"]));'''
        if app_field and title_field
        else "title_events = [];\n    app_events = [];"
    )
    return f"""
    {canonical_query}
    {presentation}
    cat_events = sort_by_duration(merge_events_by_keys(events, ["$category"]));
    app_events = limit_events(app_events, {queries.default_limit});
    title_events = limit_events(title_events, {queries.default_limit});
    duration = sum_durations(events);
    RETURN = {{
            "events": events,
            "window": {{
                "app_events": app_events,
                "title_events": title_events,
                "cat_events": cat_events,
                "active_events": not_afk,
                "duration": duration
            }},
            "browser": {{
                "domains": [],
                "urls": [],
                "duration": 0
            }}
    }};
    """


@main.command(help="Generate an activity report")
@click.argument("hostname")
@click.option("--cache", is_flag=True)
@click.option("--start", default=now - td1day, type=click.DateTime())
@click.option("--stop", default=now + td1yr, type=click.DateTime())
@click.option("--limit", default=10)
@click.pass_obj
def report(
    obj: _Context,
    hostname: str,
    cache: bool,
    start: datetime,
    stop: datetime,
    name: Optional[str] = None,
    limit: int = 10,
):
    logger.info(f"Querying between {start} and {stop}")

    if not start.tzinfo:
        start = start.astimezone()
    if not stop.tzinfo:
        stop = stop.astimezone()

    materialized = obj.client.build_profile_query_v2(hostname=hostname)
    source_id = materialized.app_title_source_id
    app_field = f"$source.{source_id}.app" if source_id else None
    title_field = f"$source.{source_id}.title" if source_id else None
    query = _report_query(materialized.query(), app_field, title_field)
    logger.debug("Query: \n" + queries.pretty_query(query))

    result = obj.client.query(query, [(start, stop)], cache=cache, name=name)

    # TODO: Print titles, apps, categories, with most time
    for period in result:
        print()
        # print(period["window"]["cat_events"])

        cat_events = _parse_events(period["window"]["cat_events"])
        print_top(
            cat_events,
            lambda e: " > ".join(e.data["$category"]),
            title="Categories",
            n=limit,
        )

        title_events = _parse_events(period["window"]["title_events"])
        if title_field:
            print_top(title_events, lambda e: e.data.get(title_field, ""), title="Titles", n=limit)
        else:
            print("Titles:\tUnavailable (profile has no app/title presentation source)\n")

        print(
            "Total duration:\t",
            timedelta(seconds=period["window"]["duration"]),
        )


def _parse_events(events: List[dict]) -> List[Event]:
    return [Event(**event) for event in events]


def print_top(events: List[Event], key=lambda e: e.data, title="Events", n=10):
    print(f"Top {n} {title}" + (f" (out of {len(events)})" if len(events) > 10 else ""))
    print(
        tabulate(
            [
                (event.duration, key(event))
                for event in sorted(events, key=lambda e: e.duration, reverse=True)[:10]
            ],
            headers=["Duration", "Key"],
        )
    )
    print()


@main.command(help="Query 'canonical events' for a single host (filtered, classified)")
@click.argument("hostname")
@click.option("--cache", is_flag=True)
@click.option("--start", default=now - td1day, type=click.DateTime())
@click.option("--stop", default=now + td1yr, type=click.DateTime())
@click.pass_obj
def canonical(
    obj: _Context,
    hostname: str,
    cache: bool,
    start: datetime,
    stop: datetime,
    name: Optional[str] = None,
):
    logger.info(f"Querying between {start} and {stop}")

    if not start.tzinfo:
        start = start.astimezone()
    if not stop.tzinfo:
        stop = stop.astimezone()

    materialized = obj.client.build_profile_query_v2(hostname=hostname)
    query = f"""{materialized.query()}\n RETURN = events;"""
    logger.debug("Query: \n" + queries.pretty_query(query))

    result = obj.client.query(query, [(start, stop)], cache=cache, name=name)

    # TODO: Print titles, apps, categories, with most time
    for period in result:
        print()
        events = _parse_events(period)
        print(f"Showing last 10 out of {len(events)} events:")

        print(
            tabulate(
                [
                    (
                        str(e.timestamp).split(".")[0],
                        str(e.duration).split(".")[0],
                        f'[{e.data.get(f"$source.{materialized.app_title_source_id}.app", "")}] '
                        f'{textwrap.shorten(e.data.get(f"$source.{materialized.app_title_source_id}.title", ""), 60, placeholder="...")}',
                    )
                    for e in events[-10:]
                ],
                headers=["Timestamp", "Duration", "Data"],
            )
        )

        print()
        print(
            "Total duration:\t",
            timedelta(seconds=sum(e["duration"] for e in period)),
        )


if __name__ == "__main__":
    main()
