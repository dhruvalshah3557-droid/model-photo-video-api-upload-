#!/usr/bin/env python3
"""Write a compact production health report to the GitHub run summary."""
import json
import os
import re
from pathlib import Path


def count(pattern, text):
    return len(re.findall(pattern, text, flags=re.MULTILINE | re.IGNORECASE))


def main():
    log_path = Path("/tmp/auto_upload.log")
    meta_path = Path(os.getenv("META_SYNC_REPORT", "/tmp/meta_sync_report.json"))
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    uploaded = count(r"uploaded ->", log)
    failed = count(r": failed \(", log)
    skipped = count(r": skipped -", log)
    review = count(r"needs_review|Auto-cleaned|Auto-blocked", log)
    selected = sum(int(x) for x in re.findall(r"-> (\d+) healthy job\(s\) selected", log))
    new_accounts = meta.get("new_accounts") or []

    lines = [
        "## Auto Upload Health",
        "",
        "| Check | Result |",
        "|---|---:|",
        f"| Meta token | {'Valid' if meta.get('token_valid') else 'Invalid / not checked'} |",
        f"| Connected Meta pages | {meta.get('page_count', 0)} |",
        f"| New accounts added disabled | {len(new_accounts)} |",
        f"| Healthy jobs selected | {selected} |",
        f"| Uploaded | {uploaded} |",
        f"| Failed | {failed} |",
        f"| Skipped | {skipped} |",
        f"| Needs review | {review} |",
    ]
    if meta.get("error"):
        lines.extend(["", f"**Meta error:** {meta['error']}"])
    if new_accounts:
        lines.extend(["", "### Newly discovered accounts"])
        lines.extend(
            f"- {item['account_id']}: {item['name']} ({item['platform']}) — disabled pending review"
            for item in new_accounts
        )
    summary = "\n".join(lines) + "\n"
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(summary)
    print(summary)


if __name__ == "__main__":
    main()
