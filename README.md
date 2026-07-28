# What Every Claude Code Operation Actually Costs

Everyone writing about Claude Code cost repeats the same sentence: "overhead is baked into every prompt." Nobody publishes the numbers. So you tune by superstition — you delete a paragraph from `CLAUDE.md`, you disconnect an MCP server, you feel virtuous, and you have no idea whether you saved 200 tokens or 20,000. Meanwhile the thing actually draining your week might be a test runner dumping 4,000 lines into context twice an hour, and you have never once suspected it.

This is the measured ledger. Free, in full, no email required.

## Methodology

Every figure comes from one real, heavily configured machine: **185 session transcripts and 3,400 API calls, deduplicated by `message.id`**, using the token counts the API itself returns on each response (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`). Nothing here is estimated from pricing pages or vendor documentation.

Three grades of number appear, and they are not equal:

- **Direct** — the API's billed counts, pulled from transcripts. Trust these.
- **Estimate** — file sizes converted at roughly 3.5 characters per token, because no tokenizer was installed. Off by 10–15%. Marked with a tilde (`~`).
- **Extrapolation** — one directly measured server or file scaled to a larger set. Named as such in the row.

Where something could not be measured, the row says so instead of guessing. There are three of those, and they are the most useful rows in the table.

One caveat about a source you will find on your own: Anthropic's context-window documentation includes an interactive visualization with per-component token figures. That page states outright that it "uses representative numbers." They are illustrative, not measurements. Do not quote them as data, and be suspicious of any guide that does.

## The ledger

| Operation | Measured tokens | When you pay it |
|---|---|---|
| System prompt + built-in tool definitions | 69,978 (direct, full startup floor) | Recurring, every turn |
| Environment + git status block | ~56 (estimate) | Recurring |
| `~/.claude/CLAUDE.md` (user scope) | ~3,124 (estimate, 201 lines) | Recurring, every session, every project |
| `~/.claude/rules/*.md` (20 files, unscoped) | ~11,142 (estimate) | Recurring, every session, regardless of task |
| Project `./CLAUDE.md` | ~2,416 (estimate, median of 16) | Recurring |
| Auto memory `MEMORY.md` index | ~105 (estimate, median) | Recurring |
| Skill descriptions — the roster, not bodies (192 skills) | ~13,263 (estimate) | Recurring |
| MCP tool names, deferred (the default, 255 tools / 8 servers) | ~3,623 (direct name measurement, extrapolated by count) | Recurring, small |
| Same MCP tools with schemas loaded upfront | ~36,975 (extrapolation — worst case, deferral off) | Recurring, large |
| MCP server instructions | 0 (direct, for the one locally-inspectable server) | Recurring |
| *Subtotal: plugin-supplied overhead* | *~16,886 — skill roster + MCP names combined, 24.1% of the startup floor. **Not additive with the two rows above.*** | *Recurring* |
| One file read | ~3,483 (direct chars, median, n=498; mean ~47,152) | One-time, then cached |
| One search result (Bash proxy) | ~144 (direct chars, median, n=774) | One-time |
| Unfiltered test output | ~2,131 (direct, single live run) | One-time, repeated every run |
| Same test output, hook-filtered to failures | ~363 (direct, same command) — an 83.0% reduction | One-time |
| One invoked skill body | ~1,614 (estimate, median) | One-time, on invocation only |
| Extended thinking, high effort | **Not separable.** Modern transcripts store thinking blocks with content stripped, and the API's usage object has no reasoning counter | Per turn, billed as output |
| One subagent: cost inside its own window | 67,670 (direct, n=331) | One-time, in the subagent |
| One subagent: what returns to your context | ~559 (direct chars, median, n=32) | One-time, then recurring |
| One agent team teammate | ~67,670 (direct) | For its entire lifetime |
| One MCP tool result | ~547 (direct chars, median, n=32) — ~3.8x a CLI equivalent | One-time |
| One `ToolSearch` call | ~31 (direct, n=50) — returns pointers, not schemas | One-time |
| `/compact` | 207,589 (direct — median context size at the trigger, n=8) | One-time, at your choosing |
| `/clear` | 0 | Never |
| Cache read, healthy session | ~10% of the input rate. Measured read-to-write ratio: **15.5 : 1** | Every turn |
| Cache miss after a break | ~70,000 (direct, 2 observed cold starts) | On the cliff |
| Background jobs while idle | under $0.04 per session | Continuous, trivial |

## Three rows worth reading twice

**The startup floor was 69,978 tokens.** That is 35% of a 200K context window consumed before the first character is typed. Of it, ~11,142 was twenty global rules files that loaded on every project on the machine regardless of language or task — a rules file about database indexing loading while editing CSS. Config changes alone removed 30,857 of those tokens, a **44.1% cut to the startup floor.**

**Unfiltered test output at ~2,131 tokens, three or four times an hour, dwarfs the `CLAUDE.md` you spent an evening trimming.** The same command filtered to failures costs ~363. This is the highest-leverage single change on the list and almost nobody makes it.

**A subagent is relocated work, not free work.** It costs 67,670 tokens in its own window — including your entire `CLAUDE.md` hierarchy, again — and returns ~559 to yours. For verbose throwaway work that trade wins decisively. For a two-file edit it loses badly. If your global config is heavy, delegation multiplies the problem rather than dodging it.

## The honest gaps

Three things in the table could not be measured, and every tool reading the same transcripts has the same blind spots:

- **Extended thinking tokens.** Real, billed as output, and unrecoverable from transcripts.
- **The Grep tool.** Invoked zero times in the entire corpus, because this setup shells out to Bash. The search figure is a Bash proxy, not a Grep measurement.
- **Built-in tool definitions in isolation.** Never written to disk. Only an upper bound exists.

Two figures carry known confounds: the weekly totals compare two observed periods rather than a controlled A/B, and the largest startup reduction on this machine spans a Claude Code version change as well as a config change. The 44.1% above is the config-only figure, which is why it is the one quoted.

## Reproduce it yourself

Don't trust one machine's numbers, including this one. `/context` reports your recurring block. `/usage`, then `w`, attributes recent usage to skills, subagents, plugins, and individual MCP servers, flagging anything at 10% or more. Watch `cache_creation_input_tokens` against `cache_read_input_tokens` on every response — if creation stays high turn after turn, something in your prefix keeps changing.

---

**From *The Token Ledger*, Edition 1** — the how-to-shrink-it column and 69 optimizations are in the book: https://basementdante.github.io/token-ledger/

*The Token Ledger is an independent publication. It is not affiliated with, authorized by, or endorsed by Anthropic. Mechanics were verified against official Claude Code documentation at the time of writing; all measured figures come from instrumented sessions and are reproducible with the measurement scripts included with the book.*
