#!/usr/bin/env python3
"""Enable LINE-CD on the Accounts sheet without rewriting other rows.

LINE-CD is already present on the workbook. This script only updates that
row (enabled, username, notes) or appends it if missing. Other accounts
are left untouched.
"""
import sys

from sheets_reader import SheetsReader

ACCOUNT_ID = "LINE-CD"
UPDATES = {
    "enabled": "Yes",
    "username_or_channel": "colourdiam",
    "credential_property_key": "META_TOKEN_LINE_CD",
    "approval_required": "No",
    "product_tagging": "No",
    "notes": (
        "LINE Messaging API enabled. Broadcasts carousel/image/video jobs "
        "to Official Account followers. Token: LINE_CHANNEL_ACCESS_TOKEN "
        "or META_TOKEN_LINE_CD."
    ),
}
NEW_ROW = {
    "account_id": ACCOUNT_ID,
    "platform": "LINE",
    "account_name": "Colour Diam LINE",
    "platform_account_id": "",
    "username_or_channel": "colourdiam",
    "primary_language": "en-GB",
    "fallback_language": "en-GB",
    "timezone": "Asia/Bangkok",
    "enabled": "Yes",
    "allowed_formats": "Image Post, Carousel, Video Message",
    "caption_style": "International luxury; concise",
    "hashtag_set": "Product-specific tags",
    "cta_rule": "Visit shop",
    "posting_window": "10:00-21:00 local",
    "min_gap_minutes": "180",
    "product_tagging": "No",
    "catalog_or_store_id": "",
    "credential_property_key": "META_TOKEN_LINE_CD",
    "approval_required": "No",
    "notes": UPDATES["notes"],
}


def main():
    reader = SheetsReader()
    ws = reader.accounts_ws
    header_row = reader.accounts_header_row
    headers = [str(h).strip() for h in ws.row_values(header_row)]
    col_map = {h.lower(): idx + 1 for idx, h in enumerate(headers) if h}

    records = ws.get_all_records(head=header_row)
    target_row = None
    for idx, rec in enumerate(records, start=header_row + 1):
        if str(rec.get("account_id", "")).strip() == ACCOUNT_ID:
            target_row = idx
            break

    if target_row is None:
        row = [NEW_ROW.get(h, "") for h in headers]
        ws.append_rows([row], value_input_option="USER_ENTERED")
        print("LINE-CD was missing; appended enabled row.")
        return 0

    data = []
    for key, value in UPDATES.items():
        col_idx = col_map.get(key)
        if not col_idx:
            continue
        data.append({
            "range": f"{SheetsReader._col_letter(col_idx)}{target_row}",
            "values": [[value]],
        })
    if data:
        ws.batch_update(data, value_input_option="USER_ENTERED")
    print(f"LINE-CD updated on Accounts row {target_row}: enabled=Yes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
