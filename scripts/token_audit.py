#!/usr/bin/env python3
"""
token_audit.py -- Audit your own Claude Code token overhead.

Reads three things that live on your machine and reports what they cost you:

  1. Static context   -- ~/.claude/CLAUDE.md and ~/.claude/rules/*.md, which are
                         re-sent on every single session.
  2. Project memory   -- ~/.claude/projects/*/memory/
  3. Real API usage   -- ~/.claude/projects/**/*.jsonl session transcripts, which
                         record the actual usage object returned by the API
                         (input / output / cache_creation / cache_read tokens).

Nothing here is guessed where a real number exists. Static file sizes are token
ESTIMATES from character counts; everything drawn from transcripts is a DIRECT
MEASUREMENT of what the API billed.

Usage:
    python3 token_audit.py                  # audit ~/.claude
    python3 token_audit.py --dir /some/path # audit a different .claude dir
    python3 token_audit.py --since 2026-01-01
    python3 token_audit.py --self-check     # run internal assertions and exit

Standard library only. No install step.
"""

import argparse
import glob
import json
import os
import re
import statistics
import sys

# Characters per token. English prose/markdown runs ~3.5-4.0; dense code and
# JSON run lower. 3.5 is the conservative (cost-overstating) end for markdown.
CHARS_PER_TOKEN_MARKDOWN = 3.5
CHARS_PER_TOKEN_CODE = 2.5

# A turn's "effective input" is everything the model had to be given, whether it
# was freshly written to cache, read from cache, or sent uncached.
USAGE_INPUT_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def est_tokens(chars, ratio=CHARS_PER_TOKEN_MARKDOWN):
    return int(chars / ratio)


def percentile(values, p):
    """Nearest-rank percentile. p in 0..1."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p * len(ordered)))]


def read_chars(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return len(fh.read())
    except OSError:
        return 0


def iter_api_calls(path, since=None):
    """Yield one dict per real API call in a transcript.

    Claude Code writes an assistant message across MULTIPLE JSONL lines that all
    repeat the same `message.id` and the same `usage` object. Summing lines
    instead of unique message ids inflates every total several-fold, so dedupe.
    """
    seen = set()
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
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
            yield {
                "input": usage.get("input_tokens") or 0,
                "cache_creation": usage.get("cache_creation_input_tokens") or 0,
                "cache_read": usage.get("cache_read_input_tokens") or 0,
                "output": usage.get("output_tokens") or 0,
                "timestamp": timestamp,
                "model": message.get("model") or "?",
            }


def effective_input(call):
    return call["input"] + call["cache_creation"] + call["cache_read"]


def audit_static(claude_dir, report):
    """CLAUDE.md + rules/ -- the context you pay for before saying anything."""
    claude_md = os.path.join(claude_dir, "CLAUDE.md")
    chars = read_chars(claude_md)
    report.section("STATIC CONTEXT (re-sent every session)")
    if chars:
        report.row("CLAUDE.md", f"{est_tokens(chars):,} tok", f"{chars:,} chars (estimate)")
    else:
        report.row("CLAUDE.md", "not found", claude_md)

    # Recursive: rules in subdirectories (rules/swift/, rules/python/, ...) still load
    # on every session unless you scope them yourself, so they must be counted.
    rule_files = sorted(glob.glob(os.path.join(claude_dir, "rules", "**", "*.md"), recursive=True))
    if rule_files:
        total = 0
        rules_root = os.path.join(claude_dir, "rules")
        for path in rule_files:
            file_chars = read_chars(path)
            total += file_chars
            report.row("  rules/" + os.path.relpath(path, rules_root), f"{est_tokens(file_chars):,} tok", "")
        report.row("rules/ TOTAL", f"{est_tokens(total):,} tok",
                   f"{len(rule_files)} files, {total:,} chars (estimate)")
        report.row("STATIC TOTAL", f"{est_tokens(chars + total):,} tok",
                   "paid on every session, before your first word")
    else:
        report.row("rules/*.md", "none found", os.path.join(claude_dir, "rules"))


def audit_memory(claude_dir, report):
    memory_dirs = sorted(glob.glob(os.path.join(claude_dir, "projects", "*", "memory")))
    report.section("PROJECT MEMORY")
    if not memory_dirs:
        report.row("memory/", "none found", "no ~/.claude/projects/*/memory directories")
        return
    grand_bytes = 0
    grand_files = 0
    for directory in memory_dirs:
        size = 0
        count = 0
        for entry in glob.glob(os.path.join(directory, "**", "*"), recursive=True):
            if os.path.isfile(entry):
                size += os.path.getsize(entry)
                count += 1
        grand_bytes += size
        grand_files += count
    report.row("memory dirs", f"{len(memory_dirs)}", f"{grand_files} files total")
    report.row("memory total", f"{est_tokens(grand_bytes):,} tok",
               f"{grand_bytes:,} bytes (estimate; only the active project's memory loads per session)")


def audit_transcripts(claude_dir, since, report):
    projects_root = os.path.join(claude_dir, "projects")
    main_files = sorted(glob.glob(os.path.join(projects_root, "*", "*.jsonl")))
    sub_files = sorted(glob.glob(os.path.join(projects_root, "*", "*", "subagents", "*.jsonl")))

    report.section("TRANSCRIPTS FOUND")
    report.row("main sessions", f"{len(main_files)}", projects_root)
    report.row("subagent sessions", f"{len(sub_files)}", "stored under <session-id>/subagents/")
    if not main_files:
        report.row("!", "nothing to measure", "no .jsonl transcripts found")
        return

    first_turns = []
    cold_starts = []
    all_calls = []
    sessions = []

    for path in main_files:
        calls = list(iter_api_calls(path, since))
        if not calls:
            continue
        all_calls.extend(calls)
        first = calls[0]
        first_turns.append(effective_input(first))
        # A cold start is a first turn with no cache hit at all: nothing was
        # warm, so cache_creation is the true size of the whole prompt.
        if first["cache_read"] == 0:
            cold_starts.append(effective_input(first))
        sessions.append((sum(effective_input(c) + c["output"] for c in calls), len(calls)))

    report.section("SESSION START OVERHEAD (direct measurement)")
    report.row("first-turn input, median", f"{int(statistics.median(first_turns)):,} tok",
               f"n={len(first_turns)} sessions")
    report.row("  range p25-p75", f"{percentile(first_turns,.25):,} - {percentile(first_turns,.75):,} tok", "")
    report.row("  min / max", f"{min(first_turns):,} / {max(first_turns):,} tok", "")
    if cold_starts:
        report.row("cold start (no cache hit)", f"{int(statistics.median(cold_starts)):,} tok",
                   f"n={len(cold_starts)}; the truest 'before you type anything' number")
    else:
        report.row("cold start", "unmeasurable",
                   "every first turn hit a warm cache; no zero-cache_read sample")

    report.section("PER-TURN COST (direct measurement)")
    reads = [c["cache_read"] for c in all_calls]
    creates = [c["cache_creation"] for c in all_calls]
    outputs = [c["output"] for c in all_calls]
    effective = [effective_input(c) for c in all_calls]
    report.row("API calls sampled", f"{len(all_calls):,}", "deduped by message id")
    report.row("cache_read, median", f"{int(statistics.median(reads)):,} tok",
               f"p90 {percentile(reads,.90):,} / max {max(reads):,}")
    report.row("cache_creation, median", f"{int(statistics.median(creates)):,} tok",
               f"mean {int(statistics.mean(creates)):,}")
    report.row("effective input, median", f"{int(statistics.median(effective)):,} tok",
               "input + cache_creation + cache_read")
    report.row("output, median", f"{int(statistics.median(outputs)):,} tok",
               f"mean {int(statistics.mean(outputs)):,} / max {max(outputs):,}")

    ratios = [c["output"] / effective_input(c) for c in all_calls if effective_input(c) > 0]
    if ratios:
        median_ratio = statistics.median(ratios)
        report.row("output : input, median", f"1 : {round(1 / median_ratio):,}",
                   "you pay to re-read the conversation, not to generate")
    total_in = sum(effective)
    total_out = sum(outputs)
    if total_out:
        report.row("output : input, aggregate", f"1 : {round(total_in / total_out):,}",
                   f"{total_in:,} in vs {total_out:,} out")
    # Cache health: reads are the cheap path, creations the expensive one. A high ratio
    # means the prefix is being reused; near 1:1 means something invalidates it each turn.
    if sum(creates):
        report.row("cache read : write", f"{sum(reads) / sum(creates):.1f} : 1",
                   "higher is better; near 1:1 means the cache keeps breaking")

    report.section("CONTEXT GROWTH (median effective input by turn number)")
    buckets = {}
    for path in main_files:
        for index, call in enumerate(iter_api_calls(path, since), start=1):
            buckets.setdefault(min(index, 60), []).append(effective_input(call))
    for turn in (1, 5, 10, 20, 30, 40, 50, 60):
        values = buckets.get(turn)
        if values:
            label = f"turn {turn}+" if turn == 60 else f"turn {turn}"
            report.row(label, f"{int(statistics.median(values)):,} tok", f"n={len(values)}")

    report.section("WHAT FILLS THE CONTEXT")
    composition = measure_composition(main_files, since)
    total_chars = sum(composition.values()) or 1
    for key in ("tool_result", "user_text", "assistant_text"):
        chars = composition.get(key, 0)
        report.row(key, f"{100 * chars / total_chars:.1f}%",
                   f"{chars:,} chars (~{est_tokens(chars, CHARS_PER_TOKEN_CODE):,} tok, code ratio)")

    report.section("COST PER TOOL (result size -- what each call adds to context)")
    # Subagents included: their tool results cost real tokens too, just in another thread.
    per_tool = measure_tool_results(main_files + sub_files, since)
    if per_tool:
        report.row("tool", "calls / median chars", "~tokens at code ratio")
        ranked = sorted(per_tool.items(), key=lambda kv: -len(kv[1]))
        for name, values in ranked[:12]:
            median_chars = int(statistics.median(values))
            report.row(f"  {name[:28]}", f"{len(values)} / {median_chars:,}",
                       f"~{est_tokens(median_chars, CHARS_PER_TOKEN_CODE):,} tok median, "
                       f"max {max(values):,} chars")
        mcp_values = [v for n, vs in per_tool.items() if n.startswith("mcp__") for v in vs]
        if mcp_values:
            report.row("  all mcp__* combined", f"{len(mcp_values)} / {int(statistics.median(mcp_values)):,}",
                       f"~{est_tokens(int(statistics.median(mcp_values)), CHARS_PER_TOKEN_CODE):,} tok median")

    report.section("COMPACTION")
    pre_tokens = measure_compactions(main_files)
    if pre_tokens:
        report.row("compactions", f"{len(pre_tokens)}", "auto-compact events recorded")
        report.row("context at trigger, median", f"{int(statistics.median(pre_tokens)):,} tok",
                   f"range {min(pre_tokens):,} - {max(pre_tokens):,}")
    else:
        report.row("compactions", "none recorded", "no compact_boundary records found")

    report.section("SESSION BURN (total tokens per session, incl. cache reads)")
    sessions.sort()
    totals = [s[0] for s in sessions]
    report.row("sessions measured", f"{len(sessions)}", "")
    report.row("median session", f"{int(statistics.median(totals)):,} tok", "")
    for label, index in (("small (p10)", int(.10 * len(sessions))),
                         ("median (p50)", len(sessions) // 2),
                         ("large (p90)", int(.90 * len(sessions))),
                         ("largest", len(sessions) - 1)):
        total, turns = sessions[min(index, len(sessions) - 1)]
        report.row(f"  {label}", f"{total:,} tok", f"{turns} turns, {total // max(turns,1):,} tok/turn")
    report.row("ALL SESSIONS", f"{sum(totals):,} tok", "sum of every session measured")


def measure_composition(files, since=None):
    """Character volume of tool results vs human text vs assistant prose.

    Counts transcript BODY content, which is what accumulates in context as a
    session runs. Returns raw character counts.
    """
    counts = {"tool_result": 0, "user_text": 0, "assistant_text": 0}
    for path in files:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if since and (rec.get("timestamp") or "")[:10] < since:
                    continue
                message = rec.get("message") or {}
                content = message.get("content")
                if rec.get("type") == "assistant":
                    for block in content or []:
                        if isinstance(block, dict) and block.get("type") == "text":
                            counts["assistant_text"] += len(block.get("text") or "")
                elif rec.get("type") == "user":
                    if isinstance(content, str):
                        counts["user_text"] += len(content)
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_result":
                                counts["tool_result"] += len(
                                    json.dumps(block.get("content"), ensure_ascii=False))
                            elif block.get("type") == "text":
                                counts["user_text"] += len(block.get("text") or "")
    return counts


def measure_tool_results(files, since=None):
    """Result size per tool, in characters.

    A tool_result carries no usage of its own -- it becomes part of the NEXT turn's input.
    Mapping each result back to the tool that produced it requires matching `tool_use_id`
    against the `tool_use` block that requested it.
    """
    per_tool = {}
    for path in files:
        id_to_name = {}
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if since and (rec.get("timestamp") or "")[:10] < since:
                    continue
                message = rec.get("message") or {}
                if rec.get("type") == "assistant":
                    for block in message.get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            id_to_name[block.get("id")] = block.get("name") or "?"
                elif rec.get("type") == "user":
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_result":
                            continue
                        name = id_to_name.get(block.get("tool_use_id"), "?")
                        size = len(json.dumps(block.get("content"), ensure_ascii=False))
                        per_tool.setdefault(name, []).append(size)
    return per_tool


def measure_compactions(files):
    """Context size at each auto-compaction, from Claude Code's own compact_boundary record."""
    pre_tokens = []
    for path in files:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if rec.get("subtype") == "compact_boundary":
                    value = (rec.get("compactMetadata") or {}).get("preTokens")
                    if value:
                        pre_tokens.append(value)
    return pre_tokens


def audit_skills(claude_dir, report):
    """Skill roster cost: every skill's name + description is loaded every session.

    Skill BODIES load only when a skill is invoked, so they are per-use cost. The roster is
    the part you pay for unconditionally.
    """
    paths = set(glob.glob(os.path.join(claude_dir, "skills", "**", "SKILL.md"), recursive=True))
    paths |= set(glob.glob(os.path.join(claude_dir, "plugins", "**", "SKILL.md"), recursive=True))
    # Installed plugins keep one directory per cached version; counting them all would
    # multiply every skill by however many versions happen to be on disk.
    live = [p for p in paths if os.sep + "cache" + os.sep not in p]

    report.section("SKILLS")
    if not live:
        report.row("SKILL.md", "none found", os.path.join(claude_dir, "skills"))
        return

    seen_names = {}
    for path in sorted(live):
        try:
            head = open(path, encoding="utf-8", errors="replace").read(4000)
        except OSError:
            continue
        match = re.match(r"^---\s*\n(.*?)\n---", head, re.S)
        if not match:
            continue
        block = match.group(1)
        name = re.search(r"^name:\s*(.+)$", block, re.M)
        desc = re.search(r"^description:\s*(.+)$", block, re.M)
        if name and desc and name.group(1).strip() not in seen_names:
            seen_names[name.group(1).strip()] = (desc.group(1).strip(), os.path.getsize(path))

    if not seen_names:
        report.row("SKILL.md", f"{len(live)} files", "no parseable frontmatter")
        return

    roster_chars = sum(len(f"- {n}: {d}\n") for n, (d, _) in seen_names.items())
    bodies = [size for _, (_, size) in seen_names.items()]
    report.row("registered skills", f"{len(seen_names)}", f"{len(live)} SKILL.md files on disk")
    report.row("roster (names + descs)", f"{est_tokens(roster_chars):,} tok",
               f"{roster_chars:,} chars -- paid EVERY session (estimate)")
    report.row("skill body, median", f"{est_tokens(int(statistics.median(bodies))):,} tok",
               "loaded only when that skill is invoked")


class Report:
    """Plain aligned text output. No dependencies, pipes cleanly to a file."""

    WIDTH = 78

    def section(self, title):
        print()
        print(title)
        print("-" * self.WIDTH)

    def row(self, label, value, note=""):
        print(f"  {label:<30} {value:>22}   {note}")

    def header(self, claude_dir):
        print("=" * self.WIDTH)
        print("  CLAUDE CODE TOKEN AUDIT")
        print(f"  source: {claude_dir}")
        print("=" * self.WIDTH)

    def footer(self):
        print()
        print("-" * self.WIDTH)
        print("  'tok' on static files is an ESTIMATE from character count.")
        print("  Everything from transcripts is a DIRECT MEASUREMENT of billed usage.")
        print("  cache_read tokens are billed at a discount -- check current API pricing.")
        print("-" * self.WIDTH)


def self_check():
    """One runnable check: the dedupe and math that everything else depends on."""
    import tempfile

    # Two JSONL lines sharing one message id must count as ONE API call.
    duplicated = {
        "type": "assistant", "timestamp": "2026-01-02T00:00:00Z",
        "message": {"id": "msg_1", "model": "m", "usage": {
            "input_tokens": 5, "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 900, "output_tokens": 50}},
    }
    other = json.loads(json.dumps(duplicated))
    other["message"]["id"] = "msg_2"

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        for record in (duplicated, duplicated, duplicated, other):
            fh.write(json.dumps(record) + "\n")
        temp_path = fh.name

    calls = list(iter_api_calls(temp_path))
    assert len(calls) == 2, f"dedupe failed: got {len(calls)} calls, expected 2"
    assert effective_input(calls[0]) == 1005, effective_input(calls[0])

    # A `since` filter drops older records.
    assert len(list(iter_api_calls(temp_path, since="2026-06-01"))) == 0

    # Percentiles stay in range and stay ordered.
    sample = list(range(100))
    assert percentile(sample, 0.5) == 50
    assert percentile(sample, 1.0) == 99
    assert percentile([], 0.5) == 0

    # Composition counts tool results separately from prose.
    counts = measure_composition([temp_path])
    assert counts["tool_result"] == 0

    os.unlink(temp_path)

    # A tool_result must be attributed to the tool that produced it, via tool_use_id.
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-01-02T00:00:00Z",
                             "message": {"id": "m1", "usage": {}, "content": [
                                 {"type": "tool_use", "id": "tu_1", "name": "Read"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "timestamp": "2026-01-02T00:00:01Z",
                             "message": {"content": [
                                 {"type": "tool_result", "tool_use_id": "tu_1",
                                  "content": "0123456789"}]}}) + "\n")
        fh.write(json.dumps({"type": "system", "subtype": "compact_boundary",
                             "compactMetadata": {"preTokens": 199999}}) + "\n")
        tools_path = fh.name

    per_tool = measure_tool_results([tools_path])
    assert list(per_tool) == ["Read"], per_tool
    assert measure_compactions([tools_path]) == [199999]
    os.unlink(tools_path)

    print("self-check passed")


def main():
    parser = argparse.ArgumentParser(description="Audit your Claude Code token overhead.")
    parser.add_argument("--dir", default=os.path.expanduser("~/.claude"),
                        help="path to your .claude directory (default: ~/.claude)")
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="ignore transcript activity before this date")
    parser.add_argument("--self-check", action="store_true", help="run internal assertions and exit")
    args = parser.parse_args()

    if args.self_check:
        self_check()
        return 0

    claude_dir = os.path.abspath(os.path.expanduser(args.dir))
    if not os.path.isdir(claude_dir):
        print(f"error: no such directory: {claude_dir}", file=sys.stderr)
        print("hint: pass --dir if your Claude Code config lives elsewhere.", file=sys.stderr)
        return 1

    report = Report()
    report.header(claude_dir)
    audit_static(claude_dir, report)
    audit_skills(claude_dir, report)
    audit_memory(claude_dir, report)
    audit_transcripts(claude_dir, args.since, report)
    report.footer()
    return 0


if __name__ == "__main__":
    sys.exit(main())
