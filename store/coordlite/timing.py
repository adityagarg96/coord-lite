"""Planning time, never a scheduler. Date-only values use the planning timezone;
due and end dates include the whole final day. All stored timestamps are UTC."""
import os
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

TIMEZONE = os.environ.get("COORD_TIMEZONE", "America/Los_Angeles")
HORIZONS = {"year", "quarter", "month", "week", "sprint"}
TIME_FIELDS = ("due_at", "starts_at", "ends_at", "anchor_kind", "moveable")


def instant(value, *, end=False):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    elif len(value) == 10:
        dt = datetime.combine(date.fromisoformat(value), time(23, 59, 59) if end else time.min)
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(TIMEZONE))
    return dt.astimezone(timezone.utc)


def normalize(value, *, end=False):
    return instant(value, end=end).isoformat().replace("+00:00", "Z") if value else None


def validate(current, patch):
    result = dict(patch)
    for key in ("due_at", "starts_at", "ends_at"):
        if key in result:
            if result[key] is not None and (not isinstance(result[key], str) or not result[key].strip()):
                raise ValueError(f"{key} must be an ISO date or timestamp, or null")
            result[key] = normalize(result[key], end=key in ("due_at", "ends_at"))
    merged = {**current, **result}
    if merged.get("anchor_kind") not in (None, "live", "artificial"):
        raise ValueError("anchor_kind must be live, artificial, or null")
    if merged.get("moveable") not in (None, True, False, 0, 1):
        raise ValueError("moveable must be a boolean or null")
    if merged.get("anchor_kind") != "live":
        if patch.get("moveable") is not None:
            raise ValueError("moveable only applies to live anchors")
        if "anchor_kind" in patch:
            result["moveable"] = None
    start, end = instant(merged.get("starts_at")), instant(merged.get("ends_at"))
    if start and end and start > end:
        raise ValueError("starts_at must not be after ends_at")
    return result


def derived(node, now=None):
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(TIMEZONE)
    today = now.astimezone(zone).date()
    start, end, due = (instant(node.get(k)) for k in ("starts_at", "ends_at", "due_at"))
    out = {}
    if start and end:
        first, last = start.astimezone(zone).date(), end.astimezone(zone).date()
        out.update(window_days=(last - first).days + 1, days_to_end=(last - today).days)
        if start <= now <= end:
            out["day_in_window"] = (today - first).days + 1
    if due:
        out.update(days_to_due=(due.astimezone(zone).date() - today).days, overdue=now > due)
    return out
