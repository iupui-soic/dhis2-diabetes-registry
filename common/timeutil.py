"""Time handling.

Everything in this pipeline is UTC, and every event should say so.

The AI-READI wearable and CGM files carry a Z suffix, and a diurnal check
confirms those labels are true UTC rather than mislabelled local time: sleep
segments cluster at 07:00-12:00 UTC for UW participants and 05:00-10:00 for
UAB, which is 00:00-05:00 local at both sites. The environment CSVs carry no
suffix at all but are UTC too, confirmed the same way: for UW the daily indoor
temperature maximum falls at 23:00-03:00 in the file's own labels, which is
16:00-20:00 local Pacific.

The audit found hour matching done by comparing the first 19 characters of two
formatted strings. That works only while every side happens to render UTC, and
fails silently and completely otherwise. Compare instants instead.
"""

from datetime import datetime, timedelta, timezone


def parse_instant(value):
    """Parse any timestamp this project handles into an aware UTC datetime.

    Accepts a datetime, an ISO string with Z or an offset, and the
    'YYYY-MM-DD HH:MM:SS' form used by the environment sensor CSVs. A value
    with no offset is treated as UTC, which is what those files actually are.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        if "T" not in text and " " in text:
            text = text.replace(" ", "T", 1)
        if "." in text:
            head, _, tail = text.partition(".")
            frac = tail[:6]
            rest = ""
            for i, ch in enumerate(tail):
                if not ch.isdigit():
                    rest = tail[i:]
                    break
            frac = "".join(c for c in frac if c.isdigit())
            text = f"{head}.{frac}{rest}" if frac else head + rest
        dt = datetime.fromisoformat(text)

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hour_bucket(value):
    """Truncate a timestamp to its UTC hour. Returns an aware datetime."""
    return parse_instant(value).replace(minute=0, second=0, microsecond=0)


def hour_key(value):
    """A hashable, timezone-correct key for one UTC hour.

    Use this on both sides of any source-to-DHIS2 comparison. Two timestamps
    that denote the same instant produce the same key regardless of how each
    side was formatted.
    """
    dt = hour_bucket(value)
    return (dt.year, dt.month, dt.day, dt.hour)


def to_iso(dt):
    """Serialise an instant with an explicit offset, for occurredAt."""
    return parse_instant(dt).isoformat()


def hour_end(value):
    """The end of the UTC hour that contains value."""
    return hour_bucket(value) + timedelta(hours=1)


def format_display(value):
    """Human-readable UTC stamp for the Hour Start / Hour End text fields."""
    return parse_instant(value).strftime("%Y-%m-%d %H:%M UTC")


def time_only(value):
    """UTC clock time, for the flagged-reading timestamp lists."""
    return parse_instant(value).strftime("%H:%M:%S")
