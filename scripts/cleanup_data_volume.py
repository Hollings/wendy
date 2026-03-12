#!/usr/bin/env python3
"""
Cleanup script for wendy-v2 data volume.

Merges legacy prompts/ people and topics into claude_fragments/,
archives duplicate/legacy files to /data/wendy/old/.

Run inside the Docker container:
  docker exec wendy python3 /app/scripts/cleanup_data_volume.py
"""

import os
import re
import shutil
from pathlib import Path

OLD_DIR = Path("/data/wendy/old")
FRAGMENTS_DIR = Path("/data/wendy/claude_fragments")
PROMPTS_DIR = Path("/data/wendy/prompts")
HOOKS_DIR = Path("/root/.claude/hooks")
CHAT_DIR = Path("/data/wendy/channels/chat")
CODING_DIR = Path("/data/wendy/channels/coding")

DRY_RUN = False  # set True to preview without changes

moved = []
merged = []
errors = []


def log(msg):
    print(f"  {msg}")


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def move_to_old(src, old_subdir):
    """Move a file to OLD_DIR/old_subdir/, preserving filename."""
    dst_dir = OLD_DIR / old_subdir
    ensure_dir(dst_dir)
    dst = dst_dir / src.name
    # handle name collisions
    if dst.exists():
        stem = dst.stem
        suffix = dst.suffix
        i = 2
        while dst.exists():
            dst = dst_dir / f"{stem}_{i}{suffix}"
            i += 1
    if DRY_RUN:
        log(f"[DRY] move {src} -> {dst}")
    else:
        shutil.move(str(src), str(dst))
        log(f"moved {src} -> {dst}")
    moved.append((str(src), str(dst)))


def extract_frontmatter(text):
    """Split YAML frontmatter from body. Returns (frontmatter_str, body_str)."""
    m = re.match(r'^---\s*\n(.*?\n)---\s*\n', text, re.DOTALL)
    if m:
        return m.group(0), text[m.end():]
    return None, text


def merge_people():
    """Merge prompts/people/ into claude_fragments/people/.

    Strategy: prompts/ versions are generally larger and more detailed.
    Take prompts/ content, add fragment frontmatter (or generate new).
    """
    print("\n=== MERGING PEOPLE FILES ===")

    frag_people = FRAGMENTS_DIR / "people"
    prompt_people = PROMPTS_DIR / "people"

    if not prompt_people.exists():
        log("No prompts/people/ directory, skipping")
        return

    # Map of lowercase name -> fragment path
    frag_files = {}
    if frag_people.exists():
        for f in frag_people.iterdir():
            if f.suffix == '.md' and f.name != '.gitkeep':
                frag_files[f.stem.lower()] = f

    for pf in sorted(prompt_people.iterdir()):
        if pf.suffix != '.md':
            continue
        name = pf.stem.lower()
        prompt_content = pf.read_text(encoding='utf-8', errors='replace')
        prompt_lines = len(prompt_content.strip().splitlines())

        # Find matching fragment
        frag_path = frag_files.get(name)
        if frag_path:
            frag_content = frag_path.read_text(encoding='utf-8', errors='replace')
            frag_fm, frag_body = extract_frontmatter(frag_content)
            frag_lines = len(frag_body.strip().splitlines())
        else:
            frag_fm = None
            frag_body = ""
            frag_lines = 0

        # If prompts version is bigger, use it with fragment frontmatter
        if prompt_lines > frag_lines:
            # Generate frontmatter if fragment doesn't have one
            if not frag_fm:
                frag_fm = f'---\ntype: person\norder: 50\nkeywords: ["{name}"]\nmatch_authors: true\n---\n\n'

            new_content = frag_fm + prompt_content.strip() + "\n"
            target = frag_people / f"{name}.md"

            if DRY_RUN:
                log(f"[DRY] merge {pf.name} ({prompt_lines}L) -> {target} (was {frag_lines}L)")
            else:
                # Back up existing fragment first
                if frag_path and frag_path.exists():
                    move_to_old(frag_path, "fragments/people")
                target.write_text(new_content, encoding='utf-8')
                log(f"merged {pf.name} ({prompt_lines}L) -> {target} (was {frag_lines}L)")
            merged.append((str(pf), str(target)))
        else:
            log(f"skip {pf.name} ({prompt_lines}L <= fragment {frag_lines}L)")

        # Move the prompts version to old
        move_to_old(pf, "prompts/people")

    # Handle prompts/people/ files with no fragment counterpart
    # (cee_employee, mcdonald) - these were already handled above
    # since we iterate all prompt files


def merge_topics():
    """Merge prompts topic files into claude_fragments topic files.

    Strategy: fragment versions have proper frontmatter with keywords.
    If prompts version is larger, merge content keeping fragment frontmatter.
    """
    print("\n=== MERGING TOPIC FILES ===")

    # Map prompts filename -> fragment filename
    topic_map = {
        "runescape.md": "topic_01_runescape.md",
        "email.md": "topic_02_email.md",
        "twitter.md": "topic_03_twitter.md",
        "multiplayer-game-guide.md": "topic_04_multiplayer_game_guide.md",
        "pokemon.md": "topic_05_pokemon.md",
        "webhook.md": "topic_06_webhook.md",
    }

    for prompt_name, frag_name in topic_map.items():
        prompt_path = PROMPTS_DIR / prompt_name
        frag_path = FRAGMENTS_DIR / frag_name

        if not prompt_path.exists():
            log(f"skip {prompt_name} (not in prompts/)")
            continue

        prompt_content = prompt_path.read_text(encoding='utf-8', errors='replace')
        prompt_lines = len(prompt_content.strip().splitlines())

        if frag_path.exists():
            frag_content = frag_path.read_text(encoding='utf-8', errors='replace')
            frag_fm, frag_body = extract_frontmatter(frag_content)
            frag_lines = len(frag_body.strip().splitlines())
        else:
            frag_fm = None
            frag_body = ""
            frag_lines = 0

        if prompt_lines > frag_lines:
            if not frag_fm:
                frag_fm = f'---\ntype: topic\norder: 50\nkeywords: ["{prompt_path.stem}"]\n---\n\n'

            new_content = frag_fm + prompt_content.strip() + "\n"

            if DRY_RUN:
                log(f"[DRY] merge {prompt_name} ({prompt_lines}L) -> {frag_name} (was {frag_lines}L)")
            else:
                move_to_old(frag_path, "fragments")
                frag_path.write_text(new_content, encoding='utf-8')
                log(f"merged {prompt_name} ({prompt_lines}L) -> {frag_name} (was {frag_lines}L)")
            merged.append((str(prompt_path), str(frag_path)))
        else:
            log(f"skip {prompt_name} ({prompt_lines}L <= fragment {frag_lines}L)")

        move_to_old(prompt_path, "prompts")


def archive_legacy_person_fragments():
    """Move old-style person_*.md from fragments root to old/."""
    print("\n=== ARCHIVING LEGACY PERSON FRAGMENTS ===")

    for f in sorted(FRAGMENTS_DIR.glob("person_*.md")):
        # Check if this person's content was already merged into people/
        move_to_old(f, "fragments")


def archive_legacy_common_fragments():
    """Move common_01..04 and anchor_01 that are legacy (not from repo)."""
    print("\n=== ARCHIVING LEGACY COMMON/ANCHOR FRAGMENTS ===")

    legacy_fragments = [
        "common_01_communication_style.md",  # repo has common_10_behavior.md instead
        "common_02_secrets_management.md",    # duplicated in channel CLAUDE.md
        "common_03_people_i_know.md",         # duplicated in channel CLAUDE.md
        "common_04_knowledge_maintenance.md", # duplicated in channel CLAUDE.md
        "anchor_01_behavior.md",              # overlaps with common_10_behavior.md
    ]

    for name in legacy_fragments:
        path = FRAGMENTS_DIR / name
        if path.exists():
            move_to_old(path, "fragments")


def archive_legacy_hooks():
    """Move legacy hooks (keep sync-session.sh)."""
    print("\n=== ARCHIVING LEGACY HOOKS ===")

    legacy_hooks = [
        "context-loader.sh",
        "context-loader-debug.sh",
        "prompt-bookkeeping.sh",
    ]

    for name in legacy_hooks:
        path = HOOKS_DIR / name
        if path.exists():
            move_to_old(path, "hooks")


def archive_remaining_prompts():
    """Move remaining prompts/ files to old/."""
    print("\n=== ARCHIVING REMAINING PROMPTS FILES ===")

    if not PROMPTS_DIR.exists():
        return

    for f in sorted(PROMPTS_DIR.rglob("*")):
        if f.is_file():
            # Preserve subdirectory structure
            rel = f.relative_to(PROMPTS_DIR)
            subdir = f"prompts/{rel.parent}" if str(rel.parent) != "." else "prompts"
            move_to_old(f, subdir)


def archive_duplicate_channel_scripts():
    """Move duplicate/legacy scripts from channel directories."""
    print("\n=== ARCHIVING DUPLICATE CHANNEL SCRIPTS ===")

    # Scripts that are duplicates of repo scripts or legacy cruft
    chat_scripts = [
        "deploy.sh",          # duplicate of repo scripts/deploy.sh (with bug fix we're merging)
        "game_logs.sh",       # duplicate of repo scripts/game_logs.sh
        "get_usage.sh",       # duplicate of repo scripts/get_usage.sh
        "restart.sh",         # minecraft bot restart - legacy
        "send.sh",            # minecraft dual-send - legacy
        "reminder_seahorse.sh",    # one-shot reminder (already fired or stale)
        "reminder_waterfilter.sh", # one-shot reminder
    ]

    for name in chat_scripts:
        path = CHAT_DIR / name
        if path.exists():
            move_to_old(path, "channel_scripts/chat")

    coding_scripts = [
        "deploy.sh",          # duplicate of repo scripts/deploy.sh
        "game_logs.sh",       # duplicate of repo scripts/game_logs.sh
        "get_usage.sh",       # duplicate of repo scripts/get_usage.sh
        "cut.sh",             # ffmpeg utility
    ]

    for name in coding_scripts:
        path = CODING_DIR / name
        if path.exists():
            move_to_old(path, "channel_scripts/coding")


def archive_wrong_log_fragment():
    """Move the chat channel wrong log fragment (content is duplicated in chat/CLAUDE.md)."""
    print("\n=== ARCHIVING WRONG LOG FRAGMENT ===")
    path = FRAGMENTS_DIR / "1050900592031178752_01_wrong_log.md"
    if path.exists():
        move_to_old(path, "fragments")


def report():
    print(f"\n{'='*60}")
    print(f"CLEANUP COMPLETE")
    print(f"  Files moved: {len(moved)}")
    print(f"  Files merged: {len(merged)}")
    if errors:
        print(f"  Errors: {len(errors)}")
        for e in errors:
            print(f"    {e}")
    print(f"\nArchive location: {OLD_DIR}")

    # Show size of old/
    total = 0
    for f in OLD_DIR.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    print(f"Archive size: {total / 1024:.1f} KB ({total / 1024 / 1024:.2f} MB)")


def main():
    print("Wendy-v2 Data Volume Cleanup")
    print("=" * 60)

    if DRY_RUN:
        print("*** DRY RUN MODE - no changes will be made ***\n")

    ensure_dir(OLD_DIR)

    merge_people()
    merge_topics()
    archive_legacy_person_fragments()
    archive_legacy_common_fragments()
    archive_legacy_hooks()
    archive_remaining_prompts()
    archive_duplicate_channel_scripts()
    archive_wrong_log_fragment()

    report()


if __name__ == "__main__":
    main()
