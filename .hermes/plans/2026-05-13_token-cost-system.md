# Token Cost System — Master Plan
**Goal:** Build a modular, multi-tool AI token-cost-reduction and observability system that any AI coding tool can plug into. Inspired by the article you shared + caveman (output compression) + codeburn (observability).

---

## What We're Building

Three independent modules that compose into one system:

```
┌─────────────────────────────────────────────────────────────────┐
│                     TOKEN COST SYSTEM                           │
├────────────────┬────────────────────┬───────────────────────────┤
│  Module 1      │  Module 2          │  Module 3                 │
│  OBSERVER      │  ROUTER            │  COMPRESSOR               │
│  (codeburn     │  (model routing    │  (caveman-style           │
│   inspired)    │   config system)   │   output compression)     │
│                │                    │                           │
│  - What am I   │  - Which model     │  - Cut output tokens      │
│    spending?   │    for this task?  │    75%                    │
│  - Per-tool    │  - Plug routing    │  - Works on any tool      │
│    breakdown   │    config into     │  - SKILL.md pattern       │
│  - Waste audit │    any AI tool     │                           │
└────────────────┴────────────────────┴───────────────────────────┘
```

Each module is a standalone Hermes skill + config file. You can give any one to a different tool without needing the others.

---

## Module 1: OBSERVER — Token Observability Layer

### What it does
- Wraps codeburn (`npm install -g codeburn`) so you can ask Hermes "what's my spend today?" or "show me my waste report"
- Adds a daily cron job that runs `codeburn optimize` and surfaces the top 3 waste findings
- Outputs a weekly digest: top cost drivers, cache hit rate, one-shot rate per model

### Files
```
~/.hermes/skills/token-cost/
  observer/SKILL.md        ← Hermes skill: query codeburn in natural language
  observer/scripts/
    daily-waste.sh          ← cron: runs codeburn optimize, extracts top 3 findings
    weekly-digest.sh        ← cron: weekly summary report
```

### Cron jobs
- Daily @ 9am: waste audit → surface top 3 findings to chat
- Weekly Monday @ 9am: full digest (cost trend, cache hit %, model comparison)

### Dependencies
- `npm install -g codeburn` (Node 22+)
- Works automatically for: Claude Code, Codex, Cursor, OpenCode, Gemini CLI, GitHub Copilot, OpenClaw, Roo Code, KiloCode, Kiro (20 tools total — codeburn already supports them)

---

## Module 2: ROUTER — Multi-Model Routing Config

### What it does
- A single YAML routing config (`~/.config/token-router/config.yaml`) that defines which model handles which task type
- A Hermes skill that helps you generate/tune this config for your specific tooling
- Ready-made config files for: Claude Code (CLAUDE.md patterns), Cursor (.cursorrules), Aider (CLI flags), Codex, OpenCode

### Routing tiers (per the article)
```
Tier       Model              Use case                         Cost
──────────────────────────────────────────────────────────────────
Premium    Claude Opus 4.6    Architecture, security review    $15/$75/M
Workhorse  Kimi 2.6           All serious impl/debug/refactor  $0.50/$2/M
Utility    Claude Haiku 4.5   Lint, format, single-line edits  $1/$5/M
Local      Ollama/Qwen3       Autocomplete, boilerplate        $0
```

### Files
```
~/.config/token-router/
  config.yaml               ← canonical routing config (copy-paste ready)
  profiles/
    claude-code.md           ← CLAUDE.md snippet to paste
    cursor.md                ← .cursorrules snippet
    aider.md                 ← CLI flags for aider
    codex.md                 ← Codex config snippet
    opencode.md              ← OpenCode config snippet

~/.hermes/skills/token-cost/
  router/SKILL.md            ← Hermes skill: help tune routing, explain decisions
```

### Key config (the article's exact config, enhanced)
```yaml
# ~/.config/token-router/config.yaml
default: kimi-2.6-instruct

routes:
  planning:
    model: claude-opus-4-6
    fallback: gpt-5
    triggers: [plan, architect, design system, security review]

  implementation:
    model: kimi-2.6-instruct
    triggers: [review, debug, implement, build feature, refactor]

  cleanup:
    model: claude-haiku-4-5
    triggers: [lint, format, fix typo, rename variable]

  boilerplate:
    model: ollama:qwen3:7b
    triggers: [autocomplete, stub, generate boilerplate]

caching:
  enabled: true
  prefix_cache: true

context:
  max_tokens: 50000
  auto_summarize_after: 15
  use_grep_first: true
```

---

## Module 3: COMPRESSOR — Output Token Compression

### What it does
- A Hermes skill that activates caveman-style output compression on demand
- Installs caveman into whichever tool you use (Claude Code, Codex, Cursor, etc.) via their own install script
- Adds a custom `compress` command to Hermes that rewrites verbose outputs/docs/CLAUDE.md into compressed form
- Includes the caveman-compress script from the repo (reduces CLAUDE.md by ~46%)

### Files
```
~/.hermes/skills/token-cost/
  compressor/SKILL.md        ← Hermes skill: activate compression, compress files
  compressor/scripts/
    install-caveman.sh        ← installs caveman to the user's active tools
    compress-file.sh          ← wraps caveman-compress for any .md/.txt file
```

### Installation approach
```bash
# One-line install into Claude Code / Codex / etc.
curl -fsSL https://raw.githubusercontent.com/JuliusBrussee/caveman/main/install.sh | bash
```

Hermes skill wraps this and also provides:
- `/compress <file>` — compress any markdown file (CLAUDE.md, PRDs, docs)
- `/caveman` — activate compressed mode for the current session
- Stats: "how many tokens did I save this session?"

---

## Module 4: ORCHESTRATOR — The Master Skill

A single `token-cost-master` Hermes skill that routes to the 3 modules above.

Triggers:
- "show my spend" → Observer
- "optimize my costs" → Observer (waste audit) + Router (suggest tier changes)
- "compress this file" → Compressor
- "what model should I use for X?" → Router
- "set up caveman" → Compressor (install)
- "weekly report" → Observer (digest)

---

## Phase Breakdown

### Phase 1: OBSERVER (Day 1, ~2h)
1. Install codeburn globally
2. Write `observer/SKILL.md` — natural language queries over codeburn output
3. Write `daily-waste.sh` — parses `codeburn optimize --format json`, extracts top 3
4. Write `weekly-digest.sh` — full report with cost trend + model comparison
5. Set up 2 cron jobs in Hermes (daily + weekly)
6. Test: ask Hermes "what's my spend today?" and "show waste report"

### Phase 2: ROUTER (Day 1-2, ~1h)
1. Write the canonical `config.yaml` with all 4 tiers
2. Generate per-tool snippets (CLAUDE.md, .cursorrules, aider flags, codex, opencode)
3. Write `router/SKILL.md` — helps tune routing, explains trade-offs
4. Test: ask Hermes "what model should I use to debug a race condition?"

### Phase 3: COMPRESSOR (Day 2, ~1h)
1. Write `compressor/SKILL.md` — wraps caveman install + compress-file
2. Write `install-caveman.sh` and `compress-file.sh`
3. Test: compress an existing CLAUDE.md, verify token savings
4. Test: activate caveman mode for a Hermes session

### Phase 4: ORCHESTRATOR (Day 2, ~30min)
1. Write `token-cost-master/SKILL.md` with routing table to sub-skills
2. Test all trigger phrases end-to-end

---

## Modularity Contract

Each module is self-contained:
- Has its own SKILL.md installable into any Hermes instance
- Has its own scripts/ directory with no cross-module deps
- Config files are plain YAML/shell, usable without Hermes
- Router config works standalone — paste into CLAUDE.md, .cursorrules, aider, etc.
- Compressor works standalone — just run `install.sh` from caveman repo
- Observer works standalone — just use `codeburn` CLI directly

Anyone can take Module 2 (router config) alone and get 50-70% cost reduction without installing anything else.

---

## File Structure (Final)

```
~/.hermes/skills/token-cost/
  token-cost-master/
    SKILL.md
  observer/
    SKILL.md
    scripts/
      daily-waste.sh
      weekly-digest.sh
  router/
    SKILL.md
    templates/
      config.yaml
      claude-code.md
      cursor.md
      aider.md
      codex.md
      opencode.md
  compressor/
    SKILL.md
    scripts/
      install-caveman.sh
      compress-file.sh

~/.config/token-router/
  config.yaml               ← live router config

Hermes cron jobs:
  token-observer-daily      ← 9am daily waste audit
  token-observer-weekly     ← Monday 9am digest
```

---

## Open Questions / Decisions Needed

1. Where do you want the cron job output to land? Current chat? A specific Telegram/Discord channel?
2. Do you want the router config to be active immediately (copy CLAUDE.md snippets into your existing projects) or just generated for future use?
3. For compressor: install caveman into Claude Code only, or all tools you have (Codex, Cursor, OpenCode)?
4. codeburn tracks Claude Code, Codex, Cursor, Gemini CLI, GitHub Copilot, OpenCode, Roo Code, and 13 more. Which tools are you actively using that we should focus the observer on?

---

## Expected Outcomes

- Observer: know exactly where every dollar goes, automated waste surfacing
- Router: 50-70% bill reduction from model routing alone (the single biggest lever)
- Compressor: additional 65-75% on output tokens for sessions where it's active
- Realistic combined reduction: 75-85% total bill, matching the article's claim
