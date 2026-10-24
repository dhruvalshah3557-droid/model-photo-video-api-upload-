"""Autonomous LLM issue-fixer for the auto_upload repository.

Watches issues labeled `auto-fix`, asks the configured LLM to produce a fix,
validates it locally (git apply + py_compile), and opens a pull request. All
work happens on a feature branch; master is never modified directly.

Requires USER_LLM_API_KEY in the repository secrets. When the key is missing
the script exits cleanly so the workflow remains green while disabled.
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com/repos"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
NO_FIX_MARKER = "NO_FIX"
AUTO_FIX_LABEL = "auto-fix"
SKIP_LABELS = {"auto-pr", "needs-human", "wontfix"}
MAX_SELECT_FILES = 8
MAX_FILE_CHARS = 18000
MAX_CONTEXT_CHARS = 80000
MAX_DIFF_CHARS = 40000
USER_AGENT = "auto-upload-issue-fixer"
PIPELINE_TEST_TIMEOUT = 90


def repo():
    return os.environ["GITHUB_REPOSITORY"]


def log(*args):
    print("[issue-fixer]", *args)


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


def llm_chat(api_key, base_url, model, messages):
    payload = {"model": model, "messages": messages, "temperature": 0}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer %s" % api_key)
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def llm_chat_with_retry(api_key, base_url, model, messages, attempts=3):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return llm_chat(api_key, base_url, model, messages)
        except urllib.error.HTTPError as exc:
            last_error = "HTTP %s" % exc.code
            if exc.code == 429:
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(2 ** attempt)
    raise RuntimeError("LLM request failed: %s" % last_error)


def event_issue():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return None
    with open(event_path) as handle:
        event = json.load(handle)
    return event.get("issue")


def list_auto_fix_issues(token):
    qs = urllib.parse.urlencode({"state": "open", "per_page": 100})
    status, data = gh_api(token, "GET", "/issues?" + qs)
    if status != 200 or not isinstance(data, list):
        log("Could not list issues (HTTP %s)" % status)
        return []
    issues = []
    for issue in data:
        if "pull_request" in issue:
            continue
        labels = {label["name"] for label in issue.get("labels", [])}
        if AUTO_FIX_LABEL in labels:
            issues.append(issue)
    return issues


def repo_file_index():
    index = []
    for root, _, files in os.walk("auto_upload"):
        for name in files:
            if name.endswith(".py"):
                index.append(os.path.join(root, name))
    for name in os.listdir(".github/workflows"):
        if name.endswith((".yml", ".yaml")):
            index.append(os.path.join(".github/workflows", name))
    index.sort()
    return index


def parse_json_array(text):
    cleaned = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE)
    cleaned = cleaned.strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return []
    return [p for p in parsed if isinstance(p, str)]


def select_files(api_key, base_url, model, issue):
    body = issue.get("body") or ""
    title = issue.get("title") or ""
    files = repo_file_index()
    messages = [
        {"role": "system", "content": (
            "You select which source files are relevant to a GitHub issue. "
            "Reply with ONLY a JSON array of file paths, no prose."
        )},
        {"role": "user", "content": (
            "Issue title: %s\n\nIssue body:\n%s\n\n"
            "Repository files:\n%s\n\n"
            "Reply with a JSON array (max %d) of the file paths most relevant "
            "to fixing this issue."
        ) % (title, body, "\n".join(files), MAX_SELECT_FILES)},
    ]
    content = llm_chat_with_retry(api_key, base_url, model, messages)
    selected = parse_json_array(content)
    return [f for f in selected if os.path.isfile(f) and f in files]


def read_selected_files(paths):
    parts = []
    total = 0
    for path in paths:
        with open(path, errors="replace") as handle:
            text = handle.read(MAX_FILE_CHARS)
        parts.append("=== %s ===\n%s" % (path, text))
        total += len(text)
        if total >= MAX_CONTEXT_CHARS:
            break
    return "\n\n".join(parts)


def request_diff(api_key, base_url, model, issue, context):
    messages = [
        {"role": "system", "content": (
            "You are an expert Python engineer fixing a bug in a social-media "
            "auto-uploader. Produce ONLY a unified git diff that fixes the "
            "issue. Prefer the smallest correct change. Do not invent secrets, "
            "do not delete tests, and keep existing function names. If no code "
            "change can fix it, reply with exactly %s. Never include markdown "
            "fences, prose, or explanations."
        ) % NO_FIX_MARKER},
        {"role": "user", "content": (
            "Issue title: %s\n\nIssue body:\n%s\n\n"
            "Relevant file contents:\n%s\n\n"
            "Return only a unified git diff."
        ) % (issue.get("title", ""), issue.get("body") or "", context)},
    ]
    return llm_chat_with_retry(api_key, base_url, model, messages)


def request_corrected_diff(api_key, base_url, model, issue, context, diff, error):
    messages = [
        {"role": "system", "content": (
            "You are an expert Python engineer. The previous diff you produced "
            "did not apply. Fix the diff. Reply with ONLY a corrected unified "
            "git diff, or %s if the issue cannot be fixed in code."
        ) % NO_FIX_MARKER},
        {"role": "user", "content": (
            "Issue title: %s\n\nRelevant file contents:\n%s\n\n"
            "Your previous diff:\n%s\n\n"
            "git apply error:\n%s\n\n"
            "Return only a corrected unified git diff."
        ) % (issue.get("title", ""), context, diff, error)},
    ]
    return llm_chat_with_retry(api_key, base_url, model, messages)


def strip_fences(diff):
    diff = re.sub(r"^```(diff)?|```$", "", diff.strip(), flags=re.MULTILINE)
    return diff.strip()


def apply_and_validate(diff):
    diff = strip_fences(diff)
    if not diff:
        return False, "empty diff"
    with open("/tmp/issue_fixer.diff", "w") as handle:
        handle.write(diff + "\n")
    check = subprocess.run(
        ["git", "apply", "--check", "/tmp/issue_fixer.diff"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        return False, check.stderr.strip() or "git apply failed"
    apply = subprocess.run(
        ["git", "apply", "/tmp/issue_fixer.diff"],
        capture_output=True, text=True,
    )
    if apply.returncode != 0:
        return False, apply.stderr.strip() or "git apply failed"
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True,
    )
    changed = []
    for line in porcelain.stdout.splitlines():
        if line.startswith("R "):
            changed.append(line.split(" -> ")[-1])
        else:
            changed.append(line[3:])
    changed = [path for path in changed if path.strip()]
    if not changed:
        return False, "diff applied but produced no tracked changes"
    whitespace = subprocess.run(
        ["git", "diff", "--check"], capture_output=True, text=True,
    )
    if whitespace.returncode != 0:
        return False, whitespace.stdout.strip() or "git diff --check failed"
    for path in changed:
        if path.endswith(".py"):
            compiled = subprocess.run(
                ["python3", "-m", "py_compile", path],
                capture_output=True, text=True,
            )
            if compiled.returncode != 0:
                return False, "py_compile failed for %s:\n%s" % (
                    path, compiled.stderr.strip())
    try:
        tests = subprocess.run(
            ["python3", "-m", "unittest", "discover", "-p", "test_*.py"],
            cwd="auto_upload",
            capture_output=True, text=True, timeout=PIPELINE_TEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "unittest timed out after %ss" % PIPELINE_TEST_TIMEOUT
    if tests.returncode != 0:
        return False, "unittest failed:\n%s" % (
            (tests.stderr or tests.stdout or "unknown test failure")[-4000:]
        )
    return True, changed


def discard_working_changes():
    subprocess.run(["git", "checkout", "--", "."],
                   capture_output=True, text=True)


def commit_and_push(token, issue, changed):
    branch = "fix/issue-%s" % issue["number"]
    subprocess.run(["git", "checkout", "-b", branch], check=True)
    subprocess.run(["git", "add", "-A"], check=True)
    message = "fix: %s\n\nCloses #%s" % (issue.get("title", "").strip(), issue["number"])
    subprocess.run(
        ["git", "-c", "user.name=github-actions[bot]",
         "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
         "commit", "-m", message],
        check=True,
    )
    remote = "https://x-access-token:%s@github.com/%s.git" % (token, repo())
    push = subprocess.run(
        ["git", "push", remote, branch], capture_output=True, text=True,
    )
    if push.returncode != 0:
        raise RuntimeError("push failed: %s" % push.stderr.strip())
    status, pr = gh_api(token, "POST", "/pulls", {
        "title": "fix: %s" % issue.get("title", "").strip()[:100],
        "head": branch,
        "base": "master",
        "body": "Fixes #%s\n\nChanges: %s" % (issue["number"], ", ".join(changed)),
    })
    if status not in (200, 201):
        raise RuntimeError("PR creation failed (HTTP %s)" % status)
    return pr["number"], pr["html_url"]


def add_label(token, issue_number, label):
    gh_api(token, "POST", "/issues/%s/labels" % issue_number, {"labels": [label]})


def comment_on_issue(token, issue_number, body):
    gh_api(token, "POST", "/issues/%s/comments" % issue_number, {"body": body})


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as handle:
            handle.write(text + "\n")


def handle_issue(token, api_key, base_url, model, issue):
    number = issue["number"]
    labels = {label["name"] for label in issue.get("labels", [])}
    if SKIP_LABELS & labels:
        log("Issue #%s skipped (label %s)" % (
            number, sorted(SKIP_LABELS & labels)))
        return "skipped", None

    changed = None
    reason = None
    try:
        selected = select_files(api_key, base_url, model, issue)
        if not selected:
            reason = "the LLM could not identify relevant files"
            return None, reason
        context = read_selected_files(selected)
        diff = request_diff(api_key, base_url, model, issue, context)
        if NO_FIX_MARKER in diff.upper():
            reason = "the LLM reported no code fix is possible"
            return None, reason
        ok, result = apply_and_validate(diff)
        if not ok:
            corrected = request_corrected_diff(
                api_key, base_url, model, issue, context, diff, result)
            if NO_FIX_MARKER in corrected.upper():
                reason = "the LLM reported no code fix is possible"
                return None, reason
            ok, result = apply_and_validate(corrected)
            if not ok:
                reason = "the generated fix could not be applied or validated: %s" % result
                return None, reason
        changed = result
    except Exception as exc:
        reason = "fixer encountered an error: %s" % exc
        discard_working_changes()
        return None, reason

    try:
        pr_number, pr_url = commit_and_push(token, issue, changed)
        add_label(token, number, "auto-pr")
        comment_on_issue(
            token, number,
            "Auto-fix agent opened PR #%s: %s" % (pr_number, pr_url),
        )
        return "PR #%s opened" % pr_number, None
    except Exception as exc:
        reason = "could not open PR: %s" % exc
        discard_working_changes()
        return None, reason


def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    api_key = os.environ.get("USER_LLM_API_KEY", "")
    if not token:
        log("No GITHUB_TOKEN; issue-fixer disabled.")
        return
    if not api_key:
        log("USER_LLM_API_KEY not configured; issue-fixer disabled. "
            "Add the secret to GitHub Settings > Secrets and variables "
            "> Actions to enable autonomous fixes.")
        write_summary("Issue Fixer disabled: add `USER_LLM_API_KEY` to GitHub Secrets.")
        return

    base_url = os.environ.get("USER_LLM_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("USER_LLM_MODEL", DEFAULT_MODEL)
    try:
        max_issues = int(os.environ.get("ISSUE_FIXER_MAX_ISSUES", "1"))
    except ValueError:
        max_issues = 1

    event_issue_obj = event_issue()
    if event_issue_obj:
        labels = {label["name"] for label in event_issue_obj.get("labels", [])}
        if AUTO_FIX_LABEL in labels:
            candidates = [event_issue_obj]
        else:
            candidates = []
    else:
        candidates = list_auto_fix_issues(token)

    summary_lines = []
    processed = 0
    for issue in candidates:
        if processed >= max_issues:
            break
        log("Processing issue #%s" % issue["number"])
        outcome, reason = handle_issue(token, api_key, base_url, model, issue)
        if outcome:
            summary_lines.append("- Issue #%s: %s" % (issue["number"], outcome))
        else:
            summary_lines.append("- Issue #%s: needs human review (%s)" % (
                issue["number"], reason))
            add_label(token, issue["number"], "needs-human")
            comment_on_issue(
                token, issue["number"],
                "Auto-fix agent could not resolve this issue: %s. "
                "Marked for manual review." % reason,
            )
        processed += 1

    if not summary_lines:
        summary_lines.append("- No `auto-fix` issues to process.")
    report = "## Issue Fixer Report\n\n" + "\n".join(summary_lines)
    log(report)
    write_summary(report)


if __name__ == "__main__":
    main()
