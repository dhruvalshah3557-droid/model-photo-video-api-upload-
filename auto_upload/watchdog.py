"""Pipeline watchdog for the auto_upload repository.

Monitors recent Auto Upload workflow runs and the public Publishing Queue
state, escalates persistent failures to a GitHub issue labeled `auto-fix`, and
posts a short health report. Credential/permission failures (invalidated or
expired tokens, etc.) are detected by signature and routed to a single
`manual-review` issue instead, since no code change can fix them. Every 30
minutes it also calculates the rolling 24-hour delivery deficit for each
publish-ready Facebook, Instagram and YouTube account and dispatches Auto
    Upload Production when a deficit account is due after the 30-minute spacing
    period.

No extra secrets are required: workflow-run data comes from the Actions API and
sheet state comes from the publicly readable Google Sheet via gviz.
"""
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from delivery_policy import (  # noqa: E402
    MINIMUM_GAP_HOURS,
    MINIMUM_POSTS_24H,
    due_deficit_accounts,
    rolling_activity,
)

WORKBOOK_ID = "1jjC4oaWsyqLzG6vT5EwJkVAgJCXGpz_7fWr6wb7OU3o"
GVIZ_URL = "https://docs.google.com/spreadsheets/d/%s/gviz/tq" % WORKBOOK_ID
QUEUE_SHEET = "Publishing Queue"
ACCOUNTS_SHEET = "Accounts"
WORKFLOW_FILE = ".github/workflows/auto-upload-production.yml"
IN_FLIGHT_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}
LEDGER_TITLE = "Watchdog Ledger"
AUTO_FIX_LABEL = "auto-fix"
MANUAL_REVIEW_LABEL = "manual-review"
CONSECUTIVE_FAILURE_THRESHOLD = 3
ZERO_UPLOAD_STALL_THRESHOLD = 2
ZERO_UPLOAD_RE = re.compile(
    r"(\d+)\s+pending\s+->\s+(\d+)\s+healthy job\(s\) selected",
    re.IGNORECASE,
)
STALL_SIGNAL_PATTERNS = (
    re.compile(r"No healthy upload candidates found", re.IGNORECASE),
    re.compile(r"Media host does not resolve in DNS", re.IGNORECASE),
    re.compile(r"Source row integrity mismatch", re.IGNORECASE),
    re.compile(r"No healthy candidate for enabled account", re.IGNORECASE),
)
ACTIONABLE_STALL_PATTERNS = (
    re.compile(r"Media host does not resolve in DNS", re.IGNORECASE),
    re.compile(r"Source row integrity mismatch", re.IGNORECASE),
    re.compile(
        r"No healthy candidate for enabled account \S+ \((youtube|tiktok|facebook)\)",
        re.IGNORECASE,
    ),
)

# Credential / permission / configuration failures can never be fixed by the
# Issue Fixer (no code change can refresh an invalidated access token). The
# GitHub wrapper line is a poor signature, so the Watchdog scans the full run
# log for these signals and routes matches to a single `manual-review` issue
# instead of spamming fresh `auto-fix` issues on every 30-minute tick.
CREDENTIAL_SIGNAL_PATTERNS = (
    re.compile(r"code[=:\s]?190\b", re.IGNORECASE),
    re.compile(r"subcode[=:\s]?460\b", re.IGNORECASE),
    re.compile(r"error (code )?190\b", re.IGNORECASE),
    re.compile(r"\(#190\)", re.IGNORECASE),
    re.compile(r"session has been invalidated", re.IGNORECASE),
    re.compile(r"user changed (their )?password", re.IGNORECASE),
    re.compile(r"access token has expired", re.IGNORECASE),
    re.compile(r"access token (is |was )?invalid", re.IGNORECASE),
    re.compile(r"error validating access token", re.IGNORECASE),
    re.compile(r"invalid(?:ated)? access token", re.IGNORECASE),
    re.compile(r"oauthexception", re.IGNORECASE),
    re.compile(r"(oauth|bearer) token (expired|invalid|revoked)", re.IGNORECASE),
    re.compile(r"token (has |is )?(expired|invalid|revoked)", re.IGNORECASE),
    re.compile(r"invalid.?grant", re.IGNORECASE),
    re.compile(r"invalid credentials", re.IGNORECASE),
    re.compile(r"authentication failed", re.IGNORECASE),
    re.compile(r"re-?authenticate", re.IGNORECASE),
    re.compile(r"log (back )?in to (facebook|instagram)", re.IGNORECASE),
    re.compile(r"checkpoint", re.IGNORECASE),
    re.compile(r"\b401 (unauthorized|invalid)\b", re.IGNORECASE),
)
MIN_HEADER_MATCHES = 3
QUEUE_HEADERS = {
    "job_id", "sku", "account_id", "media_selection", "platform", "format",
    "language", "scheduled_at", "timezone", "stock_id_tag", "status",
    "attempts", "last_attempt_at", "platform_post_id", "published_url",
    "error_message", "notes", "tagging_status", "tag_stock_id_used",
    "caption_final",
}
STATUS_HEADER = "status"
ACCOUNT_HEADERS = {
    "account_id", "platform", "account_name", "platform_account_id",
    "enabled", "timezone", "credential_property_key",
}
WORKFLOW_NAME = "Auto Upload Production"

API_ROOT = "https://api.github.com/repos"
USER_AGENT = "auto-upload-watchdog"


def repo():
    return os.environ["GITHUB_REPOSITORY"]


def gh_api(token, method, path, body=None):
    url = API_ROOT + "/" + repo() + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def get_workflow_runs(token):
    qs = urllib.parse.urlencode({"per_page": 20})
    encoded = urllib.parse.quote(WORKFLOW_FILE, safe="")
    path = "/actions/workflows/%s/runs?%s" % (encoded, qs)
    status, data = gh_api(token, "GET", path)
    if status != 200 or not isinstance(data, dict):
        return False, []
    return True, data.get("workflow_runs", [])


def get_run_log(token, run_id):
    url = API_ROOT + "/" + repo() + "/actions/runs/%s/logs" % run_id
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                parts = []
                for name in sorted(archive.namelist()):
                    if not name.endswith("/"):
                        parts.append(archive.read(name).decode("utf-8", errors="replace"))
                return "\n".join(parts)
        except zipfile.BadZipFile:
            return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def extract_error_signature(log_text):
    if not log_text:
        return "unknown (run log unavailable)"
    lines = [line for line in log_text.splitlines() if line.strip()]
    tail = lines[-200:]
    for line in reversed(tail):
        low = line.lower()
        if "error:" in low or "traceback" in low or "exception" in low:
            return re.sub(r"\s+", " ", line.strip())[:220]
    for line in reversed(tail):
        low = line.lower()
        if "error" in low or "failed" in low:
            return re.sub(r"\s+", " ", line.strip())[:220]
    return "unknown failure (no error text in log tail)"


def parse_selected_jobs(log_text):
    """Return (pending, selected) from a production log, or (None, None)."""
    if not log_text:
        return None, None
    match = ZERO_UPLOAD_RE.search(log_text)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def extract_stall_signature(log_text):
    if not log_text:
        return "zero healthy jobs selected (run log unavailable)"
    for pattern in ACTIONABLE_STALL_PATTERNS + STALL_SIGNAL_PATTERNS:
        for line in log_text.splitlines():
            if pattern.search(line):
                return re.sub(r"\s+", " ", line.strip())[:220]
    pending, selected = parse_selected_jobs(log_text)
    if selected == 0:
        return "Optimized queue: %s pending -> 0 healthy job(s) selected" % (
            pending if pending is not None else "?"
        )
    return "zero healthy jobs selected"


def is_actionable_stall(log_text):
    """True when a green 0-upload run failed for a code-fixable media/source reason.

    Instagram Meta cooldown with Facebook already at the 24h floor is expected
    and must not open auto-fix issues.
    """
    if not log_text:
        return False
    return any(pattern.search(log_text) for pattern in ACTIONABLE_STALL_PATTERNS)


def is_zero_upload_run(run, log_text=None):
    if str(run.get("conclusion", "") or "").strip().lower() != "success":
        return False
    pending, selected = parse_selected_jobs(log_text or "")
    return selected == 0


def consecutive_zero_upload_runs(token, runs, limit=3):
    """Inspect recent successful production runs for 0-selected-job stalls."""
    stalled = []
    for run in (runs or [])[:limit]:
        log_text = get_run_log(token, run["id"])
        if not is_zero_upload_run(run, log_text):
            break
        pending, selected = parse_selected_jobs(log_text)
        stalled.append((run, log_text, pending, selected))
    return stalled


def is_credential_failure(signature):
    """True when the failure looks like a credential/permission problem that
    code cannot fix, so the Watchdog must route it to manual review."""
    return any(pattern.search(signature) for pattern in CREDENTIAL_SIGNAL_PATTERNS)


def credential_line(log_text):
    """Return a compact line from the log that carries a credential signal, so
    manual-review issues are titled/described by the real root cause rather than
    the generic GitHub wrapper line. Returns '' when no signal is found."""
    if not log_text:
        return ""
    for line in log_text.splitlines():
        if any(pattern.search(line) for pattern in CREDENTIAL_SIGNAL_PATTERNS):
            return re.sub(r"\s+", " ", line.strip())[:220]
    return ""


def gviz_table(sheet_name):
    qs = urllib.parse.urlencode({"sheet": sheet_name, "tqx": "out:json"})
    req = urllib.request.Request(GVIZ_URL + "?" + qs)
    req.add_header("User-Agent", "Mozilla/5.0 auto-upload-watchdog")
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {"cols": [], "rows": []}
    return json.loads(text[start:end + 1]).get("table", {})


def _cell_value(cell):
    if not isinstance(cell, dict):
        return cell
    value = cell.get("v")
    if value not in (None, ""):
        return value
    return cell.get("f")


def gviz_matrix(sheet_name):
    table = gviz_table(sheet_name)
    cols = [str(c.get("label", "") or "") for c in table.get("cols", [])]
    matrix = [cols]
    for row in table.get("rows", []):
        cells = row.get("c") or []
        matrix.append([_cell_value(cell) for cell in cells])
    return matrix


def _header_index(matrix, required, min_matches=MIN_HEADER_MATCHES):
    for idx, row in enumerate(matrix[:6]):
        norm = {str(v).strip().lower() for v in row if str(v)}
        if len(norm & required) >= min_matches:
            return idx
    return None


def records_from_matrix(matrix, required, min_matches=MIN_HEADER_MATCHES):
    header_idx = _header_index(matrix, required, min_matches)
    if header_idx is None:
        return None, "could not locate header row"
    headers = [str(h or "").strip().lower() for h in matrix[header_idx]]
    records = []
    for row in matrix[header_idx + 1:]:
        rec = {}
        for idx, header in enumerate(headers):
            if not header:
                continue
            rec[header] = row[idx] if idx < len(row) else None
        if any(str(v or "").strip() for v in rec.values()):
            records.append(rec)
    return records, None


def load_accounts():
    try:
        matrix = gviz_matrix(ACCOUNTS_SHEET)
    except Exception as exc:
        return [], "gviz fetch failed: %s" % exc
    records, error = records_from_matrix(matrix, ACCOUNT_HEADERS)
    if error:
        return [], error
    accounts = []
    for rec in records:
        account_id = str(rec.get("account_id", "") or "").strip()
        if not account_id:
            continue
        enabled = str(rec.get("enabled", "") or "").strip().lower() in (
            "yes", "true", "1", "y",
        )
        accounts.append({
            "account_id": account_id,
            "platform": str(rec.get("platform", "") or "").strip().lower(),
            "account_name": str(rec.get("account_name", "") or "").strip(),
            "platform_account_id": str(rec.get("platform_account_id", "") or "").strip(),
            "timezone": str(rec.get("timezone", "") or "").strip(),
            "enabled": enabled,
        })
    return accounts, None


def load_queue_rows():
    try:
        matrix = gviz_matrix(QUEUE_SHEET)
    except Exception as exc:
        return [], "gviz fetch failed: %s" % exc
    records, error = records_from_matrix(matrix, QUEUE_HEADERS)
    if error:
        return [], error
    return records, None


def delivery_deficits(accounts, queue_rows, now=None):
    activity = rolling_activity(queue_rows, accounts, now)
    due = due_deficit_accounts(accounts, activity, now)
    return activity, due


def production_in_flight(runs):
    for run in runs or []:
        if str(run.get("status", "") or "").strip().lower() in IN_FLIGHT_STATUSES:
            return True
    return False


def should_dispatch_production(due, runs):
    return bool(due) and not production_in_flight(runs)


def dispatch_production(token, ref=None):
    ref = ref or os.environ.get("GITHUB_REF_NAME") or "master"
    encoded = urllib.parse.quote(WORKFLOW_FILE, safe="")
    status, _data = gh_api(
        token,
        "POST",
        "/actions/workflows/%s/dispatches" % encoded,
        {"ref": ref},
    )
    if status in (200, 204):
        return "dispatched Auto Upload Production"
    return "could not dispatch Auto Upload Production (HTTP %s)" % status


def queue_status_counts(queue_rows=None, error=None):
    if error:
        return {"error": error}
    if queue_rows is None:
        queue_rows, error = load_queue_rows()
        if error:
            return {"error": error}
    counts = {}
    for rec in queue_rows or []:
        value = rec.get(STATUS_HEADER)
        if value:
            value = str(value)
            counts[value] = counts.get(value, 0) + 1
    return counts


def list_open_issues(token):
    issues = []
    page = 1
    while True:
        qs = urllib.parse.urlencode({
            "state": "open", "per_page": 100, "page": page,
        })
        status, data = gh_api(token, "GET", "/issues?" + qs)
        if status != 200 or not isinstance(data, list):
            break
        issues.extend(i for i in data if "pull_request" not in i)
        if len(data) < 100:
            break
        page += 1
    return issues


def find_ledger_issue(token):
    for issue in list_open_issues(token):
        if issue.get("title") == LEDGER_TITLE:
            return issue
    return None


def get_or_create_ledger(token):
    issue = find_ledger_issue(token)
    if issue:
        return issue
    status, data = gh_api(token, "POST", "/issues", {
        "title": LEDGER_TITLE,
        "body": "Run IDs already escalated by the Watchdog, one per line.",
    })
    if status not in (200, 201):
        return None
    return data


def update_ledger(token, issue, handled_ids):
    body = "\n".join(sorted(handled_ids))
    gh_api(token, "PATCH", "/issues/%s" % issue["number"], {
        "body": "Run IDs already escalated by the Watchdog, one per line.\n\n%s" % body,
    })


def find_issue_by_title(token, title):
    for issue in list_open_issues(token):
        if issue.get("title") == title:
            return issue
    return None


def find_auto_fix_issue(token, signature):
    title = "[auto-fix] %s" % signature[:80]
    return find_issue_by_title(token, title)


def find_manual_review_issue(token, key):
    title = "[manual-review] %s" % key[:80]
    return find_issue_by_title(token, title)


def parse_run_ids(body):
    return {line.strip() for line in (body or "").splitlines()
            if line.strip().isdigit()}


def manual_review_body(key, run_ids):
    body = (
        "The Watchdog detected repeated Auto Upload failures that look like a "
        "credential, permission, or configuration problem rather than a code "
        "bug. No code change can fix an invalidated access token or secret, so "
        "this issue is routed for manual review and is NOT sent to the Issue "
        "Fixer agent.\n"
        "\n"
        "To resolve: refresh the affected credential/secret (e.g. the Facebook "
        "long-lived access token) and re-run Auto Upload Production. Once the "
        "pipeline is green again, close this issue.\n"
        "\n"
        "Root cause signal:\n"
        "```\n"
        "%s\n"
        "```\n"
        "\n"
        "Affected runs:\n"
        "%s"
    ) % (key, "\n".join(sorted(run_ids, key=int)))
    return body


def escalate_manual_review(token, key, failed_runs):
    """Handle a credential-type failure: track it on one `manual-review` issue
    (created on first sight, body updated in place afterwards) so the Issue
    Fixer is never asked to auto-fix an un-fixable error and the issue tracker
    is not spammed with a fresh issue per 30-minute tick."""
    run_ids = {str(r["id"]) for r in failed_runs}
    issue = find_manual_review_issue(token, key)
    if issue:
        existing = parse_run_ids(issue.get("body"))
        added = sorted(run_ids - existing, key=int)
        if added:
            new_body = manual_review_body(key, existing | run_ids)
            gh_api(token, "PATCH", "/issues/%s" % issue["number"],
                   {"body": new_body})
            return "updated manual-review issue #%s with run(s) %s" % (
                issue["number"], ", ".join(added))
        return "manual-review issue #%s already up to date" % issue["number"]

    legacy = find_auto_fix_issue(token, key)
    if legacy:
        number = legacy["number"]
        gh_api(token, "PATCH", "/issues/%s" % number, {
            "title": "[manual-review] %s" % key[:80],
            "body": manual_review_body(
                key, parse_run_ids(legacy.get("body")) | run_ids),
        })
        gh_api(token, "DELETE", "/issues/%s/labels/%s" % (
            number, urllib.parse.quote(AUTO_FIX_LABEL)))
        gh_api(token, "POST", "/issues/%s/labels" % number,
               {"labels": [MANUAL_REVIEW_LABEL]})
        return "converted auto-fix issue #%s to manual review" % number

    title = "[manual-review] %s" % key[:80]
    status, data = gh_api(token, "POST", "/issues", {
        "title": title,
        "body": manual_review_body(key, run_ids),
        "labels": [MANUAL_REVIEW_LABEL],
    })
    if status in (200, 201):
        return "created manual-review issue #%s" % data["number"]
    return "could not create issue (HTTP %s)" % status


def escalate_failure(token, signature, failed_runs):
    issue = find_auto_fix_issue(token, signature)
    if issue:
        ids = ", ".join(str(r["id"]) for r in failed_runs)
        gh_api(token, "POST", "/issues/%s/comments" % issue["number"], {
            "body": "Additional failures with the same signature: run(s) %s" % ids,
        })
        return "commented on existing issue #%s" % issue["number"]
    newest = failed_runs[0]
    title = "[auto-fix] %s" % signature[:80]
    body_lines = [
        "The Watchdog detected repeated Auto Upload failures.",
        "",
        "Latest failing run: [%s](%s)" % (newest["id"], newest.get("html_url", "")),
        "Affected runs: %s" % ", ".join(str(r["id"]) for r in failed_runs),
        "",
        "Error signature:",
        "```",
        signature,
        "```",
        "",
        "The Issue Fixer agent will attempt an automated fix. If it cannot, it",
        "will mark this issue for manual review.",
    ]
    status, data = gh_api(token, "POST", "/issues", {
        "title": title,
        "body": "\n".join(body_lines),
        "labels": [AUTO_FIX_LABEL],
    })
    if status in (200, 201):
        return "created issue #%s" % data["number"]
    return "could not create issue (HTTP %s)" % status


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as handle:
            handle.write(text + "\n")


def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("No GITHUB_TOKEN available; watchdog disabled.")
        return

    runs_ok, runs = get_workflow_runs(token)
    failures = [r for r in runs if r.get("conclusion") == "failure"]

    consec = []
    for run in runs:
        if run.get("conclusion") == "failure":
            consec.append(run)
        else:
            break

    lines = ["## Watchdog Report", ""]
    if not runs_ok:
        lines.append(
            "- Could not query workflow runs (Actions API error); "
            "verify the workflow has `actions: read` permission."
        )
        lines.append("- Recent runs: n/a")
    else:
        lines.append("- Recent runs: %d, failures: %d" % (len(runs), len(failures)))
        lines.append("- Consecutive failures at head: %d" % len(consec))

    if len(consec) >= CONSECUTIVE_FAILURE_THRESHOLD:
        newest = consec[0]
        ledger = get_or_create_ledger(token)
        ledger_ids = set()
        if ledger:
            for line in ledger.get("body", "").splitlines():
                line = line.strip()
                if line.isdigit():
                    ledger_ids.add(line)
        unseen = [r for r in consec if str(r["id"]) not in ledger_ids]
        if unseen:
            log_text = get_run_log(token, newest["id"])
            signature = extract_error_signature(log_text)
            credential = is_credential_failure(log_text) or is_credential_failure(
                signature)
            if credential:
                key = credential_line(log_text) or signature
                result = escalate_manual_review(token, key, consec)
                lines.append(
                    "- Escalation (credential/permission, manual review): %s"
                    % result)
            else:
                result = escalate_failure(token, signature, consec)
                lines.append("- Escalation: %s" % result)
            lines.append("- Signature: `%s`" % signature)
            if ledger:
                ledger_ids.update(str(r["id"]) for r in consec)
                update_ledger(token, ledger, ledger_ids)
        else:
            lines.append("- Escalation: already escalated (ledger up to date)")
    elif len(consec) > 0:
        lines.append(
            "- Below escalation threshold (%d); next cron run retries "
            "automatically." % CONSECUTIVE_FAILURE_THRESHOLD
        )
    else:
        lines.append("- No recent failures; pipeline healthy.")

    accounts, accounts_error = load_accounts()
    queue_rows, queue_error = load_queue_rows()
    counts = queue_status_counts(queue_rows, queue_error)
    lines.append("")
    lines.append("### Publishing Queue status counts")
    if "error" in counts:
        lines.append("- %s" % counts["error"])
    elif not counts:
        lines.append("- (empty)")
    else:
        for status_name in sorted(counts):
            lines.append("- %s: %d" % (status_name, counts[status_name]))

    lines.append("")
    lines.append("### 24h delivery floor (%s posts / %sh gap)" % (
        MINIMUM_POSTS_24H, MINIMUM_GAP_HOURS,
    ))
    if accounts_error:
        lines.append("- Accounts: %s" % accounts_error)
    elif queue_error:
        lines.append("- Queue: %s" % queue_error)
    else:
        activity, due = delivery_deficits(accounts, queue_rows)
        below = [
            (account_id, state)
            for account_id, state in sorted(activity.items())
            if int(state.get("count", 0) or 0) < MINIMUM_POSTS_24H
        ]
        if not activity:
            lines.append("- No publish-ready Facebook/Instagram/YouTube accounts.")
        elif not below:
            lines.append("- All publish-ready accounts meet the 24h floor.")
        else:
            for account_id, state in below:
                lines.append(
                    "- %s: %s/%s (deficit %s)" % (
                        account_id,
                        int(state.get("count", 0) or 0),
                        MINIMUM_POSTS_24H,
                        MINIMUM_POSTS_24H - int(state.get("count", 0) or 0),
                    )
                )
        if due:
            due_ids = ", ".join(
                "%s:%s" % (item["account_id"], item["deficit"]) for item in due
            )
            lines.append("- Due after %sh spacing: %s" % (MINIMUM_GAP_HOURS, due_ids))
            if should_dispatch_production(due, runs):
                result = dispatch_production(token)
                lines.append("- Catch-up dispatch: %s" % result)
            elif production_in_flight(runs):
                lines.append("- Catch-up dispatch: skipped (production already in flight)")
            else:
                lines.append("- Catch-up dispatch: skipped")
        else:
            lines.append("- Catch-up dispatch: not needed")

        if due and runs_ok:
            stalled = consecutive_zero_upload_runs(
                token, runs, limit=ZERO_UPLOAD_STALL_THRESHOLD
            )
            if len(stalled) >= ZERO_UPLOAD_STALL_THRESHOLD:
                _newest_run, log_text, pending, selected = stalled[0]
                if is_actionable_stall(log_text):
                    signature = extract_stall_signature(log_text)
                    result = escalate_failure(
                        token,
                        signature,
                        [item[0] for item in stalled],
                    )
                    lines.append(
                        "- Zero-upload stall: %s pending -> %s selected; %s"
                        % (pending if pending is not None else "?", selected, result)
                    )
                    lines.append("- Stall signature: `%s`" % signature)
                else:
                    lines.append(
                        "- Zero-upload stall: ignored (Instagram cooldown or accounts already at floor)"
                    )

    report = "\n".join(lines)
    print(report)
    write_summary(report)


if __name__ == "__main__":
    main()
