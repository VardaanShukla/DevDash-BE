"""Readable prompts. Claude app sessions hand Claude Code the user's message wrapped in
routing markup (<wake>, <relay>, <cited>); the raw text is stored as is, and the person's
own words are pulled out only for display."""

import html
import re

_PATTERNS = [
    # The person's own message, quoted by the app.
    re.compile(r'<cited\b[^>]*\bauthor="user"[^>]*>(.*?)</cited>', re.S),
    # The message that woke the session, before any others quoted for context.
    re.compile(r'<message\b(?=[^>]*\bfrom="human")(?=[^>]*\btrigger="true")[^>]*>(.*?)</message>', re.S),
    re.compile(r'<message\b[^>]*\bfrom="human"[^>]*>(.*?)</message>', re.S),
    # A note from the app's coordinator, which paraphrases the request.
    re.compile(r"<note>(.*?)</note>", re.S),
]
_TAG = re.compile(r"<[^>]+>")
# Harness text around the request, never the person's.
_HARNESS = re.compile(r"<(system-reminder|system-note)\b[^>]*>.*?</\1>", re.S)
# Files attached to the message, which Claude Code puts before it as @"path".
_ATTACHMENTS = re.compile(r'^(?:\s*@"[^"]*")+\s*')
# Claude Code's word that a background command finished: a prompt nobody typed.
_TASK_NOTIFICATION = re.compile(r"^\s*<task-notification>.*</task-notification>\s*$", re.S)
FOLLOW_UP = "(follow-up after a background task finished)"


def is_task_notification(prompt: str) -> bool:
    return bool(_TASK_NOTIFICATION.match(prompt))


def readable(prompt: str) -> str:
    if is_task_notification(prompt):
        return FOLLOW_UP
    text = _ATTACHMENTS.sub("", prompt)
    if not text.lstrip().startswith("<"):
        return text if text.strip() else prompt
    text = _HARNESS.sub("", text)
    for pattern in _PATTERNS:
        parts = [_clean(m) for m in pattern.findall(text)]
        parts = [p for p in parts if p]
        if parts:
            return "\n\n".join(parts)
    return _clean(text) or prompt


def _clean(text: str) -> str:
    return html.unescape(_TAG.sub("", text)).strip()
