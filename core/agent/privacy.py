# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Student pseudonymization at the agent boundary.

Everything a tool returns crosses to an external LLM provider, so student
identities (FERPA education records) must not. Every ``tools/call`` payload —
success or error — is scrubbed in ``core/mcp/protocol.py`` before it is
serialised, and every tool that accepts a student identifier unaliases it
through ``core/agent/tools/_common.py``.

An alias is stable per (course, email): the model can correlate a student
across calls and the instructor can resolve it later (``GET
/courses/{id}/agentAliases/``), but the HMAC cannot be reversed. The key is
derived from ``FIELD_ENCRYPTION_KEY`` — the one secret this deployment never
rotates (rotating it means re-encrypting the database), so aliases written
into an instructor's notes stay valid for the life of the course.

The map is built from the ORM on purpose. It is a privacy filter, not a data
path: nothing read here is returned to the caller, and it must work for every
credential regardless of VIEW_ROSTER — a dispatch-based build could 403 and
leave a successful payload unscrubbed.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Iterable

from django.conf import settings

ALIAS_PREFIX = 'student-'
ALIAS_HEX = 10
_ALIAS_RE = re.compile(r'^student-[0-9a-f]{10}$', re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-']+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
# A maximal identifier run: what a NetID or username looks like when it
# appears bare in prose, a log line or a filename (``mk1800_hw1.py``).
_IDENT_RE = re.compile(r'[A-Za-z0-9._+\-]+')
_IDENT_SPLIT_RE = re.compile(r'([._+\-])')


def _key() -> bytes:
    # Purpose-bound subkey: never use the Fernet key itself as an HMAC key.
    return hashlib.sha256(b'codepost.student-alias.v1:'
                          + settings.FIELD_ENCRYPTION_KEY.encode()).digest()


def alias_for(course_id: int, email: str) -> str:
    digest = hmac.new(_key(), f'{course_id}:{email.strip().lower()}'.encode(),
                      hashlib.sha256).hexdigest()
    return f'{ALIAS_PREFIX}{digest[:ALIAS_HEX]}'


def is_alias(value: Any) -> bool:
    return isinstance(value, str) and bool(_ALIAS_RE.match(value.strip()))


class StudentAliasMap:
    """Email ⇄ alias for one course, plus the bare identifiers to scrub."""

    def __init__(self, course_id: int, people: Iterable[tuple[str, str]]):
        """``people`` is an iterable of ``(email, username)`` pairs."""
        self.course_id = course_id
        self.by_email: dict[str, str] = {}
        self.by_alias: dict[str, list[str]] = {}
        self.by_token: dict[str, str] = {}
        self.username_of: dict[str, str] = {}
        for email, username in people:
            if not email:
                continue
            email = email.strip()
            lower = email.lower()
            alias = alias_for(course_id, lower)
            self.by_email[lower] = alias
            # Stored as-cased: unalias() feeds case-sensitive lookups.
            self.by_alias.setdefault(alias, []).append(email)
            self.username_of[lower] = username or ''
            # The local part (a NetID at Rutgers) and the Django username are
            # what a student is called when the address itself isn't written
            # out. Only whole values are tokens — never their dotted pieces.
            local = lower.split('@', 1)[0]
            for token in (local, (username or '').strip().lower()):
                if token and '@' not in token:
                    self.by_token[token] = alias

    @classmethod
    def for_course(cls, course) -> 'StudentAliasMap':
        from django.contrib.auth.models import User

        people = set(course.students.values_list('email', 'username'))
        people |= set(course.inactive_students.values_list('email', 'username'))
        # Students dropped from both lists whose work remains.
        people |= set(User.objects
                      .filter(student_submissions__assignment__course=course)
                      .values_list('email', 'username').distinct())
        return cls(course.id, people)

    # -- inbound ----------------------------------------------------------

    def unalias(self, ident: str) -> str | None:
        """alias → email as stored; anything else passes through unchanged;
        ``None`` for an alias that is unknown or (astronomically unlikely)
        ambiguous."""
        if not is_alias(ident):
            return ident
        hits = self.by_alias.get(ident.strip().lower(), [])
        return hits[0] if len(hits) == 1 else None

    # -- outbound ---------------------------------------------------------

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._scrub_str(value)
        if isinstance(value, dict):
            # Keys too: get_audit_log groupBy='user' keys its counts by email.
            return {(self._scrub_str(k) if isinstance(k, str) else k): self.scrub(v)
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(v) for v in value]
        return value

    def _scrub_str(self, text: str) -> str:
        if not self.by_email or not text:
            return text
        whole = text.strip().lower()
        if whole in self.by_email:
            return self.by_email[whole]
        if whole in self.by_token:
            return self.by_token[whole]
        if '@' in text:
            text = _EMAIL_RE.sub(
                lambda m: self.by_email.get(m.group(0).lower(), m.group(0)), text)
        return _IDENT_RE.sub(self._scrub_ident, text)

    def _scrub_ident(self, match) -> str:
        run = match.group(0)
        if _ALIAS_RE.match(run):
            return run                       # already an alias; never re-mangle
        hit = self.by_token.get(run.lower())
        if hit:
            return hit
        if not _IDENT_SPLIT_RE.search(run):
            return run
        # ``zq7712_hw1.py`` → ``student-…_hw1.py``: a NetID glued to a suffix.
        parts = _IDENT_SPLIT_RE.split(run)
        changed = False
        for i, part in enumerate(parts):
            alias = self.by_token.get(part.lower())
            if alias:
                parts[i] = alias
                changed = True
        return ''.join(parts) if changed else run
