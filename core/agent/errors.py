# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""The single error envelope every agent tool returns on failure.

Raw DRF error bodies never reach the model: a bare 403 from `returnForbidden()`
is a dead end, and even `require_capability`'s message names a capability the
model has no way to interpret.  Each `ToolError` instead carries a stable
machine code, one sentence of prose, a concrete remedy, and — critically —
`retryable`, which is the flag that stops loops.  `retryable=False` means
"do not call this again with the same arguments; report to the user instead",
so those errors deliberately carry no retry hint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ToolError(Exception):
    """A tool failure the model should be able to recover from or report.

    Surfaced as an MCP result with `isError: true` — never as a JSON-RPC
    protocol error.  Protocol errors are reserved for malformed requests and
    unknown tools (see `core/mcp/protocol.py`).
    """

    def __init__(self, code: str, message: str, *, remedy: str = '',
                 retryable: bool = False, context: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.remedy = remedy
        self.retryable = retryable
        self.context = context or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'code': self.code,
            'message': self.message,
            'retryable': self.retryable,
        }
        # A retryable:false error is a dead end by construction — omitting the
        # remedy when there is nothing to retry keeps that unambiguous.
        if self.remedy:
            payload['remedy'] = self.remedy
        if self.context:
            payload['context'] = self.context
        return {'error': payload}


# --- Constructors for the codes that have a fixed shape ---------------------

def course_archived(course) -> ToolError:
    return ToolError(
        'COURSE_ARCHIVED',
        f"{course.name} ({course.period}) is archived; all modifications are blocked.",
        remedy='Ask a course admin to unarchive the course in the codePost web UI. '
               'Read tools still work.',
    )


def insufficient_key_scope(tool_name: str, required: str, actual: str) -> ToolError:
    return ToolError(
        'INSUFFICIENT_KEY_SCOPE',
        f"'{tool_name}' requires a '{required}' course API key; this key is '{actual}'.",
        remedy=f"No retry will change this. Tell the user they need to mint a new "
               f"'{required}'-scoped key in codePost under Course Settings → API Keys.",
        context={'requiredScope': required, 'actualScope': actual},
    )


def missing_capability(capability: str, description: str = '') -> ToolError:
    detail = f" — '{description}'" if description else ''
    return ToolError(
        'MISSING_CAPABILITY',
        f"This needs the '{capability}' capability{detail}. This credential does not grant it.",
        remedy='Report this to the user; it cannot be worked around from here.',
        context={'capability': capability},
    )


def unknown_student(email: str, candidates: list[str] | None = None) -> ToolError:
    return ToolError(
        'UNKNOWN_STUDENT',
        f"'{email}' is not on this course's roster.",
        remedy="Use codepost_get_roster with a search term to find the student's alias.",
        context={'candidates': candidates or []},
    )


def not_in_scope(obj_desc: str) -> ToolError:
    return ToolError(
        'NOT_IN_SCOPE',
        f'{obj_desc} does not belong to this course.',
        remedy='This credential is pinned to one course. Report this to the user.',
    )


def from_dispatch(result, *, what: str) -> ToolError:
    """Translate a non-2xx DispatchResult into a ToolError.

    DRF's own bodies are inconsistent (a bare string here, a field-error dict
    there), so this normalises them rather than passing them through.
    """
    status = result.status
    detail = _stringify(result.data)

    if status in (401, 403):
        return ToolError(
            'MISSING_CAPABILITY',
            f'Permission denied while {what}.' + (f' {detail}' if detail else ''),
            remedy='Report this to the user; it cannot be worked around from here.',
            context={'httpStatus': status},
        )
    if status == 404:
        return ToolError(
            'NOT_FOUND', f'Not found while {what}.' + (f' {detail}' if detail else ''),
            remedy='Re-check the id with codepost_get_course_overview.',
            context={'httpStatus': status},
        )
    if status == 429:
        return ToolError(
            'RATE_LIMITED', f'Rate limited while {what}.',
            remedy='Wait before retrying.', retryable=True,
            context={'httpStatus': status},
        )
    if 400 <= status < 500:
        return ToolError(
            'PRECONDITION_NOT_MET',
            f'Rejected while {what}.' + (f' {detail}' if detail else ''),
            remedy='Fix the arguments named above and call again.',
            context={'httpStatus': status},
        )
    return ToolError(
        'UPSTREAM_ERROR', f'codePost returned {status} while {what}.',
        remedy='Retry once; if it persists, report it to the user.', retryable=True,
        context={'httpStatus': status},
    )


def _stringify(data: Any) -> str:
    """Render a DRF error body as one short sentence."""
    if data is None:
        return ''
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        parts = []
        for key, value in list(data.items())[:5]:
            if isinstance(value, (list, tuple)):
                value = '; '.join(str(v) for v in value)
            parts.append(f'{key}: {value}')
        return ' '.join(parts)
    if isinstance(data, (list, tuple)):
        return '; '.join(str(v) for v in data[:5])
    return str(data)
