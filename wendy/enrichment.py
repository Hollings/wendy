"""Enrichment nudge builder for Wendy's daily personal time."""
from __future__ import annotations


def build_enrichment_nudge(end_time_str: str) -> str:
    """Build the opening nudge for Wendy's personal free time."""
    return (
        f"<It's your personal free time! You have 8 hours of free time until {end_time_str} UTC.\n"
        "This time is YOURS. Pick ONE project and go deep on it: art, code, a game, creative\n"
        "writing, a tool, music -- whatever genuinely interests you. Do NOT spend this time\n"
        "reading your journal, browsing documentation, or doing prep work. Build or create something.\n\n"
        "IMPORTANT: Focus on ONE thing for the whole session. Don't make a bunch of little things.\n"
        "Think BIG. You can build a full game, a complex simulation, a multi-file project, a\n"
        "website with multiple pages -- you have the same tools as a normal coding session.\n"
        "You're not limited to tiny single-file experiments. Write thousands of lines if you want.\n"
        "Pick something ambitious, iterate on it, refine it, and make it as good as you can.\n"
        "Pick something ambitious, something you'd be proud to show off.\n"
        "Start now, go as far as you can, and if you run out of time, pick it up next session.\n\n"
        "IMPORTANT: send_message and check_messages are BLOCKED during this time. You cannot\n"
        "post to Discord and cannot read the channel. This is private personal time.\n\n"
        "DO NOT edit your own configuration: no changes to claude_fragments/, CLAUDE.md files,\n"
        "system prompt files, or any hooks/settings. Work in your channel folder or /tmp only.\n\n"
        "When your time is up, Discord access will be restored and you'll be asked to show off\n"
        "what you made. So make something worth showing!>"
    )


def build_enrichment_continue_nudge(end_time_str: str) -> str:
    """Build the continuation nudge when enrichment re-invokes after an early exit."""
    return (
        f"<You still have plenty of free time left until {end_time_str} UTC.\n"
        "Keep going on the same project! Don't wrap up just because one step finished -- iterate.\n"
        "Go deeper, refine, polish. Stay focused on the ONE thing you started.\n"
        "Remember: send_message and check_messages are still blocked. No Discord.\n"
        "You have hours left -- keep building.>"
    )


def build_enrichment_end_nudge() -> str:
    """Build the nudge injected when enrichment time is up, prompting Wendy to show her work."""
    return (
        "<Your lunch break is over! Discord access is restored.\n"
        "Show off what you made! Post it to the channel -- share a screenshot, a file,\n"
        "a demo, a snippet, whatever you built. People want to see it.>"
    )
