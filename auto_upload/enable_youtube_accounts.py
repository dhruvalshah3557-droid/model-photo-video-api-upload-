#!/usr/bin/env python3
"""Enable YT-CD and YT-JIYA on the Accounts sheet without rewriting other rows."""
import sys

from sheets_reader import SheetsReader

ACCOUNTS = (
    {
        "account_id": "YT-CD",
        "updates": {
            "enabled": "Yes",
            "credential_property_key": "YOUTUBE_OAUTH_REFRESH_TOKEN_COLOURDIAMONDSS",
            "approval_required": "No",
            "notes": (
                "YouTube Colour Diam channel enabled. Uses "
                "YOUTUBE_OAUTH_REFRESH_TOKEN_COLOURDIAMONDSS with dedicated "
                "client ID/secret."
            ),
        },
        "new_row": {
            "account_id": "YT-CD",
            "platform": "YouTube",
            "account_name": "Colour Diam YouTube",
            "platform_account_id": "UCTWbcY-YtvAx2QUZKXt230A",
            "username_or_channel": "colourdiamondss",
            "primary_language": "en-GB",
            "fallback_language": "en-GB",
            "timezone": "Asia/Bangkok",
            "enabled": "Yes",
            "allowed_formats": "YouTube Video, YouTube Short",
            "caption_style": "Search-friendly luxury education",
            "hashtag_set": "YouTube keywords; 3-5 hashtags",
            "cta_rule": "Product link in description",
            "posting_window": "12:00-21:00 local",
            "min_gap_minutes": "360",
            "product_tagging": "No",
            "catalog_or_store_id": "",
            "credential_property_key": "YOUTUBE_OAUTH_REFRESH_TOKEN_COLOURDIAMONDSS",
            "approval_required": "No",
            "notes": (
                "YouTube Colour Diam channel enabled. Uses "
                "YOUTUBE_OAUTH_REFRESH_TOKEN_COLOURDIAMONDSS with dedicated "
                "client ID/secret."
            ),
        },
    },
    {
        "account_id": "YT-JIYA",
        "updates": {
            "enabled": "Yes",
            "credential_property_key": "YOUTUBE_OAUTH_REFRESH_TOKEN_JIYA",
            "approval_required": "No",
            "notes": (
                "YouTube jiyasdelight channel enabled. Uses "
                "YOUTUBE_OAUTH_REFRESH_TOKEN_JIYA."
            ),
        },
        "new_row": {
            "account_id": "YT-JIYA",
            "platform": "YouTube",
            "account_name": "jiyasdelight youtube",
            "platform_account_id": "UCDFzWfIRvLu1LXq3mQrkj6Q",
            "username_or_channel": "jiyasdelight",
            "primary_language": "en-GB",
            "fallback_language": "en-GB",
            "timezone": "Asia/Bangkok",
            "enabled": "Yes",
            "allowed_formats": "YouTube Video, YouTube Short",
            "caption_style": "Search-friendly luxury education",
            "hashtag_set": "YouTube keywords; 3-5 hashtags",
            "cta_rule": "Product link in description",
            "posting_window": "12:00-21:00 local",
            "min_gap_minutes": "360",
            "product_tagging": "No",
            "catalog_or_store_id": "",
            "credential_property_key": "YOUTUBE_OAUTH_REFRESH_TOKEN_JIYA",
            "approval_required": "No",
            "notes": (
                "YouTube jiyasdelight channel enabled. Uses "
                "YOUTUBE_OAUTH_REFRESH_TOKEN_JIYA."
            ),
        },
    },
)


def _update_account(ws, headers, col_map, header_row, records, spec):
    account_id = spec["account_id"]
    target_row = None
    for idx, rec in enumerate(records, start=header_row + 1):
        if str(rec.get("account_id", "")).strip() == account_id:
            target_row = idx
            break

    if target_row is None:
        row = [spec["new_row"].get(h, "") for h in headers]
        ws.append_rows([row], value_input_option="USER_ENTERED")
        print(f"{account_id} was missing; appended enabled row.")
        return

    data = []
    for key, value in spec["updates"].items():
        col_idx = col_map.get(key)
        if not col_idx:
            continue
        data.append({
            "range": f"{SheetsReader._col_letter(col_idx)}{target_row}",
            "values": [[value]],
        })
    if data:
        ws.batch_update(data, value_input_option="USER_ENTERED")
    print(f"{account_id} updated on Accounts row {target_row}: enabled=Yes")


def main():
    reader = SheetsReader()
    ws = reader.accounts_ws
    header_row = reader.accounts_header_row
    headers = [str(h).strip() for h in ws.row_values(header_row)]
    col_map = {h.lower(): idx + 1 for idx, h in enumerate(headers) if h}
    records = ws.get_all_records(head=header_row)
    for spec in ACCOUNTS:
        _update_account(ws, headers, col_map, header_row, records, spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
