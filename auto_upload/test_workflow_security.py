#!/usr/bin/env python3
"""Secret-free checks for GitHub Actions cache and artifact paths."""

from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
SENSITIVE_PATH = re.compile(
    r"(?:^|[/_.-])(?:credentials?|tokens?|secrets?|private[-_]?keys?)(?:$|[/_. *-])",
    re.IGNORECASE,
)


def action_steps(text):
    """Yield workflow step text without requiring a YAML dependency."""
    starts = list(re.finditer(r"(?m)^\s*-\s+name:\s*", text))
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        yield text[match.start():end]


def configured_paths(step):
    """Return scalar or block values configured under a path key."""
    lines = step.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)path:\s*(.*)$", line)
        if not match:
            continue
        indent = len(match.group(1))
        value = match.group(2).strip().strip("'\"")
        if value not in {"", "|", ">"}:
            yield value
            continue
        for continuation in lines[index + 1:]:
            stripped = continuation.strip()
            if not stripped:
                continue
            continuation_indent = len(continuation) - len(continuation.lstrip())
            if continuation_indent <= indent:
                break
            yield stripped.strip("'\"")


class WorkflowSecurityPolicyTest(unittest.TestCase):
    def test_cache_and_artifact_paths_exclude_sensitive_files(self):
        violations = []
        for workflow in sorted(WORKFLOW_DIR.glob("*.y*ml")):
            text = workflow.read_text(encoding="utf-8")
            for step in action_steps(text):
                if not re.search(
                    r"uses:\s*actions/(?:cache|upload-artifact)@",
                    step,
                    re.IGNORECASE,
                ):
                    continue
                for path in configured_paths(step):
                    if SENSITIVE_PATH.search(path):
                        violations.append(f"{workflow.name}: {path}")
        self.assertEqual(
            violations,
            [],
            "Sensitive files must never be persisted in Actions cache/artifacts: "
            + ", ".join(violations),
        )

    def test_production_workflow_always_removes_runtime_credentials(self):
        workflow = (
            WORKFLOW_DIR / "auto-upload-production.yml"
        ).read_text(encoding="utf-8")
        cleanup_step = next(
            (
                step
                for step in action_steps(workflow)
                if "name: Remove runtime credential files" in step
            ),
            "",
        )
        self.assertIn("if: always()", cleanup_step)
        for path in (
            "credentials/service_account.json",
            "credentials/youtube_client_secret.json",
            "credentials/youtube_token*.pickle",
        ):
            self.assertIn(f"rm -f {path}", cleanup_step)


if __name__ == "__main__":
    unittest.main()
