# ccdash

A local dashboard of **where Claude Code spends your tokens**: per day, per model, per project, per session, per tool and per MCP server, with the cache behaviour that explains why one day costs five times another.

It reads the JSONL transcripts Claude Code already keeps under `~/.claude/projects`, so it works the same on a claude.ai subscription, an API key, or Claude Code behind a corporate gateway to Amazon Bedrock. Nothing is installed, nothing leaves the machine, and the transcripts are only read.

Requirements: Python 3.9+ (standard library only) and a browser.

## Run

```bash
git clone <this repo> && cd ccdash
python3 ccdash.py --since 30 --open       # last 30 days -> ./ccdash.html, opened in the browser
python3 ccdash.py --since 7
python3 ccdash.py --since 0 --out all.html
python3 ccdash.py --json data.json        # the aggregated numbers, no HTML
```

Re-run whenever you want fresh numbers; it takes a few seconds for a few thousand transcripts.

## Configure for your machine

Copy `config.example.json` to `config.json` (picked up automatically) and set:

- `ttl_minutes`: the prompt-cache lifetime of your main conversation. 60 on a claude.ai subscription within plan usage, **5 on an API key, Bedrock, Vertex or a gateway** unless `CLAUDE_CODE_PROMPT_CACHE_TTL=1h` is set. It decides whether a big cache rewrite is classified as "idle" (you paused longer than the lifetime) or "breaker" (something changed the prompt prefix).
- `price_multiplier`: 1.10 for Bedrock regional inference profiles (`us.`, `eu.`), 1.0 otherwise.
- `aliases`: the model names a gateway such as LiteLLM reports, mapped to real model ids. Unknown names are priced as Sonnet 5 and listed in the output so you can add them.
- `prices`: override or add rates (USD per million tokens) to match a contract.

## What the dashboard shows

| Section | Numbers | Source |
|---|---|---|
| KPIs | estimated cost, tokens, cache hit rate, cost of idle rewrites, output share, thinking share | API `usage` fields (real) |
| Per day | stacked cost (or tokens) by cache read / cache write / output / uncached input; max/median ratio | real |
| By model, by project | cost, requests, cache hit | real |
| Cache rewrites | large cache writes split into first request / idle gap / prefix breaker, with cost | real, classified with `ttl_minutes` |
| When | cost by hour of day; off-hours cost reveals loops and scheduled tasks | real |
| By tool | per tool and per MCP server: direct tokens, **carried** tokens (re-read on every later request), estimated cost, biggest single results | estimate from characters, with a calibration factor against the real counts |
| Sessions | top sessions with a **why** label (big context, idle rewrites, prefix breakers, many requests, low cache hit, expensive model, subagent fan-out), peak context, cache hit, rewrite counts, subagents, images; click for the context growth curve | real |

"Carried" is the number people miss: a 40k-token tool result in a session that runs 200 more requests is 8 million tokens of cache reads. That is why Bash output and big file reads dominate even when each result looks small.

## Reading it

- **Cache hit below ~90% on long sessions**: either you pause longer than the cache lifetime between turns (idle rewrites), you switch model or effort mid-session, MCP servers connect mid-session, or, behind a gateway, `cache_control` is not being forwarded. Zero cache reads everywhere means the gateway strips caching.
- **Idle rewrites high**: finish and `/clear` instead of leaving a big session idle; on Bedrock consider `CLAUDE_CODE_PROMPT_CACHE_TTL=1h`.
- **Peak context in the hundreds of thousands**: lower the auto-compact window (`CLAUDE_CODE_AUTO_COMPACT_WINDOW=300000`) or clear between tasks.
- **Bash or Read at the top of the tool table**: cap `BASH_MAX_OUTPUT_LENGTH`, filter test output, deny reads of build directories.
- **An MCP server high in the table**: its results are large; check `/context` for its schema cost too, and disable it in projects that do not need it.

## Caveats

- Dollar figures are list prices applied to real token counts. On a subscription they are "what this would cost on the API"; through a gateway they approximate the bill only as well as the config matches your contract.
- Per-tool tokens are estimates (characters ÷ 3.5). The calibration line in the methodology section compares the estimate with the API's real per-request counts.
- MCP tool **schema** cost (what a server adds to every request just by being connected) is not in the transcripts. Run `/context` inside a session for that.
- Results that Claude Code wrote to a file instead of the context (very large outputs) are counted at their preview size.
- The transcript format is internal to Claude Code and may change.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
