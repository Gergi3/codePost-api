# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Write-tool guardrails.

Tiers (enforced by tools; scope is enforced by the registry before any of this):

- Tier 0 — reversible, staff-visible only. ``dryRun`` available, defaults False.
- Tier 1 — student-visible but recoverable. ``dryRun`` defaults **True**: the
  first call is always a preview, and re-issuing with ``dryRun=false`` costs the
  model one turn. The preview is the guardrail.
- Tier 2 — irreversible-ish or wide blast radius. Preview first, then the real
  call must carry the ``confirmToken`` the preview returned. The token is an
  HMAC over the *plan*, so it cannot be fabricated by the model, expires in
  5 minutes, and dies if the plan changes between preview and confirm (a TA
  finalizing more submissions mid-flow forces a re-preview).

A plain ``confirm: true`` boolean would be useless at any tier — a model that
sees it in the schema sets it on the first call, every time.

Stateless on purpose: HMAC + TTL works identically across every worker with no
session and no Redis. (Tier 3 — a real human decision for deletes/resets/mass
email, via the in-chat approval dialog or the dashboard — is the section at
the bottom of this module.)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta as datetime_timedelta

from django.conf import settings

from core.agent import errors

TOKEN_TTL_SECONDS = 300


def _canonical(tool: str, args: dict, plan_hash: str, course_id: int, user_id: int,
               issued_at: int) -> bytes:
    # confirmToken/dryRun are stripped so the token binds the operation, not
    # the confirmation ceremony around it.
    clean = {k: v for k, v in sorted(args.items())
             if k not in ('confirmToken', 'dryRun')}
    return json.dumps([tool, clean, plan_hash, course_id, user_id, issued_at],
                      sort_keys=True, separators=(',', ':'), default=str).encode()


def plan_hash(plan: dict) -> str:
    """Fingerprint of the computed plan (recipient counts, cascade sizes, …)."""
    raw = json.dumps(plan, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def issue_token(tool: str, args: dict, plan: dict, *, course_id: int,
                user_id: int) -> dict:
    issued_at = int(time.time())
    digest = hmac.new(
        settings.SECRET_KEY.encode(),
        _canonical(tool, args, plan_hash(plan), course_id, user_id, issued_at),
        hashlib.sha256,
    ).hexdigest()[:32]
    return {
        'confirmToken': f'{issued_at}.{digest}',
        'confirmTokenExpiresInSeconds': TOKEN_TTL_SECONDS,
    }


def verify_token(token: str, tool: str, args: dict, plan: dict, *, course_id: int,
                 user_id: int) -> None:
    """Raise a ToolError unless *token* matches this exact plan, fresh."""
    try:
        issued_str, digest = token.split('.', 1)
        issued_at = int(issued_str)
    except (AttributeError, ValueError):
        raise errors.ToolError(
            'CONFIRM_TOKEN_STALE', 'The confirmToken is malformed.',
            remedy='Re-run with dryRun=true to get a fresh preview and token.',
            retryable=True)

    if time.time() - issued_at > TOKEN_TTL_SECONDS:
        raise errors.ToolError(
            'CONFIRM_TOKEN_STALE', 'The confirmToken has expired.',
            remedy='Re-run with dryRun=true to get a fresh preview and token.',
            retryable=True)

    expected = hmac.new(
        settings.SECRET_KEY.encode(),
        _canonical(tool, args, plan_hash(plan), course_id, user_id, issued_at),
        hashlib.sha256,
    ).hexdigest()[:32]
    if not hmac.compare_digest(digest, expected):
        raise errors.ToolError(
            'CONFIRM_TOKEN_STALE',
            'The plan changed since the preview (or the token does not match '
            'this operation).',
            remedy='Re-run with dryRun=true, review the new preview, and confirm '
                   'with the fresh token.',
            retryable=True)


def confirmation_required(tool: str, args: dict, plan: dict, *, course_id: int,
                          user_id: int, message: str) -> errors.ToolError:
    """The error a Tier-2 tool returns from its preview: the plan plus a token.

    Returned inline so the confirm retry is one turn, not two.
    """
    token = issue_token(tool, args, plan, course_id=course_id, user_id=user_id)
    return errors.ToolError(
        'CONFIRMATION_REQUIRED', message,
        remedy='Review the plan with the user, then re-call with dryRun=false '
               'and this confirmToken.',
        retryable=True,
        context={'plan': plan, **token})


# ---------------------------------------------------------------------------
# Tier 3 — human confirmation (in-chat dialog, dashboard fallback)
# ---------------------------------------------------------------------------
# The HMAC tokens above are a race guard, not a human gate: the server issues
# them and the agent consumes them, so an agent can do preview→confirm alone.
# Tier-3 operations (unrecoverable destruction, mass email) require a real
# human decision through a channel the model cannot answer:
#
# - **Elicitation** (primary): the MCP client shows a native Approve/Decline
#   dialog to the human mid-call. The client answers on the human's behalf;
#   the model never sees the dialog, so prompt injection cannot approve it.
# - **Dashboard** (fallback, clients without elicitation): a PendingAgentAction
#   row with Approve/Deny buttons in Course Settings — endpoints that reject
#   course-scoped credentials — and the agent's retry observes the decision.


def _args_hash(tool: str, args: dict) -> str:
    clean = {k: v for k, v in sorted(args.items())
             if k not in ('confirmationCode', 'confirmToken', 'dryRun')}
    raw = json.dumps([tool, clean], sort_keys=True, separators=(',', ':'),
                     default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def require_human_confirmation(tool: str, args: dict, plan: dict, *, ctx,
                               message: str) -> None:
    """Block until a human has approved this exact operation, or raise.

    Returns normally only when approval is in hand — via the in-chat dialog
    (same call) or a previously granted dashboard approval (this retry
    consumes it). Every other outcome raises a ToolError.
    """
    if ctx.elicit_channel is not None:
        _confirm_via_elicitation(tool, plan, ctx=ctx, message=message)
        return
    _confirm_via_dashboard(tool, args, plan, ctx=ctx, message=message)


def _confirm_via_elicitation(tool: str, plan: dict, *, ctx, message: str) -> None:
    # The dialog travels through the MCP client, which may log or show it to
    # the model — so it gets the same student scrub as a tool result. The hash
    # and the audit row keep the raw plan.
    from core.agent.privacy import StudentAliasMap
    safe = StudentAliasMap.for_course(ctx.course)
    dialog = (f'{safe.scrub(message)}\n\nPlan:\n'
              f'{json.dumps(safe.scrub(plan), indent=2, default=str)[:1500]}\n\n'
              'Approve to let the agent proceed; decline to cancel.')
    result = ctx.elicit_channel.elicit(
        message=dialog,
        # Accept/decline IS the answer — no form fields to fill in.
        requested_schema={'type': 'object', 'properties': {}})

    approved = result.get('action') == 'accept'
    _audit_confirmation(ctx, tool, plan, approved=approved, origin='elicitation')
    if not approved:
        raise errors.ToolError(
            'CONFIRMATION_DENIED',
            'The user declined this action in the approval dialog. Do not '
            'retry; report the decision.',
            retryable=False)


def _confirm_via_dashboard(tool: str, args: dict, plan: dict, *, ctx,
                           message: str) -> None:
    from django.utils import timezone

    from core.models import PendingAgentAction

    ah, ph = _args_hash(tool, args), plan_hash(plan)
    now = timezone.now()

    # A denial sticks until it expires — keyed on args alone, so plan drift
    # (a TA grading in the background) can't be used to dodge it and re-nag.
    denied = PendingAgentAction.objects.filter(
        course=ctx.course, tool=tool, args_hash=ah,
        denied_at__isnull=False, expires_at__gt=now).first()
    if denied is not None:
        raise errors.ToolError(
            'CONFIRMATION_DENIED',
            'The course admin denied this action from the dashboard. Do not '
            'retry or re-request it; report the denial to the user.',
            retryable=False,
            context={'expiresAt': denied.expires_at.isoformat()})

    action = PendingAgentAction.objects.filter(
        course=ctx.course, tool=tool, args_hash=ah, plan_hash=ph,
        denied_at=None, redeemed_at=None, expires_at__gt=now).first()

    invalidated = False
    if action is None:
        # The plan drifted out from under any older request for these args:
        # expire the stale row (kept for audit) so the dashboard never shows
        # two rows — or honours a superseded approval — for one operation.
        invalidated = bool(PendingAgentAction.objects.filter(
            course=ctx.course, tool=tool, args_hash=ah,
            denied_at=None, redeemed_at=None, expires_at__gt=now,
        ).exclude(plan_hash=ph).update(expires_at=now))
        action = PendingAgentAction.objects.create(
            course=ctx.course, tool=tool, args_hash=ah, plan_hash=ph,
            plan=plan,
            requested_by=ctx.user if getattr(ctx.user, 'pk', None) else None,
            expires_at=now + datetime_timedelta(
                minutes=PendingAgentAction.EXPIRY_MINUTES))

    if action.approved_at is not None:
        # Conditional UPDATE, not save(): two concurrent retries must not both
        # consume the approval (select_for_update is unreliable under SQLite —
        # see core/services/dataset_assignment.py).
        claimed = PendingAgentAction.objects.filter(
            pk=action.pk, redeemed_at=None, denied_at=None,
            expires_at__gt=now).update(redeemed_at=now)
        if claimed == 1:
            return
        raise errors.ToolError(
            'CONFIRMATION_REQUIRED',
            'The approval was already consumed by another call.',
            remedy='Re-call to request a fresh approval from the dashboard.',
            retryable=True)

    raise errors.ToolError(
        'CONFIRMATION_REQUIRED',
        message + (' (An earlier request for this operation was invalidated '
                   'because its blast radius changed.)' if invalidated else ''),
        remedy=('This needs the course admin\'s approval in the codePost '
                'dashboard. Give the user this link and ask them to click '
                'Approve under Pending agent actions, then re-call this tool '
                'with the same arguments: ' + _approve_url(ctx.course) +
                f' — the request expires in '
                f'{PendingAgentAction.EXPIRY_MINUTES} minutes.'),
        retryable=True,
        context={'plan': plan,
                 'approveUrl': _approve_url(ctx.course),
                 'expiresAt': action.expires_at.isoformat()})


def _approve_url(course) -> str:
    from urllib.parse import quote

    base = getattr(settings, 'CLIENT_URL', 'http://localhost:3000')
    return (f'{base}/admin/{quote(course.name, safe="")}/'
            f'{quote(course.period, safe="")}/settings?section=pending-agent-actions')


def _audit_confirmation(ctx, tool: str, plan: dict, *, approved: bool,
                        origin: str, action_id=None) -> None:
    """One audit row per human Tier-3 decision. Never fails the call."""
    try:
        from core.services.audit import record_audit_event
        meta = {'tool': tool, 'planHash': plan_hash(plan), 'origin': origin}
        if action_id is not None:
            meta['actionId'] = action_id
        record_audit_event(
            course=ctx.course,
            event_type='agent_action_approved' if approved
                       else 'agent_action_denied',
            user=ctx.user if getattr(ctx.user, 'pk', None) else None,
            meta=meta)
    except Exception:                                          # pragma: no cover
        pass
