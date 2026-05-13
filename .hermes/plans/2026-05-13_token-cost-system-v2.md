# Token Cost System — Revised Plan (Hermes-First)
Date: 2026-05-13
Status: APPROVED — awaiting execution

---

## What We're Building

A modular token cost system that lives inside Hermes as skills.
Hermes-first. Extendable to any other tool.
No cron jobs. No automation. On-demand, at your control.

```
┌─────────────────────────────────────────────────────────┐
│              TOKEN COST SYSTEM (Hermes-first)            │
├────────────────┬───────────────────┬────────────────────┤
│  Module 1      │  Module 2         │  Module 3          │
│  ROUTER        │  COMPRESSOR       │  OBSERVER          │
│                │                   │                    │
│  Auto-loads    │  Caveman          │  codeburn          │
│  at chat start │  installed INTO   │  on-demand queries │
│  Shows routing │  Hermes as skill  │  "what did I spend │
│  decision tree │  /compress /       │  today?"          │
│  for the task  │  caveman mode      │                   │
└────────────────┴───────────────────┴────────────────────┘
         ↑
  token-cost-master skill orchestrates all 3
```

---

## Module 1: ROUTER — Auto-Loads at Chat Start

### What it does
A skill that gets loaded at the start of any Hermes chat.
When loaded it shows you (and the agent) the routing decision tree so every model choice is deliberate, not default.

### Routing table (per the article, hardened)
```
Task type                      → Model            Cost/M in/out
──────────────────────────────────────────────────────────────
Architecture / system design   → Claude Opus 4.6  $15/$75
Security review, complex debug → Claude Opus 4.6  $15/$75
Implementation, code review,   → Kimi 2.6         $0.50/$2
  debugging, refactoring       (default workhorse)
Long agentic loops (10+ steps) → Kimi 2.6         $0.50/$2
Lint, format, rename, single   → Claude Haiku 4.5 $1/$5
  line edits
Autocomplete, boilerplate,     → Ollama/Qwen3     $0
  stub generation
```

### How it loads at chat start
- Saved as `~/.hermes/skills/token-cost/router/SKILL.md`
- Add `router` to the auto-loaded skills list in Hermes config
- At each chat start, the routing table and context tips surface in the agent's system context
- Result: every session starts with "here is the routing map, pick the right model"

### Extendability (other tools)
- The routing table also gets written as:
  - `templates/CLAUDE.md-snippet.md`  — paste into any project's CLAUDE.md
  - `templates/cursorrules-snippet.md` — paste into .cursorrules
  - `templates/config.yaml`           — standalone router config

---

## Module 2: COMPRESSOR — Caveman Inside Hermes

### What it does
Installs caveman's compression rules as a Hermes skill.
When activated: Hermes responses are ~75% fewer output tokens, full technical accuracy.
Also: compress any .md file (CLAUDE.md, PRDs, wikis) to shrink input tokens ~46%.

### Installation plan
1. Copy `skills/caveman/SKILL.md` from the caveman repo into Hermes skills
2. Copy `skills/caveman-compress/SKILL.md` and its compress.py script
3. Copy `skills/caveman-stats/SKILL.md`

These become native Hermes skills — no external install needed.

### Commands available after install
```
/caveman [lite|full|ultra]    activate compressed mode for this session
/compress <filepath>          shrink any .md/.txt file (~46% smaller)
/caveman-stats                token savings this session in USD
```

### Extendability
- Caveman repo has a one-line installer for 30+ other tools:
  `curl -fsSL .../install.sh | bash`
- Our Hermes skill includes an `install-to-tool.sh` that lets you push it to Claude Code, Codex, Cursor, etc.

---

## Module 3: OBSERVER — On-Demand Spend Queries

### What it does
Wraps codeburn CLI as a Hermes skill.
You ask in natural language, get spend data back.
No automation, no cron — you pull it when you want it.

### Commands
```
"what did I spend today?"           → codeburn status
"show my spend this month"          → codeburn report -p 30days
"show waste report"                 → codeburn optimize
"compare Sonnet vs Kimi cost"       → codeburn compare
"which sessions burned most?"       → codeburn report --format json (top 5)
"show per-project breakdown"        → codeburn report (projects table)
```

### Dependencies
- `npm install -g codeburn` (Node 22+, one-time install)
- Works for: Claude Code, Codex, Cursor, Gemini CLI, Copilot, OpenCode, Roo, Kilo, Kiro, and 11 more

### Extendability
- Skill is just a thin wrapper — anyone can copy it to their Hermes instance
- codeburn itself is MIT, works standalone without Hermes

---

## Module 4: MASTER ORCHESTRATOR

Single `token-cost-master/SKILL.md` that routes trigger phrases:

```
"what should I use for X?"      → Router
"optimize / save costs"         → Router + Observer
"compress this file"            → Compressor
"/caveman"                      → Compressor
"what did I spend?"             → Observer
"waste report"                  → Observer
```

This is the skill you tell people "install this one skill and you have the whole system."

---

## File Structure

```
~/.hermes/skills/token-cost/
  token-cost-master/
    SKILL.md

  router/
    SKILL.md
    templates/
      config.yaml               ← standalone router config
      CLAUDE.md-snippet.md      ← paste into any project
      cursorrules-snippet.md    ← paste into .cursorrules
      aider-flags.md            ← CLI flags
      codex-config.md

  compressor/
    SKILL.md
    scripts/
      compress.py               ← from caveman-compress (unchanged)
      install-to-tool.sh        ← push caveman to Claude Code / Codex / Cursor
    sub-skills/
      caveman/SKILL.md          ← from caveman repo (compression rules)
      caveman-compress/SKILL.md ← file compression
      caveman-stats/SKILL.md    ← session savings stats

  observer/
    SKILL.md
    scripts/
      query.sh                  ← thin wrapper: codeburn CLI → structured output
```

---

## Build Order

### Step 1: Router skill + templates (~45 min)
- Write `router/SKILL.md` with the routing table + context discipline tips
- Generate all 4 per-tool template files
- Wire up as an auto-loading skill in Hermes

### Step 2: Compressor skill (~30 min)
- Copy caveman SKILL.md files from the cloned repo (already at ~/Desktop/ai-pr-review-agent/caveman_repo)
- Adapt caveman/SKILL.md to be a native Hermes skill (minor frontmatter changes)
- Copy compress.py, wire up the /compress command
- Write install-to-tool.sh for extendability

### Step 3: Observer skill (~20 min)
- Check if codeburn is already installed
- Write `observer/SKILL.md` with the NL → codeburn command mapping
- Test: ask "what did I spend today?"

### Step 4: Master orchestrator (~15 min)
- Write `token-cost-master/SKILL.md` with trigger routing table
- Test all trigger phrases end-to-end

---

## Decisions Made

- No cron jobs
- Hermes-only for now, extendable via templates + install-to-tool.sh
- caveman goes directly into Hermes skills (no external install required for Hermes)
- Observer requires one npm install (codeburn) — we do that in Step 3
- Router auto-loads at every Hermes chat start
