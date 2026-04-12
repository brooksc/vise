#!/usr/bin/env python3
"""vise bridge: pipe a target doc + debate ledger through the Gemini CLI."""
import argparse
import subprocess
import sys
import os
import re
import datetime
import shutil
import fcntl
from pathlib import Path

# Default to the working Gemini 3 Pro ID confirmed against the CLI.
# Override via env var if Google renames it.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-pro-preview")
# Soft guard against context bloat. Char count is a rough proxy for tokens;
# dense code or CJK will hit real token limits earlier. Lower if you see API
# 400s before this trips.
MAX_PAYLOAD_CHARS = 750_000
# Kill a hung gemini subprocess after this many seconds.
SUBPROCESS_TIMEOUT = 300
# Token Gemini is instructed to append when it has no more feedback.
NO_FEEDBACK_TOKEN = "[NO_FURTHER_FEEDBACK]"
# Collisions we defensively strip from Gemini output — ONLY these specific
# ledger headers, not all '### ...' lines.
LEDGER_HEADER_PATTERNS = (
    re.compile(r"^\s*###\s+Gemini Reviewer\b", re.IGNORECASE),
    re.compile(r"^\s*###\s+Primary Architect\b", re.IGNORECASE),
)
# Exit codes (documented contract with the slash command loop).
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
    "You may use '### ' sub-section headers, but NEVER emit '### Gemini Reviewer' "
    "or '### Primary Architect' — those are reserved for the ledger. "
    f"If you have no further substantive feedback, end your response with the literal token {NO_FEEDBACK_TOKEN} on its own line."
)


def resolve_ledger_paths(design_path: Path) -> tuple[Path, Path, Path]:
    """Anchor ledger dir next to the target doc so CWD changes can't fragment state.
    Uses .stem (not .name) so 'foo.md' → 'foo.discussion.md', not 'foo.md.discussion.md'."""
    ledger_dir = design_path.parent / ".vise"
    ledger_dir.mkdir(exist_ok=True)
    stem = design_path.stem
    ledger_path = ledger_dir / f"{stem}.discussion.md"
    lock_path = ledger_dir / f"{stem}.lock"
    return ledger_dir, ledger_path, lock_path


def verify_dependencies():
    if not shutil.which("gemini"):
        print(
            "CRITICAL ERROR: 'gemini' CLI not found. Run 'npm install -g @google/gemini-cli'.",
            file=sys.stderr,
        )
        sys.exit(EXIT_ERROR)


def next_cycle_number(ledger_content: str) -> int:
    """Use max(cycle_numbers) + 1 instead of len(matches) so that quoted
    references to earlier cycles can't inflate the counter."""
    real_turns = re.findall(
        r"^### Gemini Reviewer — Cycle (\d+) \((?!SYSTEM\))",
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


def parse_args():
    ap = argparse.ArgumentParser(description="vise Gemini bridge")
    ap.add_argument("--design", required=True, help="path to the target document under review")
    ap.add_argument("--max-cycles", type=int, default=3, help="hard cap on debate cycles (default 3)")
    return ap.parse_args()


def run():
    args = parse_args()
    verify_dependencies()

    design_path = Path(args.design).resolve()
    if not design_path.exists():
        print(f"ERROR: target document {args.design} does not exist.", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    ledger_dir, ledger_path, lock_path = resolve_ledger_paths(design_path)
    ledger_path.touch(exist_ok=True)

    # Advisory lock: open in append mode so we never truncate, and never
    # unlink the lockfile (unlink-under-lock is a classic POSIX race).
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

        # Bridge-enforced convergence. The SYSTEM marker is excluded from the
        # cycle counter so re-invocation after forced convergence does not
        # infinite-loop.
        if cycle > args.max_cycles:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            marker = (
                f"\n\n### Gemini Reviewer — Cycle {cycle} (SYSTEM) ({timestamp})\n"
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

        cmd = ["gemini", "-m", MODEL, "-p", cycle_prompt]
        # Force UTF-8 and disable color even if the child inherits a C locale
        # or a TTY that would otherwise leak ANSI escapes into the ledger.
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"
        env.setdefault("LANG", "en_US.UTF-8")
        env.setdefault("LC_ALL", "en_US.UTF-8")

        print(f"Triggering Gemini review (cycle {cycle}/{args.max_cycles}, model={MODEL})...")
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
            print(f"GEMINI CLI TIMEOUT after {SUBPROCESS_TIMEOUT}s.", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        except FileNotFoundError:
            print("CRITICAL ERROR: 'gemini' CLI disappeared between check and exec.", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        except subprocess.CalledProcessError as e:
            print(
                "GEMINI CLI SUBPROCESS ERROR\n"
                f"Return code: {e.returncode}\n"
                f"Stderr:\n{e.stderr}\n"
                "If this is an auth failure, run `gemini` interactively once to refresh OAuth.",
                file=sys.stderr,
            )
            sys.exit(EXIT_ERROR)

        raw_output = (result.stdout or "").strip()
        if not raw_output:
            print(
                f"CRITICAL ERROR: Gemini returned empty output.\nStderr:\n{result.stderr}",
                file=sys.stderr,
            )
            sys.exit(EXIT_ERROR)

        signaled_done = NO_FEEDBACK_TOKEN in raw_output

        cleaned = strip_ledger_collisions(raw_output).strip()
        # If defensive stripping removed everything (shouldn't happen, but
        # don't false-positive an "empty output" error), fall back to raw.
        gemini_output = cleaned if cleaned else raw_output

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(f"\n\n### Gemini Reviewer — Cycle {cycle} ({timestamp})\n")
            f.write(f"{gemini_output}\n")

        if signaled_done:
            print(f"SUCCESS: Cycle {cycle} appended. Gemini signaled {NO_FEEDBACK_TOKEN}.")
            sys.exit(EXIT_NO_FURTHER_FEEDBACK)

        # If this WAS the last allowed cycle, tell Claude to tie-break now
        # instead of wasting a turn drafting Cycle N+1 just to be rejected.
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
        # NOTE: We intentionally do NOT unlink lock_path. Unlink-under-lock is
        # a POSIX race: another process that has the file open can acquire a
        # lock on a stale inode and bypass the mutex.


if __name__ == "__main__":
    run()
