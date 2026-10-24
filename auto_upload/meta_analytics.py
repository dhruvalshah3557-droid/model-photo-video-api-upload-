#!/usr/bin/env python3
"""Collect read-only Facebook and Instagram analytics into Google Sheets daily."""

import os
from datetime import datetime, timedelta, timezone

import gspread
import requests

from config import Config


GRAPH_URL = os.getenv("META_GRAPH_URL", "https://graph.facebook.com/v26.0").rstrip("/")
ACCOUNT_SHEET = "Meta Account Analytics"
CONTENT_SHEET = "Meta Content Analytics"
ERROR_SHEET = "Meta Analytics Errors"

ACCOUNT_HEADERS = [
    "snapshot_date", "collected_at_utc", "account_id", "platform", "account_name",
    "platform_account_id", "username", "followers", "media_count", "views",
    "reach", "engagements", "video_views", "total_interactions", "profile_links_taps",
]
CONTENT_HEADERS = [
    "snapshot_date", "collected_at_utc", "account_id", "platform", "account_name",
    "content_id", "content_type", "published_at", "permalink", "caption", "likes",
    "comments", "shares", "saved", "views", "reach", "total_interactions",
]
ERROR_HEADERS = [
    "snapshot_date", "collected_at_utc", "account_id", "platform", "account_name",
    "platform_account_id", "stage", "error_code", "error_message",
]


class GraphError(RuntimeError):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = str(code or "")


def graph_get(object_id, edge="", token="", **params):
    path = f"{object_id}/{edge}" if edge else str(object_id)
    response = requests.get(
        f"{GRAPH_URL}/{path}",
        params={**params, "access_token": token},
        timeout=30,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise GraphError(f"Meta returned HTTP {response.status_code} with non-JSON data") from exc
    error = data.get("error") or {}
    if error:
        raise GraphError(error.get("message", "Unknown Meta API error"), error.get("code", ""))
    response.raise_for_status()
    return data


def _metric_value(item):
    total = item.get("total_value")
    if isinstance(total, dict):
        return total.get("value", "")
    values = item.get("values") or []
    if values:
        return values[-1].get("value", "")
    return ""


def insight_metrics(object_id, token, metrics, since, until, metric_type="total_value"):
    """Fetch metrics one-by-one so one deprecated metric cannot hide the rest."""
    values, errors = {}, []
    for metric in metrics:
        params = {"metric": metric, "period": "day", "since": since, "until": until}
        if metric_type:
            params["metric_type"] = metric_type
        try:
            data = graph_get(object_id, "insights", token, **params)
            item = (data.get("data") or [{}])[0]
            values[metric] = _metric_value(item)
        except GraphError as exc:
            errors.append((metric, exc.code, str(exc)))
    return values, errors


def _token_for(account):
    key = str(account.get("credential_property_key") or "").strip()
    return (os.getenv(key) if key else "") or Config.FB_ACCESS_TOKEN or ""


def _enabled_meta_accounts(records):
    for row in records:
        platform = str(row.get("platform") or "").strip().lower()
        if platform not in {"facebook", "instagram"}:
            continue
        if str(row.get("enabled") or "").strip().lower() not in {"yes", "true", "1"}:
            continue
        if not str(row.get("platform_account_id") or "").strip():
            continue
        yield row


def _sheet(book, title, headers):
    try:
        ws = book.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=title, rows=2000, cols=len(headers))
    first = ws.row_values(1)
    if first != headers:
        ws.update(range_name="A1", values=[headers])
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}})
    return ws


def _existing_keys(ws, key_columns):
    values = ws.get_all_values()
    if len(values) < 2:
        return set()
    headers = values[0]
    indexes = [headers.index(name) for name in key_columns]
    return {tuple(row[i] if i < len(row) else "" for i in indexes) for row in values[1:]}


def _append_unique(ws, headers, rows, key_columns):
    existing = _existing_keys(ws, key_columns)
    indexes = [headers.index(name) for name in key_columns]
    new_rows = []
    for row in rows:
        key = tuple(str(row[i]) for i in indexes)
        if key not in existing:
            new_rows.append(row)
            existing.add(key)
    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED", table_range="A1")
    return len(new_rows)


def _error_row(day, collected, account, stage, code, message):
    return [
        day, collected, account.get("account_id", ""), account.get("platform", ""),
        account.get("account_name", ""), account.get("platform_account_id", ""),
        stage, code, str(message)[:500],
    ]


def collect_facebook(account, token, day, collected, since, until):
    page_id = str(account["platform_account_id"]).strip()
    profile = graph_get(page_id, token=token, fields="id,name,username,followers_count,fan_count")
    metrics, metric_errors = insight_metrics(
        page_id, token,
        ["page_media_view", "page_post_engagements", "page_video_views", "page_follows"],
        since, until, metric_type="",
    )
    account_row = [
        day, collected, account.get("account_id", ""), "Facebook", account.get("account_name", ""),
        page_id, profile.get("username", account.get("username_or_channel", "")),
        profile.get("followers_count", profile.get("fan_count", "")), "",
        metrics.get("page_media_view", ""), "", metrics.get("page_post_engagements", ""),
        metrics.get("page_video_views", ""), "", "",
    ]
    errors = [_error_row(day, collected, account, f"metric:{m}", c, e) for m, c, e in metric_errors]
    content_rows = []
    posts = graph_get(
        page_id, "posts", token,
        fields="id,message,created_time,permalink_url,shares,comments.limit(0).summary(true),reactions.limit(0).summary(true)",
        limit=50, since=since,
    ).get("data", [])
    for post in posts:
        reactions = ((post.get("reactions") or {}).get("summary") or {}).get("total_count", 0)
        comments = ((post.get("comments") or {}).get("summary") or {}).get("total_count", 0)
        shares = (post.get("shares") or {}).get("count", 0)
        post_metrics, post_errors = insight_metrics(
            post.get("id", ""), token,
            ["post_media_view", "post_clicks", "post_video_views"],
            since, until, metric_type="",
        )
        errors.extend(_error_row(day, collected, account, f"content:{post.get('id')}:{m}", c, e) for m, c, e in post_errors)
        content_rows.append([
            day, collected, account.get("account_id", ""), "Facebook", account.get("account_name", ""),
            post.get("id", ""), "post", post.get("created_time", ""), post.get("permalink_url", ""),
            str(post.get("message", ""))[:500], reactions, comments, shares, "",
            post_metrics.get("post_media_view", post_metrics.get("post_video_views", "")), "",
            reactions + comments + shares,
        ])
    return account_row, content_rows, errors


def collect_instagram(account, token, day, collected, since, until):
    ig_id = str(account["platform_account_id"]).strip()
    profile = graph_get(ig_id, token=token, fields="id,username,name,followers_count,media_count")
    metrics, metric_errors = insight_metrics(
        ig_id, token,
        ["views", "reach", "accounts_engaged", "total_interactions", "profile_links_taps"],
        since, until,
    )
    account_row = [
        day, collected, account.get("account_id", ""), "Instagram", account.get("account_name", ""),
        ig_id, profile.get("username", account.get("username_or_channel", "")),
        profile.get("followers_count", ""), profile.get("media_count", ""), metrics.get("views", ""),
        metrics.get("reach", ""), metrics.get("accounts_engaged", ""), "",
        metrics.get("total_interactions", ""), metrics.get("profile_links_taps", ""),
    ]
    errors = [_error_row(day, collected, account, f"metric:{m}", c, e) for m, c, e in metric_errors]
    media = graph_get(
        ig_id, "media", token,
        fields="id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count",
        limit=50, since=since,
    ).get("data", [])
    content_rows = []
    for item in media:
        item_metrics, item_errors = insight_metrics(
            item.get("id", ""), token,
            ["views", "reach", "total_interactions", "shares", "saved"],
            since, until, metric_type="",
        )
        errors.extend(_error_row(day, collected, account, f"content:{item.get('id')}:{m}", c, e) for m, c, e in item_errors)
        content_rows.append([
            day, collected, account.get("account_id", ""), "Instagram", account.get("account_name", ""),
            item.get("id", ""), item.get("media_product_type") or item.get("media_type", ""),
            item.get("timestamp", ""), item.get("permalink", ""), str(item.get("caption", ""))[:500],
            item.get("like_count", 0), item.get("comments_count", 0), item_metrics.get("shares", ""),
            item_metrics.get("saved", ""), item_metrics.get("views", ""), item_metrics.get("reach", ""),
            item_metrics.get("total_interactions", ""),
        ])
    return account_row, content_rows, errors


def main():
    credentials = gspread.service_account(filename=Config.GOOGLE_SHEET_CREDENTIALS)
    book = credentials.open_by_url(Config.GOOGLE_SHEET_URL)
    accounts = book.worksheet(Config.ACCOUNTS_SHEET).get_all_records()
    now = datetime.now(timezone.utc)
    day = (now - timedelta(days=1)).date().isoformat()
    collected = now.replace(microsecond=0).isoformat()
    since = int((now - timedelta(days=2)).timestamp())
    until = int(now.timestamp())

    account_rows, content_rows, error_rows = [], [], []
    for account in _enabled_meta_accounts(accounts):
        token = _token_for(account)
        if not token:
            error_rows.append(_error_row(day, collected, account, "token", "", "No Meta access token configured"))
            continue
        try:
            if str(account.get("platform", "")).lower() == "facebook":
                row, content, errors = collect_facebook(account, token, day, collected, since, until)
            else:
                row, content, errors = collect_instagram(account, token, day, collected, since, until)
            account_rows.append(row)
            content_rows.extend(content)
            error_rows.extend(errors)
        except (GraphError, requests.RequestException) as exc:
            error_rows.append(_error_row(day, collected, account, "account", getattr(exc, "code", ""), exc))

    account_ws = _sheet(book, ACCOUNT_SHEET, ACCOUNT_HEADERS)
    content_ws = _sheet(book, CONTENT_SHEET, CONTENT_HEADERS)
    error_ws = _sheet(book, ERROR_SHEET, ERROR_HEADERS)
    added_accounts = _append_unique(account_ws, ACCOUNT_HEADERS, account_rows, ["snapshot_date", "account_id"])
    added_content = _append_unique(content_ws, CONTENT_HEADERS, content_rows, ["snapshot_date", "account_id", "content_id"])
    added_errors = _append_unique(error_ws, ERROR_HEADERS, error_rows, ["snapshot_date", "account_id", "stage", "error_message"])
    print(f"Analytics collected: accounts={added_accounts}, content={added_content}, errors={added_errors}")
    print(f"Coverage: configured={len(list(_enabled_meta_accounts(accounts)))}, successful={len(account_rows)}")
    if not account_rows:
        raise SystemExit("No Facebook or Instagram account analytics could be collected; check Meta permissions in the error sheet")


if __name__ == "__main__":
    main()
