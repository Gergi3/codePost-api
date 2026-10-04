# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Helpers shared by the tool modules."""
from __future__ import annotations

from typing import Any

from core.agent import errors

# The subset of the assignment serializer worth showing in a list. The full
# staff serializer is ~35 fields; almost none of them answer a question an
# instructor actually asks.
ASSIGNMENT_SUMMARY_FIELDS = (
    'id', 'name', 'points', 'state', 'effectiveState', 'feedbackStatus',
    'hideGrades', 'uploadDueDate', 'allowStudentUpload', 'publishAt',
    'releaseFeedbackAt', 'sortKey',
)

ASSIGNMENT_COUNT_FIELDS = (
    'submissionsCount', 'submissionsFinalizedCount', 'submissionsInprogressCount',
    'submissionsUnclaimedCount', 'submissionsMissingCount',
)


def course_header(course) -> dict:
    """Echoed by every tool so the agent always knows its blast radius."""
    return {
        'id': course.id,
        'name': course.name,
        'period': course.period,
        'archived': course.archived,
    }


def fetch_assignment(ctx, assignment_id: int) -> dict:
    """One assignment via the real viewset, scope-checked.

    Retrieve (not list) is what returns the statistics serializer for an
    admin, which is where the course-wide grading counts live —
    ``queueLength`` computes its finalized/unfinalized against the *calling*
    user and so reads 0 for a service account.
    """
    from core.views.assignment import AssignmentViewSet

    data = ctx.dispatch.require(
        AssignmentViewSet, {'get': 'retrieve'},
        method='GET', path=f'/assignments/{assignment_id}/', pk=assignment_id,
        what=f'reading assignment {assignment_id}')
    ctx.dispatch.assert_in_scope(data.get('course'),
                                 what=f'assignment {assignment_id}')
    return _camelize_stats(data)


# The statistics serializer emits its count fields in snake_case
# (submissions_count, stats_mean, …) — one of the documented camelCase leaks.
# Normalise here so every tool speaks one casing and a model never has to
# guess. (See AGENTS.md, "camelCase is not applied globally".)
_STATS_RENAMES = {
    'submissions_count': 'submissionsCount',
    'submissions_finalized_count': 'submissionsFinalizedCount',
    'submissions_inprogress_count': 'submissionsInprogressCount',
    'submissions_unclaimed_count': 'submissionsUnclaimedCount',
    'submissions_missing_count': 'submissionsMissingCount',
    'stats_max': 'statsMax',
    'stats_min': 'statsMin',
    'stats_mean': 'statsMean',
}


def _camelize_stats(data: dict) -> dict:
    return {_STATS_RENAMES.get(k, k): v for k, v in data.items()}


def load_assignments(ctx) -> dict[int, dict]:
    """Every assignment in the course, hydrated and cached for this call.

    The course serializer only returns assignment *ids* and there is no
    ``courses/{id}/assignments/`` action, so this is an N-way fan-out. Cached
    on the context because several tools need it and ids are stable.
    """
    if ctx._assignments is not None:
        return ctx._assignments

    from core.views.course import CourseViewSet

    course = ctx.dispatch.require(
        CourseViewSet, {'get': 'retrieve'},
        method='GET', path=f'/courses/{ctx.course.id}/', pk=ctx.course.id,
        what='reading the course')

    out: dict[int, dict] = {}
    for assignment_id in course.get('assignments') or []:
        try:
            out[int(assignment_id)] = fetch_assignment(ctx, assignment_id)
        except errors.ToolError:
            # One unreadable assignment shouldn't sink the whole overview.
            continue

    ctx._assignments = out
    return out


def load_roster(ctx) -> dict:
    """The course roster, cached.

    Also the only identity-resolution path: ``/users/{email}/`` cannot resolve
    a course from a User, so a course key gets 403 on every address but its
    own. Student lookups therefore happen here, not against the users API.
    """
    if ctx._roster is not None:
        return ctx._roster

    from core.views.course import CourseViewSet

    ctx._roster = ctx.dispatch.require(
        CourseViewSet, {'get': 'roster'},
        method='GET', path=f'/courses/{ctx.course.id}/roster/', pk=ctx.course.id,
        what='reading the roster')
    return ctx._roster


def alias_map(ctx):
    """The course's student alias map, built once per call."""
    if ctx._aliases is None:
        from core.agent.privacy import StudentAliasMap
        ctx._aliases = StudentAliasMap.for_course(ctx.course)
    return ctx._aliases


def unalias(ctx, ident: str) -> str:
    """alias → email. Anything else (a typed email, a brand-new address, a
    grader) passes through unchanged; an unknown alias is an error."""
    from core.agent import privacy

    if not privacy.is_alias(ident):
        return ident
    email = alias_map(ctx).unalias(ident)
    if email is None:
        raise errors.unknown_student(ident, [])
    return email


def resolve_student(ctx, ident: str) -> str:
    """Alias or email → roster email, with fuzzy candidates on a miss.

    The candidates are emails here; the outbound scrub turns them into
    aliases before the model sees them."""
    email = unalias(ctx, ident)
    roster = load_roster(ctx)
    students = [e for e in (roster.get('students') or [])]
    for e in students:
        if e.lower() == email.lower():
            return e

    needle = email.split('@')[0].lower()
    candidates = [e for e in students if needle in e.lower()][:5]
    raise errors.unknown_student(ident, candidates)


def camelize_roster(roster: dict) -> dict:
    """Normalise the snake_case keys the roster endpoint leaks.

    ``djangorestframework-camel-case`` is NOT installed globally, so this
    endpoint really does return ``inactive_students`` / ``not_activated``
    alongside camelCase siblings. A model shown those will guess the camelCase
    form and its next call will silently do nothing.
    """
    renames = {
        'inactive_students': 'inactiveStudents',
        'inactive_graders': 'inactiveGraders',
        'inactive_courseAdmins': 'inactiveCourseAdmins',
        'not_activated': 'notActivated',
    }
    # ``not_activated`` is a SerializerMethodField that returns a map object;
    # in-process dispatch never JSON-renders it, so materialise it here.
    return {renames.get(k, k): (list(v) if isinstance(v, map) else v)
            for k, v in roster.items()}
