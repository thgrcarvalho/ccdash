# Runbook: lower token spend for Claude Code behind a gateway to Amazon Bedrock

A step-by-step procedure meant to be **executed by a Claude Code session** on the machine to configure. It applies the fixes that the ccdash dashboard usually points at when Claude Code runs through a corporate gateway (LiteLLM, Portkey, Kong, etc.) to Bedrock:

1. a 1-hour prompt-cache lifetime for the main conversation (the gateway default is 5 minutes, so every pause longer than that rewrites the whole context),
2. the `haiku` alias pointed at the newest Haiku the gateway offers (Claude Code's own Bedrock default lags a generation behind, and the older Haiku costs about ten times more below 100k tokens, as of 2026-10),
3. an `Explore` subagent and a log-search subagent running on that Haiku with a 100k context ceiling (the Haiku price-tier boundary as of 2026-10; check the pricing page), so exploration and log triage stay out of the expensive Sonnet session,
4. optional per-model auto-compact windows and output caps.

**How to use it.** On the target machine, with the ccdash repo cloned:

```bash
cd ~/dev/repos/ccdash && git pull
claude "Read docs/setup-runbook.md and execute it step by step. Follow its rules section exactly."
```

Test the 1-hour cache manually first (`CLAUDE_CODE_PROMPT_CACHE_TTL=1h claude`, two prompts, then check the transcript as described in Step 5). This runbook assumes that test passed.

---

## Rules for the executing session

- **Scope.** Change only `~/.claude/settings.json` and files under `~/.claude/agents/`. Nothing else: no project files, no shell profiles, no git operations.
- **Back up and merge.** Copy `~/.claude/settings.json` to `~/.claude/settings.json.bak-<YYYYMMDD-HHMM>` before editing. Merge keys into the existing JSON; never replace the file. Show the final file at the end.
- **Secrets.** Never print the values of `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `AWS_*` or anything that looks like a token. Mask them in every command output you show.
- **Existing files.** If an agent file you are about to write already exists, show the diff and ask before overwriting.
- **Asking.** Steps 0 to 6 proceed without asking unless a step says otherwise. Every item in Step 7 needs an explicit yes from the user.
- **Nested runs.** Some steps run `claude -p ...` for verification. If the CLI refuses to start inside this session, give the user the exact command to run in another terminal and wait for the pasted output.
- **Do not** run `/model`, `/effort` or `/autocompact` in this session; they would change this session's state. The runbook tells the user when to run those themselves.

---

## Step 0. Collect facts (no changes)

Run and record:

```bash
claude --version
env | grep -E 'ANTHROPIC|CLAUDE_CODE|BEDROCK' | sed -E 's/(TOKEN|KEY|SECRET)=.*/\1=***/'
cat ~/.claude/settings.json 2>/dev/null || echo "no settings.json"
ls -la ~/.claude/agents 2>/dev/null || echo "no agents dir"
claude mcp list
```

Note:

- `VERSION`. Requirements: `CLAUDE_CODE_PROMPT_CACHE_TTL` needs 2.1.242 or newer; `autoCompactWindow` in agent frontmatter needs 2.1.296 or newer; `modelSettings` / `/autocompact` need 2.1.288 or newer. If the version is older than one of these, skip the dependent line and tell the user.
- `CONNECTION`: `ANTHROPIC_BASE_URL` set means an Anthropic-format gateway; `CLAUDE_CODE_USE_BEDROCK=1` means the Bedrock-native path. Either is fine for this runbook.
- `SONNET_ID`: the value of `ANTHROPIC_MODEL` (or the `model` key in settings). It shows the naming pattern the gateway expects, for example `us.anthropic.claude-sonnet-5-5` or a plain alias configured in the gateway.
- `MCP_SERVER`: the name of the MCP gateway server in `claude mcp list` (the one whose tools look like `mcp__<name>__search_tools`, `mcp__<name>__call_tool`). If there is no such server, skip Step 4 and say so.

## Step 1. Find the newest Haiku model ID the gateway offers

Nothing in this runbook names a Haiku version on purpose: the agent files use the `haiku` alias, and this step picks whatever is newest at the time it runs.

1. Ask the gateway for its model list. Try both header styles; one of them matches how the key is configured:

```bash
curl -s "$ANTHROPIC_BASE_URL/v1/models" -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" | jq -r '.data[].id' | grep -i haiku
curl -s "$ANTHROPIC_BASE_URL/v1/models" -H "x-api-key: $ANTHROPIC_API_KEY" | jq -r '.data[].id' | grep -i haiku
```

2. Choose `HAIKU_ID`:
   - **Prefer a version-less alias** if the gateway has one (an entry containing `haiku` with no version digits, or ending in `latest`). The platform team moves it forward when a new Haiku ships, so the setting never goes stale.
   - Otherwise pick the **highest version number** among the Haiku entries (compare the digits after `haiku`, for example `5-5` beats `4-5`).
   - If `ANTHROPIC_DEFAULT_HAIKU_MODEL` is already set, keep it only if it is that same newest entry.
   - If the list is empty, ambiguous, or the endpoint is not exposed, **ask the user** for the ID (their platform team knows it). Do not guess.
3. Verify it answers:

```bash
claude --model "<HAIKU_ID>" -p "Reply with the single word OK" --output-format json | jq '{result, modelUsage}'
```

Exit code 0 and an `OK` result are enough. If `modelUsage` is present, its key must be the chosen ID.

## Step 2. Settings: cache lifetime and Haiku alias

Merge these two keys into the `env` object of `~/.claude/settings.json` (create the file or the object if missing), using Python so the rest of the file is preserved:

```bash
python3 - "<HAIKU_ID>" <<'EOF'
import json, os, sys, shutil, datetime
p = os.path.expanduser("~/.claude/settings.json")
haiku = sys.argv[1]
cfg = {}
if os.path.exists(p):
    shutil.copy(p, p + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M"))
    with open(p) as fh:
        cfg = json.load(fh)
env = cfg.setdefault("env", {})
env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = "1h"
env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = haiku
with open(p, "w") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
print(open(p).read())
EOF
```

What they do: the first keeps the main conversation's prompt cache alive for 1 hour instead of 5 minutes (cache writes cost 2x input instead of 1.25x; pauses under an hour no longer rewrite the context). The second makes `haiku` mean the chosen model everywhere, including background tasks such as session titles. It is the only value in this setup that names a concrete model: when the gateway adds a newer Haiku, re-run Step 1 and update it, or avoid even that by using a version-less gateway alias.

## Step 3. The Explore agent on Haiku

Write `~/.claude/agents/Explore.md` (a user agent named `Explore` replaces the built-in one and keeps its own model). Remove the `autoCompactWindow` line if `VERSION` is older than 2.1.296.

```markdown
---
name: Explore
description: Fast read-only codebase exploration. Use for "where is X defined", "which files reference Y", "how does Z flow" questions.
model: haiku
effort: low
tools: Read, Grep, Glob, Bash
maxTurns: 25
autoCompactWindow: 100000
---
You locate code; you never modify it. Grep and glob first, then read only the line ranges you need, never whole files over 300 lines. Stop when you have the answer or after 25 tool calls. Report in under 200 words: the files and line numbers that answer the question, one sentence each, and anything you could not find. No code dumps.
```

## Step 4. The log-search agent on Haiku (Splunk or similar, through the MCP gateway)

Write `~/.claude/agents/logsearch.md`, replacing `<MCP_SERVER>` with the name from Step 0. Remove the `autoCompactWindow` line on versions older than 2.1.296.

```markdown
---
name: logsearch
description: Runs log searches (Splunk) through the <MCP_SERVER> MCP and returns only the findings. Use for log and error investigations.
model: haiku
effort: low
tools: mcp__<MCP_SERVER>__*
maxTurns: 15
autoCompactWindow: 100000
omitClaudeMd: true
---
You query the log platform for the caller through the <MCP_SERVER> tools. Activate the log server if the gateway requires it, then call its search tool directly; use the gateway's tool search only if the direct call fails. Keep every query narrow: explicit time range, index or source, service, and a result limit of 50 or less. Report in under 200 words: the query you ran, the hit count, the 3 to 5 most relevant log lines with timestamps, and your one-sentence reading. Never paste raw result dumps.
```

Why these fields: `model: haiku` is an alias, so the files never need a version; `effort: low` keeps thinking small (it cannot be turned off on current Haiku models); `maxTurns` is the real budget; `autoCompactWindow: 100000` keeps requests in Haiku's cheap tier (as of 2026-10 prices rise 5x above a 100k-token prompt; verify the boundary on the pricing page when a new Haiku ships); `omitClaudeMd` keeps the repo instructions out of a context that does not need them; and the `tools` pattern limits the agent to the gateway's tools.

## Step 5. Verify

1. **Cache lifetime from settings, with the shell variable unset for this one call:**

```bash
env -u CLAUDE_CODE_PROMPT_CACHE_TTL claude -p "Say OK" --output-format json | jq '.usage.cache_creation'
```

The tokens must appear under `ephemeral_1h_input_tokens`. If they appear under `ephemeral_5m_input_tokens`, a managed settings file (pushed by the platform team) is probably overriding user settings; report that to the user. If `cache_creation` is absent from the JSON, fall back to reading the newest transcript:

```bash
python3 - <<'EOF'
import glob, json, os
f = max(glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl')), key=os.path.getmtime)
seen = set()
for line in open(f, errors='replace'):
    try: d = json.loads(line)
    except Exception: continue
    m = d.get('message') or {}
    u = m.get('usage') if d.get('type') == 'assistant' else None
    if not u or m.get('id') in seen: continue
    seen.add(m.get('id'))
    cc = u.get('cache_creation') or {}
    print(f"write_1h={cc.get('ephemeral_1h_input_tokens', 0)} write_5m={cc.get('ephemeral_5m_input_tokens', 0)} cache_read={u.get('cache_read_input_tokens', 0)} model={m.get('model')}")
EOF
```

2. **Haiku alias:**

```bash
claude --model haiku -p "Reply with the single word OK" --output-format json | jq '{result, modelUsage}'
```

The model in `modelUsage` (or in the newest transcript, same script as above) must be the ID chosen in Step 1, not the older Haiku.

3. **Agents present:** `ls -la ~/.claude/agents` shows `Explore.md` and `logsearch.md`. Optional smoke test from inside any repository:

```bash
claude -p "Use the Explore agent to find the file containing this project's main entry point; reply with the path only" --output-format json | jq -r '.result'
```

## Step 6. Tell the user the two things only they can do

1. **Per-model ceilings for Haiku sessions.** Start `claude --model haiku`, run `/autocompact 100k` and `/effort low`, then exit. Both save under `modelSettings` for that model only, so interactive Haiku sessions stay under 100k and at low effort without affecting Sonnet.
2. **Sonnet window, if they want it.** Start a normal Sonnet session and run `/autocompact 250k`. This is also saved per model. **Do not** set `CLAUDE_CODE_AUTO_COMPACT_WINDOW` as an env var for this: the env var wins over every per-model value, including the Haiku one from item 1.

Remind them: the effort default for the current Sonnet in Claude Code is `medium` (as of 2026-10; `/effort` shows the active level), so no effort setting is needed for Sonnet. And `model: haiku` in the two agent files resolves to the chosen Haiku because of Step 2; if they ever remove that setting, replace the alias in the files with the full ID.

## Step 7. Optional, one explicit yes per item

Ask about each separately; apply only what is approved, into the same `env` or `permissions` objects of `~/.claude/settings.json`, same merge method as Step 2.

| Item | Setting | Effect | Trade-off |
|---|---|---|---|
| Cap Bash output | `"env": {"BASH_MAX_OUTPUT_LENGTH": "20000"}` | A noisy test or build run adds at most ~5k tokens to the context instead of ~8k | Long outputs are truncated; the full output is still written to a file Claude can read |
| Keep build output out | `"permissions": {"deny": ["Read(**/target/**)", "Read(**/build/**)", "Read(**/*.log)"]}` | Claude cannot read compiled or generated files and local logs | Blocks reading any directory named `build`, and local log files the user might want triaged |
| Compact instructions | A `# Compact instructions` section in each work repo's `CLAUDE.md`: "When compacting, keep: ticket goal, changed files, failing test names, decisions, next steps. Drop tool output and exploration." | Compaction summaries keep what matters on multi-task tickets | Project files, so the user adds them per repo; show the text and stop |

## Step 8. Report

End with: the backup path, the final `~/.claude/settings.json`, the two agent files' paths, the verification results (cache lifetime observed, Haiku model observed), the two manual follow-ups from Step 6, and a reminder to re-run `python3 ccdash.py --since 7` after a week. The dashboard's "observed" lifetime should read 1-hour, the 5-to-60-minute idle rewrites should drop to near zero, and `mcp__*` and exploration results should start appearing under the subagents rather than the main sessions.
