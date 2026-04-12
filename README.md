# Vise

A Claude Code plugin that locks your documents in a dual-LLM review vise. Claude acts as the Primary Architect, Google's Gemini CLI acts as a Staff Engineer Reviewer, and they iterate back and forth until the design converges.

Like a craftsman's vise that holds a workpiece rigid while precision tools are applied, the append-only debate ledger prevents the LLMs from hallucinating or drifting while they hammer out architectural flaws.

## Prerequisites

1. [Claude Code](https://claude.com/claude-code) installed
2. Python 3.9+
3. [Google Gemini CLI](https://github.com/google-gemini/gemini-cli) installed and authenticated:
   ```bash
   npm install -g @google/gemini-cli
   gemini   # run once interactively to complete OAuth
   ```
4. Verify Gemini works headlessly:
   ```bash
   echo hi | gemini -m gemini-3-pro-preview -p ""
   ```
   If that fails, check your auth or set `GEMINI_MODEL` to a working model ID.

## Install

From inside a Claude Code session:

```
/plugin marketplace add brooksc/vise
/plugin install vise@vise
/reload-plugins
```

Verify the skill is available by typing `/vise:` — you should see `review` in the autocomplete.

## Usage

```
/vise:review spec.md                        # 3 debate cycles (default)
/vise:review docs/design.md 5 iterations    # 5 cycles
```

Each cycle:
1. Claude reads the doc, drafts changes, and posts questions to a debate ledger
2. The bridge pipes the doc + ledger to `gemini -m gemini-3-pro-preview -p "..."`
3. Gemini's review is appended to the ledger
4. Claude reads Gemini's feedback, decides whether to accept or counter-argue, and loops

The loop stops when:
- Gemini signals `[NO_FURTHER_FEEDBACK]` (natural convergence)
- The cycle cap is reached (Claude tie-breaks)
- Claude declares `[CONVERGENCE_ACHIEVED]`

## What gets created

A `.vise/` directory is created next to each target document:

```
docs/
├── design.md                          # your document
└── .vise/
    ├── design.discussion.md           # the debate ledger
    └── design.lock                    # advisory lock (never deleted)
```

Add `.vise/` to `.gitignore` unless you want the debate ledgers committed.

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `GEMINI_MODEL` | `gemini-3-pro-preview` | Override the Gemini model ID |

## How it works

The plugin consists of two files in `plugins/vise/skills/review/`:

- **`SKILL.md`** — The orchestration prompt that tells Claude how to run the debate loop, interpret exit codes, and manage the ledger.
- **`vise_bridge.py`** — A Python script that handles file I/O, pipes context to the Gemini CLI via stdin, enforces the cycle cap, detects convergence signals, and manages advisory locks.

## Uninstall

```
/plugin uninstall vise@vise
/plugin marketplace remove vise
```

## License

MIT
