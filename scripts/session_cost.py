#!/usr/bin/env python3
"""
session_cost.py -- Rank your Claude Code sessions by token burn.

token_audit.py tells you what your setup costs on average. This tells you WHICH
sessions actually burned the budget, so you can go look at what you did in them.

Claude Code stores one JSONL transcript per session under:
    ~/.claude/projects/<encoded-project-path>/<session-id>.jsonl
and subagent transcripts under:
    ~/.claude/projects/<encoded-project-path>/<session-id>/subagents/*.jsonl

Usage:
    python3 session_cost.py                        # every project, ranked
    python3 session_cost.py --project my-app       # match project dirs by substring
    python3 session_cost.py --dir ~/.claude/projects/-Users-me-code-my-app
    python3 session_cost.py --top 10 --since 2026-01-01
    python3 session_cost.py --csv > burn.csv
    python3 session_cost.py --self-check

Standard library only.
"""

import argparse
import csv
import glob
import json
import os
import sys

# Cache reads are billed well below fresh input tokens, so a raw token total
# overstates real cost. Totals here are TOKENS MOVED, not dollars. Check current
# API pricing before converting.
USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def scan_session(path, since=None):
    """Sum one transcript's real API usage.

    Assistant messages span multiple JSONL lines that repeat the same
    `message.id` and `usage` object. Dedupe by id or every total is inflated.
    """
    seen = set()
    totals = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0}
    turns = 0
    first_ts = None
    last_ts = None
    models = set()

    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if rec.get("type") != "assistant":
                continue
            message = rec.get("message") or {}
            msg_id = message.get("id")
            if not msg_id or msg_id in seen:
                continue
            timestamp = rec.get("timestamp") or ""
            if since and timestamp[:10] < since:
                continue
            seen.add(msg_id)
            usage = message.get("usage") or {}
            totals["input"] += usage.get("input_tokens") or 0
            totals["cache_creation"] += usage.get("cache_creation_input_tokens") or 0
            totals["cache_read"] += usage.get("cache_read_input_tokens") or 0
            totals["output"] += usage.get("output_tokens") or 0
            turns += 1
            if timestamp:
                first_ts = first_ts or timestamp
                last_ts = timestamp
            if message.get("model"):
                models.add(message["model"])

    if not turns:
        return None
    return {
        "path": path,
        "session": os.path.basename(path)[:-6],
        "project": project_label(path),
        "turns": turns,
        "date": (first_ts or "")[:10],
        "models": ",".join(sorted(models)) or "?",
        "is_subagent": os.sep + "subagents" + os.sep in path,
        "total": sum(totals.values()),
        **totals,
    }


def project_label(path):
    """Recover a readable project name from Claude Code's encoded directory name.

    Claude Code encodes the project path by replacing separators with dashes,
    e.g. /Users/me/code/my-app -> -Users-me-code-my-app. The trailing segment is
    the best available guess at the project's own name.
    """
    parts = os.path.abspath(path).split(os.sep)
    try:
        encoded = parts[parts.index("projects") + 1]
    except (ValueError, IndexError):
        return "?"
    return encoded.rstrip("-").split("-")[-1] or encoded


def collect(roots, since=None, include_subagents=True):
    files = []
    for root in roots:
        files.extend(glob.glob(os.path.join(root, "*.jsonl")))
        if include_subagents:
            files.extend(glob.glob(os.path.join(root, "*", "subagents", "*.jsonl")))
    rows = [r for r in (scan_session(f, since) for f in sorted(set(files))) if r]
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def resolve_roots(args):
    if args.dir:
        path = os.path.abspath(os.path.expanduser(args.dir))
        return [path] if os.path.isdir(path) else []
    projects_root = os.path.join(os.path.abspath(os.path.expanduser(args.claude_dir)), "projects")
    roots = sorted(d for d in glob.glob(os.path.join(projects_root, "*")) if os.path.isdir(d))
    if args.project:
        needle = args.project.lower()
        roots = [d for d in roots if needle in os.path.basename(d).lower()]
    return roots


def print_table(rows, top, show_all_projects):
    if not rows:
        print("No sessions with recorded usage found.")
        return

    shown = rows[:top] if top else rows
    print()
    print(f"{'RANK':<5}{'TOTAL TOK':>14}{'TURNS':>7}{'TOK/TURN':>11}{'OUTPUT':>10}"
          f"{'CACHE READ':>13}  {'DATE':<11}{'SESSION':<10}{'PROJECT'}")
    print("-" * 108)
    for rank, row in enumerate(shown, start=1):
        tag = " [sub]" if row["is_subagent"] else ""
        print(f"{rank:<5}{row['total']:>14,}{row['turns']:>7}"
              f"{row['total'] // max(row['turns'], 1):>11,}{row['output']:>10,}"
              f"{row['cache_read']:>13,}  {row['date']:<11}{row['session'][:8]:<10}"
              f"{row['project']}{tag}")

    grand = sum(r["total"] for r in rows)
    print("-" * 108)
    print(f"  {len(rows)} sessions, {sum(r['turns'] for r in rows):,} API calls, "
          f"{grand:,} tokens moved")
    if grand:
        top_share = sum(r["total"] for r in rows[:max(1, len(rows) // 10)])
        print(f"  top 10% of sessions account for {100 * top_share / grand:.0f}% of all tokens")
        print(f"  cache reads are {100 * sum(r['cache_read'] for r in rows) / grand:.0f}% "
              f"of the total (billed at a discount -- see current API pricing)")
    if show_all_projects:
        totals = {}
        for row in rows:
            totals[row["project"]] = totals.get(row["project"], 0) + row["total"]
        print()
        print("  BY PROJECT")
        for name, value in sorted(totals.items(), key=lambda kv: -kv[1])[:15]:
            print(f"    {name:<28}{value:>16,} tok")


def print_csv(rows):
    writer = csv.DictWriter(sys.stdout, fieldnames=[
        "project", "session", "date", "turns", "total", "input",
        "cache_creation", "cache_read", "output", "models", "is_subagent"])
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row[k] for k in writer.fieldnames})


def self_check():
    """One runnable check: dedupe, totals, and ranking order."""
    import tempfile

    base = {"type": "assistant", "timestamp": "2026-03-04T10:00:00Z",
            "message": {"id": "a", "model": "m", "usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 70, "output_tokens": 5}}}
    second = json.loads(json.dumps(base))
    second["message"]["id"] = "b"

    tmpdir = tempfile.mkdtemp()
    small = os.path.join(tmpdir, "small.jsonl")
    with open(small, "w") as fh:
        for rec in (base, base, base):   # same id three times -> one API call
            fh.write(json.dumps(rec) + "\n")
    big = os.path.join(tmpdir, "big.jsonl")
    with open(big, "w") as fh:
        for rec in (base, second):
            fh.write(json.dumps(rec) + "\n")

    small_row = scan_session(small)
    assert small_row["turns"] == 1, f"dedupe failed: {small_row['turns']}"
    assert small_row["total"] == 105, small_row["total"]

    rows = collect([tmpdir])
    assert [r["turns"] for r in rows] == [2, 1], "rows not sorted by burn"

    assert scan_session(small, since="2027-01-01") is None, "since filter failed"
    assert scan_session(os.path.join(tmpdir, "nope.jsonl")) is None, "missing file must be None"

    for path in (small, big):
        os.unlink(path)
    os.rmdir(tmpdir)
    print("self-check passed")


def main():
    parser = argparse.ArgumentParser(description="Rank Claude Code sessions by token burn.")
    parser.add_argument("--dir", help="a single project transcript directory to scan")
    parser.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"),
                        help="your .claude directory (default: ~/.claude)")
    parser.add_argument("--project", help="only projects whose directory name contains this text")
    parser.add_argument("--top", type=int, default=25, help="rows to show (0 = all, default 25)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="ignore activity before this date")
    parser.add_argument("--no-subagents", action="store_true", help="exclude subagent transcripts")
    parser.add_argument("--csv", action="store_true", help="emit CSV instead of a table")
    parser.add_argument("--self-check", action="store_true", help="run internal assertions and exit")
    args = parser.parse_args()

    if args.self_check:
        self_check()
        return 0

    roots = resolve_roots(args)
    if not roots:
        target = args.dir or args.project or args.claude_dir
        print(f"error: no transcript directories matched: {target}", file=sys.stderr)
        print("hint: sessions live in ~/.claude/projects/<encoded-project-path>/", file=sys.stderr)
        return 1

    rows = collect(roots, args.since, include_subagents=not args.no_subagents)
    if args.csv:
        print_csv(rows)
    else:
        print_table(rows, args.top, show_all_projects=len(roots) > 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
