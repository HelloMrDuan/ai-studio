#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _request_json(url: str, token: str = "") -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "xiaoduan-studio-upstream-watch",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def inspect_upstream(item: dict[str, Any], token: str) -> dict[str, Any]:
    repository = str(item["repository"])
    tracked_commit = str(item.get("tracked_commit") or "")
    api = f"https://api.github.com/repos/{repository}"
    repo = _request_json(api, token)
    default_branch = str(repo.get("default_branch") or "main")
    latest = _request_json(f"{api}/commits/{default_branch}", token)
    latest_sha = str(latest.get("sha") or "")
    changed = bool(latest_sha and latest_sha != tracked_commit)
    comparison: dict[str, Any] | None = None
    if changed and tracked_commit:
        try:
            raw_compare = _request_json(f"{api}/compare/{tracked_commit}...{latest_sha}", token)
            files = raw_compare.get("files") or []
            tracked_paths = tuple(str(path) for path in item.get("tracked_paths") or [])
            relevant_files = [
                file.get("filename")
                for file in files
                if any(str(file.get("filename") or "").startswith(path) for path in tracked_paths)
            ]
            comparison = {
                "ahead_by": raw_compare.get("ahead_by"),
                "total_commits": raw_compare.get("total_commits"),
                "relevant_files": relevant_files,
                "high_value_review": bool(relevant_files),
            }
        except Exception as exc:  # comparison is advisory; latest SHA is still useful
            comparison = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "id": item.get("id"),
        "repository": repository,
        "license": item.get("license"),
        "tracked_commit": tracked_commit,
        "latest_commit": latest_sha,
        "default_branch": default_branch,
        "changed": changed,
        "comparison": comparison,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Xiaoduan Studio upstream watch", ""]
    lines.append(f"Checked: {report['checked_at']}")
    lines.append("")
    for item in report["upstreams"]:
        lines.append(f"## {item.get('repository')}")
        if item.get("error"):
            lines.append(f"- status: ERROR — {item['error']}")
        else:
            lines.append(f"- changed: {item['changed']}")
            lines.append(f"- tracked: `{item['tracked_commit']}`")
            lines.append(f"- latest: `{item['latest_commit']}`")
            comparison = item.get("comparison") or {}
            relevant = comparison.get("relevant_files") or []
            lines.append(f"- relevant changed files: {len(relevant)}")
            if relevant:
                for filename in relevant[:30]:
                    lines.append(f"  - `{filename}`")
                lines.append("- review disposition: HIGH_VALUE_REVIEW")
            elif item["changed"]:
                lines.append("- review disposition: REVIEW")
            else:
                lines.append("- review disposition: CURRENT")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/upstreams.json")
    parser.add_argument("--output", default="upstream-watch-report.json")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    token = os.environ.get("GITHUB_TOKEN", "")
    results: list[dict[str, Any]] = []
    for item in config.get("upstreams") or []:
        try:
            results.append(inspect_upstream(item, token))
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as exc:
            results.append(
                {
                    "id": item.get("id"),
                    "repository": item.get("repository"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    report = {
        "schema_version": "1.0",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "upstreams": results,
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = render_markdown(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
