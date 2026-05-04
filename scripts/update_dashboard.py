#!/usr/bin/env python3
"""
update_dashboard.py
Weekly auto-update for the FlowTrader design dashboard.

Reads FlowTrader source files, checks if anything changed via a SHA-256 hash,
and uses Claude to regenerate index.html only when the source has changed.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic

HASH_FILE = Path(".dashboard-hash")
INDEX_FILE = Path("index.html")

SOURCE_FILES = [
    "main.py",
    "config.yaml",
    "requirements.txt",
    "agents/decision.py",
    "agents/executor.py",
    "agents/researcher.py",
    "data/fetcher.py",
    "journal/logger.py",
    ".github/workflows/trading-bot.yml",
]

# Extra files read from a separate path (e.g. the deployed dashboard repo)
EXTRA_SOURCE_FILES: dict[str, str] = {}  # populated at runtime if FLOWTRADER_DASHBOARD_PATH is set

SYSTEM_PROMPT = """You are a technical documentation expert maintaining a self-contained HTML \
design dashboard for FlowTrader, an automated trading system.

You will receive the current dashboard HTML and the current source code. Update the dashboard \
HTML to accurately reflect the current state of the source code.

Rules:
- Preserve the visual design, dark terminal theme, CSS, and overall structure exactly
- Only update content that has actually changed: agent descriptions, schedule tables, config \
values, watchlist symbols, environment variables, signal scoring logic, strategy parameters, etc.
- Do not add new sections unless a genuinely new major component exists in the source
- Do not remove sections unless a component was completely removed
- Return ONLY the complete updated HTML — no explanation, no markdown fences, no preamble
- CRITICAL: Your response must begin with the exact characters `<!DOCTYPE html>` — \
nothing before it, not even a single space or newline. Any text before `<!DOCTYPE html>` \
will corrupt the page."""


def read_source_files(source_path: Path) -> dict[str, str]:
    files = {}
    for rel in SOURCE_FILES:
        p = source_path / rel
        if p.exists():
            files[rel] = p.read_text(encoding="utf-8")
        else:
            print(f"  [warn] {rel} not found", file=sys.stderr)

    # Also read dashboard.py from the deployed dashboard repo if path is set
    dashboard_path = os.environ.get("FLOWTRADER_DASHBOARD_PATH")
    if dashboard_path:
        for extra_rel in ["dashboard.py", "agents/researcher.py", "data/fetcher.py"]:
            ep = Path(dashboard_path) / extra_rel
            if ep.exists() and extra_rel not in files:
                files[f"flowtrader-dashboard/{extra_rel}"] = ep.read_text(encoding="utf-8")

    files.update(EXTRA_SOURCE_FILES)
    return files


def compute_hash(files: dict) -> str:
    combined = json.dumps(files, sort_keys=True)
    return hashlib.sha256(combined.encode()).hexdigest()


def regenerate(files: dict, current_html: str) -> str:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    files_block = "\n\n".join(
        f"=== {path} ===\n{content}" for path, content in files.items()
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"CURRENT DASHBOARD HTML:\n{current_html}\n\n"
                f"CURRENT SOURCE CODE:\n{files_block}\n\n"
                "Update the dashboard to accurately reflect the current source. "
                "Return the complete HTML."
            ),
        }],
    )
    return resp.content[0].text


def main():
    source_path = Path(os.environ.get("FLOWTRADER_SOURCE_PATH", "flowtrader-source"))
    if not source_path.exists():
        print(f"ERROR: source path not found: {source_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading source files from {source_path}...")
    files = read_source_files(source_path)
    print(f"  Read {len(files)} file(s)")

    new_hash = compute_hash(files)
    old_hash = HASH_FILE.read_text().strip() if HASH_FILE.exists() else None

    if new_hash == old_hash:
        print("No changes detected — dashboard is up to date.")
        return

    old_short = old_hash[:8] if old_hash else "none"
    print(f"Changes detected ({old_short} -> {new_hash[:8]}). Calling Claude...")

    current_html = INDEX_FILE.read_text(encoding="utf-8")
    updated_html = regenerate(files, current_html)

    INDEX_FILE.write_text(updated_html, encoding="utf-8")
    HASH_FILE.write_text(new_hash)
    print("Dashboard updated.")


if __name__ == "__main__":
    main()
