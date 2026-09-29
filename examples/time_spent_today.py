"""Print today's total activity using the server's configured activity profile."""

from datetime import datetime, time, timedelta, timezone
import socket

import aw_client

if __name__ == "__main__":
    daystart = datetime.combine(datetime.now().date(), time()).astimezone(timezone.utc)
    dayend = daystart + timedelta(days=1)

    awc = aw_client.ActivityWatchClient("time-spent-today")
    profile = awc.build_profile_query_v2(hostname=socket.gethostname())
    query = f"""
    {profile.query()}
    RETURN = sum_durations(events);
    """
    duration_seconds = awc.query(query, [(daystart, dayend)])[0]
    print(f"Total configured activity today: {timedelta(seconds=duration_seconds)}")
