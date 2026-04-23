#!/usr/bin/env python3
"""vise bridge: pipe a target doc + debate ledger through Gemini or Codex CLI."""
import argparse
import subprocess
import sys
import os
import re
import datetime
import shutil
import fcntl
from pathlib import Path

# Reviewer selection: "auto" tries gemini first, then codex; or force one.
REVIEWER = os.environ.get("VISE_REVIEWER", "auto").lower()

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-pro-preview")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "o4-mini")
MAX_PAYLOAD_CHARS = 750_000
SUBPROCESS_TIMEOUT = 300
NO_FEEDBACK_TOKEN = "[NO_FURTHER_FEEDBACK]"
LEDGER_HEADER_PATTERNS = (
    re.compile(r"^\s*###\s+Gemini Reviewer\b", re.IGNORECASE),
    re.compile(r"^\s*###\s+Codex Reviewer\b", re.IGNORECASE),
    re.compile(r"^\s*###\s+Primary Architect\b", re.IGNORECASE),
)
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PAYLOAD_TOO_LARGE = 2
EXIT_NO_FURTHER_FEEDBACK = 3
EXIT_FORCED_CONVERGENCE = 4

SYSTEM_INSTRUCTION = (
    "You are a Staff Engineer reviewing a technical document. "
    "Read the current document, then read the discussion history. "
    "Address the most recent questions posed by the Primary Architect. "
    "Be brutally honest, look for edge cases, and propose concrete fixes. "
    "You may use '### ' sub-section headers, but NEVER emit '### Gemini Reviewer', "
    "'### Codex Reviewer', or '### Primary Architect' — those are reserved for the ledger. "
    f"If you have no further substantive feedback, end your response with the literal token {NO_FEEDBACK_TOKEN} on its own line."
)


def select_reviewer() -> str:
    """Return 'gemini' or 'codex', or exit with an error."""
    if REVIEWER == "gemini":
        if not shutil.which("gemini"):
            print("ERROR: 'gemini' CLI not found. Run 'npm install -g @google/gemini-cli'.", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        return "gemini"
    if REVIEWER == "codex":
        if not shutil.which("codex"):
            print("ERROR: 'codex' CLI not found. Install from https://github.com/openai/codex.", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        return "codex"
    # auto: prefer gemini, fall back to codex
    if shutil.which("gemini"):
        return "gemini"
    if shutil.which("codex"):
        return "codex"
    print(
        "ERROR: Neither 'gemini' nor 'codex' CLI found. "
        "Install one: 'npm install -g @google/gemini-cli' or the Codex CLI.",
        file=sys.stderr,
    )
    sys.exit(EXIT_ERROR)


def resolve_ledger_paths(design_path: Path) -> tuple[Path, Path, Path]:
    ledger_dir = design_path.parent / ".vise"
    ledger_dir.mkdir(exist_ok=True)
    stem = design_path.stem
    ledger_path = ledger_dir / f"{stem}.discussion.md"
    lock_path = ledger_dir / f"{stem}.lock"
    return ledger_dir, ledger_path, lock_path


def next_cycle_number(ledger_content: str) -> int:
    real_turns = re.findall(
        r"^### (?:Gemini|Codex) Reviewer — Cycle (\d+) \((?!SYSTEM\))",
        ledger_content,
        re.MULTILINE,
    )
    if not real_turns:
        return 1
    return max(int(m) for m in real_turns) + 1


def strip_ledger_collisions(text: str) -> str:
    kept = [
        line for line in text.splitlines()
        if not any(p.match(line) for p in LEDGER_HEADER_PATTERNS)
    ]
    return "\n".join(kept)


def build_subprocess_cmd(reviewer: str, cycle_prompt: str) -> list[str]:
    if reviewer == "gemini":
        return ["gemini", "-m", MODEL, "-p", cycle_prompt]
    return ["codex", "exec", "-m", CODEX_MODEL, cycle_prompt]


def parse_args():
    ap = argparse.ArgumentParser(description="vise reviewer bridge")
    ap.add_argument("--design", required=True, help="path to the target document under review")
    ap.add_argument("--max-cycles", type=int, default=3, help="hard cap on debate cycles (default 3)")
    return ap.parse_args()


def run():
    args = parse_args()
    reviewer = select_reviewer()
    reviewer_label = "Gemini Reviewer" if reviewer == "gemini" else "Codex Reviewer"

    design_path = Path(args.design).resolve()
    if not design_path.exists():
        print(f"ERROR: target document {args.design} does not exist.", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    _, ledger_path, lock_path = resolve_ledger_paths(design_path)
    ledger_path.touch(exist_ok=True)

    lock_fd = open(lock_path, "a")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(
            f"ERROR: another vise run is already active (lock held on {lock_path}).",
            file=sys.stderr,
        )
        lock_fd.close()
        sys.exit(EXIT_ERROR)

    try:
        design_content = design_path.read_text(encoding="utf-8")
        ledger_content = ledger_path.read_text(encoding="utf-8")
        cycle = next_cycle_number(ledger_content)

        if cycle > args.max_cycles:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            marker = (
                f"\n\n### {reviewer_label} — Cycle {cycle} (SYSTEM) ({timestamp})\n"
                f"[CONVERGENCE FORCED: reached --max-cycles={args.max_cycles}]\n"
            )
            with ledger_path.open("a", encoding="utf-8") as f:
                f.write(marker)
            print(
                f"CONVERGENCE FORCED at max-cycles={args.max_cycles}. "
                "Claude must tie-break and finalize.",
                file=sys.stderr,
            )
            sys.exit(EXIT_FORCED_CONVERGENCE)

        stdin_payload = (
            f"SYSTEM INSTRUCTION: {SYSTEM_INSTRUCTION}\n\n"
            "--- START CURRENT TARGET DOC ---\n"
            f"{design_content}\n"
            "--- END TARGET DOC ---\n\n"
            "--- START DISCUSSION HISTORY ---\n"
            f"{ledger_content}\n"
            "--- END DISCUSSION HISTORY ---\n"
        )
        cycle_prompt = (
            f"This is cycle {cycle} of at most {args.max_cycles}. "
            "Provide your next response; it will be appended directly to the ledger. "
            f"If you have nothing substantive left to add, end with {NO_FEEDBACK_TOKEN}."
        )

        if len(stdin_payload) > MAX_PAYLOAD_CHARS:
            archive_ts = int(datetime.datetime.now().timestamp())
            archive_path = ledger_path.with_suffix(f".archive.{archive_ts}.md")
            shutil.move(str(ledger_path), str(archive_path))
            print(
                f"ERROR: payload is {len(stdin_payload):,} chars (limit {MAX_PAYLOAD_CHARS:,}). "
                f"Ledger archived to {archive_path}. "
                "Claude must write a consensus summary to a fresh ledger and retry.",
                file=sys.stderr,
            )
            sys.exit(EXIT_PAYLOAD_TOO_LARGE)

        cmd = build_subprocess_cmd(reviewer, cycle_prompt)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"
        env.setdefault("LANG", "en_US.UTF-8")
        env.setdefault("LC_ALL", "en_US.UTF-8")

        model_id = MODEL if reviewer == "gemini" else CODEX_MODEL
        print(f"Triggering {reviewer_label} review (cycle {cycle}/{args.max_cycles}, model={model_id})...")
        try:
            result = subprocess.run(
                cmd,
                input=stdin_payload,
                capture_output=True,
                text=True,
                check=True,
                timeout=SUBPROCESS_TIMEOUT,
                encoding="utf-8",
                env=env,
            )
        except subprocess.TimeoutExpired:
            print(f"{reviewer_label.upper()} CLI TIMEOUT after {SUBPROCESS_TIMEOUT}s.", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        except FileNotFoundError:
            print(f"CRITICAL ERROR: '{reviewer}' CLI disappeared between check and exec.", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        except subprocess.CalledProcessError as e:
            auth_hint = (
                "run `gemini` interactively once to refresh OAuth."
                if reviewer == "gemini"
                else "run `codex login` to authenticate."
            )
            print(
                f"{reviewer_label.upper()} CLI SUBPROCESS ERROR\n"
                f"Return code: {e.returncode}\n"
                f"Stderr:\n{e.stderr}\n"
                f"If this is an auth failure, {auth_hint}",
                file=sys.stderr,
            )
            sys.exit(EXIT_ERROR)

        raw_output = (result.stdout or "").strip()
        if not raw_output:
            print(
                f"CRITICAL ERROR: {reviewer_label} returned empty output.\nStderr:\n{result.stderr}",
                file=sys.stderr,
            )
            sys.exit(EXIT_ERROR)

        signaled_done = NO_FEEDBACK_TOKEN in raw_output

        cleaned = strip_ledger_collisions(raw_output).strip()
        reviewer_output = cleaned if cleaned else raw_output

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(f"\n\n### {reviewer_label} — Cycle {cycle} ({timestamp})\n")
            f.write(f"{reviewer_output}\n")

        if signaled_done:
            print(f"SUCCESS: Cycle {cycle} appended. {reviewer_label} signaled {NO_FEEDBACK_TOKEN}.")
            sys.exit(EXIT_NO_FURTHER_FEEDBACK)

        if cycle >= args.max_cycles:
            print(
                f"SUCCESS: Cycle {cycle} appended. Final cycle (max-cycles={args.max_cycles}) "
                "reached — Claude must tie-break now."
            )
            sys.exit(EXIT_FORCED_CONVERGENCE)

        print(f"SUCCESS: Cycle {cycle} review appended to {ledger_path}.")
        sys.exit(EXIT_OK)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            lock_fd.close()


if __name__ == "__main__":
    run()
