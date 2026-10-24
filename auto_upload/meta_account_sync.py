#!/usr/bin/env python3
"""Validate the Meta token and safely discover newly connected accounts."""
import json
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sheets_reader import SheetsReader


GRAPH_VERSION = "v26.0"
REPORT_PATH = Path(os.getenv("META_SYNC_REPORT", "/tmp/meta_sync_report.json"))
MARKETS = {
    "vietnam": ("VNM", "vi-VN", "Asia/Ho_Chi_Minh"),
    "spain": ("SPAIN", "es-ES", "Europe/Madrid"),
    "italy": ("ITALY", "it-IT", "Europe/Rome"),
    "japan": ("JPN", "ja-JP", "Asia/Tokyo"),
    "korea": ("KOR", "ko-KR", "Asia/Seoul"),
    "russia": ("RUS", "ru-RU", "Europe/Moscow"),
    "israel": ("ISR", "he-IL", "Asia/Jerusalem"),
    "myanmar": ("MMR", "my-MM", "Asia/Yangon"),
    "philippines": ("PH", "fil-PH", "Asia/Manila"),
    "indonesia": ("INDO", "id-ID", "Asia/Jakarta"),
    "dubai": ("DUBAI", "ar-AE", "Asia/Dubai"),
    "kuwait": ("KUWAIT", "ar-KW", "Asia/Kuwait"),
    "pakistan": ("PAK", "en-GB", "Asia/Karachi"),
    "singapore": ("SINGAPORE", "en-SG", "Asia/Singapore"),
    "hong kong": ("HK", "zh-HK", "Asia/Hong_Kong"),
    "colour diam hk": ("HK", "zh-HK", "Asia/Hong_Kong"),
    "colourdiamhk": ("HK", "zh-HK", "Asia/Hong_Kong"),
    "bangkok": ("BKK", "th-TH", "Asia/Bangkok"),
    "sweden": ("SWEDEN", "sv-SE", "Europe/Stockholm"),
    "germany": ("GERMANY", "de-DE", "Europe/Berlin"),
    "poland": ("POLAND", "pl-PL", "Europe/Warsaw"),
    "denmark": ("DENMARK", "da-DK", "Europe/Copenhagen"),
    "france": ("FRANCE", "fr-FR", "Europe/Paris"),
    "turkey": ("TURKEY", "tr-TR", "Europe/Istanbul"),
    "china": ("CHINA", "zh-CN", "Asia/Shanghai"),
    "greece": ("GREECE", "el-GR", "Europe/Athens"),
    "lebanon": ("LEBANON", "lb-LB", "Asia/Beirut"),
    "lebanese": ("LEBANON", "lb-LB", "Asia/Beirut"),
    "lebenesse": ("LEBANON", "lb-LB", "Asia/Beirut"),
    "czech": ("CZECH", "cs-CZ", "Europe/Prague"),
    "czechia": ("CZECH", "cs-CZ", "Europe/Prague"),
    "vestslavic": ("CZECH", "cs-CZ", "Europe/Prague"),
}


def fetch_pages(token):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/me/accounts"
    params = {
        "fields": "id,name,instagram_business_account{id,username}",
        "limit": 100,
        "access_token": token,
    }
    pages = []
    while url:
        response = requests.get(url, params=params, timeout=30)
        data = response.json()
        if response.status_code >= 400 or data.get("error"):
            error = data.get("error") or {}
            raise RuntimeError(
                "Meta token preflight failed: "
                f"code={error.get('code', response.status_code)} "
                f"subcode={error.get('error_subcode', '')} "
                f"message={error.get('message', 'unknown error')}"
            )
        pages.extend(data.get("data") or [])
        url = (data.get("paging") or {}).get("next")
        params = None
    return pages


def market_settings(name):
    lowered = str(name or "").lower()
    for marker, settings in MARKETS.items():
        if marker in lowered:
            return settings
    return "AUTO", "en-GB", "Asia/Bangkok"


def unique_account_id(platform, name, platform_id, used_ids):
    code, _, _ = market_settings(name)
    candidate = f"{platform}-{code}"
    if candidate in used_ids or code == "AUTO":
        candidate = f"{platform}-AUTO-{str(platform_id)[-6:]}"
    suffix = 2
    base = candidate
    while candidate in used_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def row_for(headers, account_id, platform, name, platform_id, username, linked_id):
    _, language, timezone = market_settings(name)
    is_fb = platform == "Facebook"
    values = {
        "account_id": account_id,
        "platform": platform,
        "account_name": name,
        "platform_account_id": str(platform_id),
        "username_or_channel": username,
        "primary_language": language,
        "fallback_language": "en-GB",
        "timezone": timezone,
        "enabled": "No",
        "allowed_formats": (
            "Image Post, Carousel, Facebook Reel" if is_fb
            else "Image Post, Carousel, Instagram Reel"
        ),
        "caption_style": "Premium luxury; concise; verify local language",
        "hashtag_set": "Local luxury jewellery + product-specific tags",
        "cta_rule": "DM or website CTA",
        "posting_window": "10:00-21:00 local",
        "min_gap_minutes": 180,
        "product_tagging": "No",
        "catalog_or_store_id": "",
        "credential_property_key": f"META_TOKEN_{account_id.replace('-', '_')}",
        "approval_required": "Yes",
        "notes": (
            "AUTO-DISCOVERED via live Meta API; verify language/timezone, then enable. "
            f"Linked account ID: {linked_id or 'none'}"
        ),
    }
    return [values.get(header, "") for header in headers]


def _identity_key(value):
    key = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    return key.replace("colordiam", "colourdiam")


def _matching_blank_instagram(existing, page_name, ig_username):
    blank = [
        account for account in existing
        if account.get("platform") == "instagram"
        and not str(account.get("platform_account_id", "") or "").strip()
    ]
    code, _, _ = market_settings(f"{page_name} {ig_username}")
    if code != "AUTO":
        direct = [
            account for account in blank
            if account.get("account_id") == f"IG-{code}"
        ]
        if len(direct) == 1:
            return direct[0]

    wanted = {
        _identity_key(page_name),
        _identity_key(ig_username),
    } - {""}
    matches = []
    for account in blank:
        identities = {
            _identity_key(account.get("account_name", "")),
            _identity_key(account.get("username_or_channel", "")),
        } - {""}
        if wanted & identities:
            matches.append(account)
    return matches[0] if len(matches) == 1 else None


def _matching_blank_facebook(existing, page_name):
    """Return one preconfigured Facebook row awaiting Meta's Page ID."""
    blank = [
        account for account in existing
        if account.get("platform") == "facebook"
        and not str(account.get("platform_account_id", "") or "").strip()
    ]
    code, _, _ = market_settings(page_name)
    if code != "AUTO":
        direct = [
            account for account in blank
            if account.get("account_id") == f"FB-{code}"
        ]
        if len(direct) == 1:
            return direct[0]

    wanted = _identity_key(page_name)
    matches = [
        account for account in blank
        if wanted and wanted == _identity_key(account.get("account_name", ""))
    ]
    return matches[0] if len(matches) == 1 else None


def sync_accounts(pages, sheets):
    existing = sheets.get_accounts()
    existing_keys = {
        (a.get("platform", "").lower(), str(a.get("platform_account_id", "")))
        for a in existing if a.get("platform_account_id")
    }
    used_ids = {a.get("account_id") for a in existing}
    headers = sheets.accounts_ws.row_values(sheets.accounts_header_row)
    header_positions = {
        str(header).strip().lower(): index + 1
        for index, header in enumerate(headers)
    }
    new_rows = []
    account_updates = []
    discovered = []

    def queue_cell(row, header, value):
        col = header_positions.get(header)
        if not col:
            return
        account_updates.append({
            "range": f"{sheets._col_letter(col)}{row}",
            "values": [[value]],
        })

    for page in pages:
        page_id = str(page.get("id", ""))
        page_name = str(page.get("name", "")).strip()
        ig = page.get("instagram_business_account") or {}
        ig_id = str(ig.get("id", ""))
        ig_username = str(ig.get("username", ""))

        if page_id and ("facebook", page_id) not in existing_keys:
            placeholder = _matching_blank_facebook(existing, page_name)
            if placeholder:
                queue_cell(placeholder["row"], "platform_account_id", page_id)
                queue_cell(
                    placeholder["row"],
                    "notes",
                    "Verified via live Meta API; "
                    f"linked Instagram Business ID: {ig_id or 'none'}.",
                )
                placeholder["platform_account_id"] = page_id
                discovered.append({
                    "account_id": placeholder["account_id"],
                    "platform": "Facebook",
                    "name": page_name,
                    "action": "updated_platform_id",
                })
            else:
                aid = unique_account_id("FB", page_name, page_id, used_ids)
                new_rows.append(row_for(headers, aid, "Facebook", page_name, page_id,
                                        ig_username, ig_id))
                discovered.append({
                    "account_id": aid, "platform": "Facebook",
                    "name": page_name, "action": "added",
                })
            existing_keys.add(("facebook", page_id))

        if ig_id and ("instagram", ig_id) not in existing_keys:
            placeholder = _matching_blank_instagram(existing, page_name, ig_username)
            if placeholder:
                queue_cell(placeholder["row"], "platform_account_id", ig_id)
                queue_cell(placeholder["row"], "username_or_channel", ig_username)
                queue_cell(
                    placeholder["row"],
                    "notes",
                    f"Verified via live Meta API; linked to Facebook Page {page_id}.",
                )
                placeholder["platform_account_id"] = ig_id
                placeholder["username_or_channel"] = ig_username
                discovered.append({
                    "account_id": placeholder["account_id"],
                    "platform": "Instagram",
                    "name": page_name,
                    "action": "updated_platform_id",
                })
            else:
                aid = unique_account_id("IG", page_name, ig_id, used_ids)
                new_rows.append(row_for(headers, aid, "Instagram", page_name, ig_id,
                                        ig_username, page_id))
                discovered.append({
                    "account_id": aid, "platform": "Instagram",
                    "name": page_name, "action": "added",
                })
            existing_keys.add(("instagram", ig_id))

    if account_updates:
        sheets.accounts_ws.batch_update(
            account_updates,
            value_input_option="USER_ENTERED",
        )
    if new_rows:
        sheets.accounts_ws.append_rows(new_rows, value_input_option="RAW")
    return discovered


def main():
    token = os.getenv("FB_ACCESS_TOKEN", "").strip()
    report = {"token_valid": False, "page_count": 0, "new_accounts": [], "error": ""}
    try:
        if not token:
            raise RuntimeError("Meta token preflight failed: FB_ACCESS_TOKEN is empty")
        pages = fetch_pages(token)
        report["token_valid"] = True
        report["page_count"] = len(pages)
        report["new_accounts"] = sync_accounts(pages, SheetsReader())
        print(
            f"Meta preflight OK: {len(pages)} page(s); "
            f"{len(report['new_accounts'])} account change(s) discovered"
        )
    except Exception as exc:
        report["error"] = str(exc)
        print(f"::error title=Meta preflight failed::{exc}")
        return_code = 1
    else:
        return_code = 0
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
