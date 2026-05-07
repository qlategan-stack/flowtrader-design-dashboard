#!/usr/bin/env python3
"""
update_dashboard.py
Bi-weekly auto-update for the FlowTrader design dashboard.

Reads FlowTrader source files, checks if anything changed via a SHA-256 hash,
and uses Claude to regenerate index.html only when the source has changed.

On every successful regeneration:
  1. Archives the previous index.html to archive/index-v<old>.html
  2. Bumps the dashboard version (v1.0 → v1.1 → v1.2 ...)
  3. Stamps today's date into the footer
"""

import hashlib
import json
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

from anthropic import Anthropic

HASH_FILE   = Path(".dashboard-hash")
INDEX_FILE  = Path("index.html")
ARCHIVE_DIR = Path("archive")

# Source files read from the trading-bot repo (FLOWTRADER_SOURCE_PATH).
# Add new entries here when the bot grows new top-level files; the hash will
# pick up on changes and trigger a regeneration.
SOURCE_FILES = [
    "main.py",
    "config.yaml",
    "requirements.txt",
    "CLAUDE.md",
    "agents/decision.py",
    "agents/executor.py",
    "agents/researcher.py",
    "agents/analyst_in.py",
    "agents/analyst_out.py",
    "agents/risk_manager.py",
    "data/fetcher.py",
    "data/crypto_fetcher.py",
    "journal/logger.py",
    "journal/suggestion_store.py",
    "scripts/push_journal.py",
    ".github/workflows/trading-bot.yml",
]

# Files read from the deployed dashboard repo (FLOWTRADER_DASHBOARD_PATH).
DASHBOARD_FILES = [
    "dashboard.py",
    "data/fetcher.py",
    "data/crypto_fetcher.py",
    "journal/logger.py",
    ".github/workflows/sync-journal.yml",
]

SYSTEM_PROMPT = """You are a technical documentation expert maintaining a self-contained HTML \
design dashboard for FlowTrader, an automated trading system.

You will receive the current dashboard HTML and the current source code. Update the dashboard \
HTML to accurately reflect the current state of the source code.

Rules for what to PRESERVE (do not change):
- The visual design, dark terminal theme, CSS, and overall page structure
- The page navigation, hero, and footer layout
- The {{VERSION}} placeholder string in the <h1> and the {{UPDATED}} placeholder in the footer \
  if they exist — they will be filled in by the post-processor

Rules for what to UPDATE:
- Agent descriptions, class names, method signatures, and file paths when they have changed
- Schedule cron tables when the workflow cron has changed
- Config values (watchlist symbols, thresholds, position caps)
- Environment variable names
- Signal scoring logic and risk-rule descriptions
- Strategy parameters (RSI bands, ADX threshold, ATR multipliers, exit rules, etc.)
- Add new sections ONLY when a genuinely new major component exists (e.g. a new agent class, \
  a new pipeline stage, a new top-level subsystem like Bybit integration). Match the existing \
  card/section visual style exactly when adding.
- Remove sections only when a component was completely removed from the source

Output format:
- Return ONLY the complete updated HTML — no explanation, no markdown fences, no preamble
- CRITICAL: Your response must begin with the exact characters `<!DOCTYPE html>` — \
  nothing before it, not even a single space or newline. Any text before `<!DOCTYPE html>` \
  will corrupt the page."""


def read_source_files() -> dict[str, str]:
    """Collect source files from the trading-bot and dashboard repos."""
    files: dict[str, str] = {}

    bot_path = Path(os.environ.get("FLOWTRADER_SOURCE_PATH", "flowtrader-source"))
    if not bot_path.exists():
        print(f"ERROR: trading-bot source path not found: {bot_path}", file=sys.stderr)
        sys.exit(1)

    for rel in SOURCE_FILES:
        p = bot_path / rel
        if p.exists():
            files[rel] = p.read_text(encoding="utf-8")
        else:
            print(f"  [warn] {rel} not found in trading-bot", file=sys.stderr)

    dash_path_env = os.environ.get("FLOWTRADER_DASHBOARD_PATH")
    if dash_path_env:
        dash_path = Path(dash_path_env)
        for rel in DASHBOARD_FILES:
            p = dash_path / rel
            if p.exists():
                files[f"flowtrader-dashboard/{rel}"] = p.read_text(encoding="utf-8")
            else:
                print(f"  [warn] flowtrader-dashboard/{rel} not found", file=sys.stderr)

    return files


def compute_hash(files: dict) -> str:
    combined = json.dumps(files, sort_keys=True)
    return hashlib.sha256(combined.encode()).hexdigest()


# ── Version bump + footer stamp ──────────────────────────────────────────────
# Match the literal v1, v1.0, v1.12 etc. AND the {{VERSION}} placeholder that
# the system prompt instructs Claude to leave in place. Same for the footer
# date.
HERO_VERSION_RE   = re.compile(r"<h1>Flow<span>Trader</span>\s*(?:v[\d.]+|\{\{VERSION\}\})</h1>")
ANY_VERSION_LABEL = re.compile(r"FlowTrader\s+(?:v[\d.]+|\{\{VERSION\}\})")
EXISTING_VERSION  = re.compile(r"FlowTrader\s+v([\d.]+)")
FOOTER_DATE_RE    = re.compile(r"<span>Updated\s+(?:\d{4}-\d{2}-\d{2}|\{\{UPDATED\}\})</span>")


def current_version(html: str) -> str:
    """Read the version that's currently displayed on the page (any 'FlowTrader vX' instance)."""
    m = EXISTING_VERSION.search(html)
    return m.group(1) if m else "1.0"


def bump_version(v: str) -> str:
    """v1 → v1.1, v1.1 → v1.2, ... — only minor bumps automatically."""
    parts = v.split(".")
    if len(parts) == 1:
        return f"{parts[0]}.1"
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def stamp(html: str, new_version: str) -> str:
    """Inject the new version + today's date everywhere they appear on the page.

    Three sites: the <h1> hero, the nav brand, and the footer brand. Each
    site can be either a literal 'FlowTrader v1.2' or the '{{VERSION}}' /
    '{{UPDATED}}' placeholder from the system prompt — handle both.
    """
    today = date.today().isoformat()

    html = HERO_VERSION_RE.sub(
        f"<h1>Flow<span>Trader</span> v{new_version}</h1>", html
    )
    html = ANY_VERSION_LABEL.sub(f"FlowTrader v{new_version}", html)
    html = FOOTER_DATE_RE.sub(f"<span>Updated {today}</span>", html)

    return html


# ── Generation ───────────────────────────────────────────────────────────────
def regenerate(files: dict, current_html: str) -> str:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    files_block = "\n\n".join(
        f"=== {path} ===\n{content}" for path, content in files.items()
    )
    user_content = (
        f"CURRENT DASHBOARD HTML:\n{current_html}\n\n"
        f"CURRENT SOURCE CODE:\n{files_block}\n\n"
        "Update the dashboard to accurately reflect the current source. "
        "Return the complete HTML."
    )
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=48000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    ) as stream:
        for _ in stream.text_stream:
            pass
        final = stream.get_final_message()

    if final.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claude hit max_tokens — output truncated. "
            "Bump max_tokens or split the prompt."
        )
    text = final.content[0].text
    if not text.rstrip().endswith("</html>"):
        raise RuntimeError(
            "Generated HTML does not end with </html> — likely truncated. "
            "Refusing to overwrite index.html."
        )
    return text


# ── Archive previous version ─────────────────────────────────────────────────
def archive_previous(current_html: str, old_version: str) -> Path:
    ARCHIVE_DIR.mkdir(exist_ok=True)
    dest = ARCHIVE_DIR / f"index-v{old_version}.html"
    # If a file already exists for this version, suffix with the date so we
    # never silently overwrite an archive entry.
    if dest.exists():
        dest = ARCHIVE_DIR / f"index-v{old_version}-{date.today().isoformat()}.html"
    dest.write_text(current_html, encoding="utf-8")
    return dest


def main():
    print("Reading source files...")
    files = read_source_files()
    print(f"  Read {len(files)} file(s)")

    new_hash = compute_hash(files)
    old_hash = HASH_FILE.read_text().strip() if HASH_FILE.exists() else None

    # Allow a manual force-regenerate via env var so the user can bump the
    # version even when the source hash is unchanged (e.g. fixing a typo
    # spotted in the dashboard itself).
    forced = os.environ.get("FLOWTRADER_FORCE_REGEN", "").lower() in ("1", "true", "yes")

    if new_hash == old_hash and not forced:
        print("No changes detected — dashboard is up to date.")
        return

    old_short = old_hash[:8] if old_hash else "none"
    print(f"Changes detected ({old_short} -> {new_hash[:8]}). Calling Claude...")

    current_html = INDEX_FILE.read_text(encoding="utf-8")
    old_version  = current_version(current_html)
    new_version  = bump_version(old_version)
    print(f"Version: v{old_version} -> v{new_version}")

    updated_html = regenerate(files, current_html)
    updated_html = stamp(updated_html, new_version)

    archive_path = archive_previous(current_html, old_version)
    print(f"Archived previous version to {archive_path}")

    INDEX_FILE.write_text(updated_html, encoding="utf-8")
    HASH_FILE.write_text(new_hash)
    print(f"Dashboard updated to v{new_version}.")


if __name__ == "__main__":
    main()
