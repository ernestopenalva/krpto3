#!/usr/bin/env python3
"""Timeline offline de commits relevantes para monitor/position/entrada."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path
from typing import Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_CSV = PROJECT_ROOT / "data" / "studies" / "entry_feature_outcome" / "git_timeline.csv"
DEFAULT_TERMS = (
    "monitor",
    "position",
    "abb",
    "trailing",
    "stop",
    "breakeven",
    "momentum",
    "pullback",
    "buy_signals",
    "config",
)


def run_git(args: List[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout


def parse_log(raw: str) -> List[Dict[str, str]]:
    commits: List[Dict[str, str]] = []
    current: Dict[str, str] | None = None
    files: List[str] = []
    for line in raw.splitlines():
        if line.startswith("COMMIT\t"):
            if current is not None:
                current["files_changed"] = ";".join(files)
                commits.append(current)
            _marker, commit_hash, date, subject = line.split("\t", 3)
            current = {"hash": commit_hash, "date": date, "subject": subject}
            files = []
            continue
        if current is not None and line.strip():
            files.append(line.strip())
    if current is not None:
        current["files_changed"] = ";".join(files)
        commits.append(current)
    return commits


def relevance_for(commit: Dict[str, str], terms: List[str]) -> List[str]:
    haystack = f"{commit.get('subject', '')} {commit.get('files_changed', '')}".lower()
    return [term for term in terms if term.lower() in haystack]


def write_csv(path: Path, rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["hash", "date", "subject", "files_changed", "relevance_terms", "is_relevant"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera timeline offline de commits relevantes.")
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--all", action="store_true", help="Imprime todos os commits, nao apenas relevantes.")
    parser.add_argument("--terms", default=",".join(DEFAULT_TERMS))
    args = parser.parse_args()

    git_args = ["log", "--date=iso-strict", "--pretty=format:COMMIT%x09%H%x09%cI%x09%s", "--name-only"]
    if args.since:
        git_args.append(f"--since={args.since}")
    if args.until:
        git_args.append(f"--until={args.until}")

    try:
        commits = parse_log(run_git(git_args))
    except RuntimeError as exc:
        print(f"erro_git={exc}", file=sys.stderr)
        sys.exit(1)

    terms = [term.strip() for term in args.terms.split(",") if term.strip()]
    for commit in commits:
        matches = relevance_for(commit, terms)
        commit["relevance_terms"] = ",".join(matches)
        commit["is_relevant"] = "true" if matches else "false"

    write_csv(args.output_csv, commits)

    print("# Git Change Timeline")
    print(f"output_csv={args.output_csv}")
    print(f"commits={len(commits)} | relevantes={sum(1 for row in commits if row['is_relevant'] == 'true')}")
    for commit in commits:
        if not args.all and commit["is_relevant"] != "true":
            continue
        print(
            f"{commit['date']} | {commit['hash'][:10]} | relevant={commit['is_relevant']} | "
            f"terms={commit['relevance_terms'] or '-'} | {commit['subject']} | files={commit['files_changed'] or '-'}"
        )


if __name__ == "__main__":
    main()
