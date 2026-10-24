"""Shared 24-hour delivery floor for Facebook, Instagram and YouTube.

Every enabled publish-ready primary account must reach MINIMUM_POSTS_24H
successful posts in each rolling 24-hour UTC window. Posts for an account
are spaced at least MINIMUM_GAP_HOURS apart so API and upload quota are not
burst in a single run. LINE is excluded from capacity while its monthly
Messaging API quota is exhausted.
"""
from datetime import datetime, timedelta, timezone
import re

PRIMARY_PLATFORMS = ("instagram", "facebook", "youtube", "tiktok")
MINIMUM_POSTS_24H = 8
MINIMUM_GAP_HOURS = 0.5
LINE_QUOTA_EXHAUSTED = True
_GVIZ_DATE_RE = re.compile(
    r"Date\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)"
    r"(?:\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+))?\s*\)"
)


def parse_queue_time(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    match = _GVIZ_DATE_RE.search(raw)
    if match:
        year, month, day, hour, minute, second = match.groups()
        try:
            return datetime(
                int(year),
                int(month) + 1,
                int(day),
                int(hour or 0),
                int(minute or 0),
                int(second or 0),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _accounts_items(accounts):
    if isinstance(accounts, dict):
        return accounts.items()
    items = []
    for account in accounts or []:
        account_id = str((account or {}).get("account_id", "") or "").strip()
        if account_id:
            items.append((account_id, account))
    return items


def slot_eligible(account):
    """True when an enabled primary destination can consume a production slot.

    Instagram placeholders with a blank platform ID stay enabled and are probed
    later, but they must not reserve capacity or count toward the 24-hour floor.
    LINE never receives a slot while its monthly broadcast quota is exhausted.
    """
    if not account or not account.get("enabled"):
        return False
    platform = str(account.get("platform", "") or "").strip().lower()
    if platform not in PRIMARY_PLATFORMS:
        return False
    if platform in ("instagram", "tiktok") and not str(account.get("platform_account_id") or "").strip():
        return False
    return True


def ready_platform_counts(accounts):
    counts = {"facebook": 0, "instagram": 0, "youtube": 0, "tiktok": 0}
    for _account_id, account in _accounts_items(accounts):
        platform = str((account or {}).get("platform", "") or "").strip().lower()
        if platform in counts and slot_eligible(account):
            counts[platform] += 1
    return counts


def rolling_activity(records, accounts, now=None):
    """Count confirmed uploads in the rolling 24-hour window per ready account."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    activity = {}
    for account_id, account in _accounts_items(accounts):
        if slot_eligible(account):
            activity[account_id] = {"count": 0, "last": None, "success_times": []}
    for rec in records or []:
        status = str(rec.get("status", "") or "").strip().lower()
        if status != "uploaded":
            continue
        account_id = str(rec.get("account_id", "") or "").strip()
        if account_id not in activity:
            continue
        uploaded_at = parse_queue_time(rec.get("last_attempt_at"))
        if not uploaded_at or uploaded_at < cutoff or uploaded_at > now:
            continue
        activity[account_id]["count"] += 1
        activity[account_id]["success_times"].append(uploaded_at)
        last = activity[account_id]["last"]
        if last is None or uploaded_at > last:
            activity[account_id]["last"] = uploaded_at
    return activity


def minimum_delivery_priority(account_id, activity, now=None):
    """Prioritize lower delivery counts once the account's safety gap has elapsed."""
    now = now or datetime.now(timezone.utc)
    state = (activity or {}).get(account_id, {})
    count = int(state.get("count", 0) or 0)
    if count >= MINIMUM_POSTS_24H:
        return (1, count)
    last = state.get("last")
    if last and now - last < timedelta(hours=MINIMUM_GAP_HOURS):
        return (1, count)
    return (0, count)


def due_deficit_accounts(accounts, activity, now=None):
    """Publish-ready primary accounts below the 24h floor whose gap has elapsed."""
    now = now or datetime.now(timezone.utc)
    due = []
    for account_id, account in _accounts_items(accounts):
        if not slot_eligible(account):
            continue
        state = (activity or {}).get(account_id) or {}
        count = int(state.get("count", 0) or 0)
        if count >= MINIMUM_POSTS_24H:
            continue
        last = state.get("last")
        if last and now - last < timedelta(hours=MINIMUM_GAP_HOURS):
            continue
        due.append({
            "account_id": account_id,
            "platform": str(account.get("platform", "") or "").strip().lower(),
            "count": count,
            "deficit": MINIMUM_POSTS_24H - count,
            "last": last,
        })
    due.sort(key=lambda item: (-item["deficit"], item["account_id"]))
    return due
