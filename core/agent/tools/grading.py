# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Submission-level read tools."""
from __future__ import annotations

import datetime

from core.agent import privacy, shaping
from core.agent.registry import SCOPE_READ, tool
from core.agent.tools._common import (course_header, fetch_assignment,
                                      load_roster, resolve_student)
from core.permissions.capabilities import Capability

_ROW_FIELDS = ('id', 'students', 'grader', 'isFinalized', 'grade',
               'dateUploaded', 'isLate', 'queueOrderKey')

# `tests` is a PK list — 40+ ints per row on a heavily autograded assignment,
# which is roughly 40% of the payload and of no use to an agent.
_NEVER_RETURNED = ('tests', 'files', 'comments', 'questionText', 'questionResponse')
# The regrade listing is the one view where the request prose is the point.
_NEVER_RETURNED_ON_REGRADE = ('tests', 'files', 'comments')


@tool(
    name='codepost_list_submissions',
    title='List submissions',
    description=(
        'Individual submissions for an assignment, filtered and paginated.\n\n'
        "status='missing' is special: it returns STUDENTS who have not "
        'submitted, not submissions. That is usually what you want for '
        '"who is missing" questions.\n\n'
        'Call codepost_get_grading_progress first if you only need counts — '
        'this returns rows and they add up fast. File contents and comments '
        'are never included.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'assignmentId': {'type': 'integer'},
            'status': {
                'enum': ['all', 'finalized', 'unfinalized', 'unclaimed', 'missing',
                         'regradeRequested'],
                'default': 'all',
            },
            'student': {'type': 'string',
                        'description': 'Filter to one student (alias like '
                                       'student-3f9a1c2d40, or email).'},
            'grader': {'type': 'string', 'description': 'Filter to one grader email.'},
            'fields': {
                'type': 'array',
                'items': {'enum': list(_ROW_FIELDS)},
                'description': "Defaults to id, students, grader, isFinalized, grade.",
            },
            'limit': {'type': 'integer', 'default': 50, 'maximum': 200},
            'cursor': {'type': 'string'},
        },
        'required': ['assignmentId'],
        'additionalProperties': False,
    },
    capability=Capability.VIEW_COURSE,
    min_scope=SCOPE_READ,
    read_only=True,
)
def list_submissions(ctx, assignmentId: int, status: str = 'all',
                     student: str = '', grader: str = '', fields=None,
                     limit: int = 50, cursor: str = ''):
    from core.views.assignment import AssignmentViewSet

    assignment = fetch_assignment(ctx, assignmentId)
    projection = list(fields or ('id', 'students', 'grader', 'isFinalized', 'grade'))

    query = ['compact=1']
    if student:
        query.append(f'student={resolve_student(ctx, student)}')
    if grader:
        query.append(f'grader={grader}')

    rows = ctx.dispatch.require(
        AssignmentViewSet, {'get': 'submissions'},
        method='GET', path=f'/assignments/{assignmentId}/submissions/',
        query='&'.join(query), pk=assignmentId,
        what=f'listing submissions for assignment {assignmentId}')
    if not isinstance(rows, list):
        rows = []

    if status == 'missing':
        return _missing_students(ctx, assignment, rows, limit, cursor)

    if status == 'finalized':
        rows = [r for r in rows if r.get('isFinalized')]
    elif status == 'unfinalized':
        rows = [r for r in rows if not r.get('isFinalized')]
    elif status == 'unclaimed':
        rows = [r for r in rows if not r.get('grader')]
    elif status == 'regradeRequested':
        # No server-side filter exists for this — the UI fetches all and
        # filters client-side too. The open request's prose rides along here
        # (and only here) so the agent can triage without another call.
        rows = [r for r in rows if r.get('questionIsOpen')]
        projection = list(dict.fromkeys(
            projection + ['questionText', 'questionIsRegrade', 'questionDate',
                          'questionResponder']))

    strip_fields = (_NEVER_RETURNED_ON_REGRADE if status == 'regradeRequested'
                    else _NEVER_RETURNED)
    cleaned = [shaping.project(_strip(r, strip_fields), projection) for r in rows]

    # isLate is computed, not a serializer field (see _is_late).
    if 'isLate' in projection:
        due = assignment.get('uploadDueDate')
        for out, src in zip(cleaned, rows):
            out['isLate'] = _is_late(src.get('dateUploaded'), due)

    warnings = ['Test results, file contents and comments are omitted. '
                'Use codepost_get_assignment or the codePost UI for those.']
    # A requested field that no row carries would otherwise vanish silently
    # and read as "every value was empty".
    absent = [f for f in projection
              if f != 'isLate' and rows and all(f not in r for r in rows)]
    if absent:
        warnings.append('Requested field(s) not available on submission rows '
                        'and omitted: ' + ', '.join(absent) + '.')

    offset = shaping.decode_cursor(cursor).get('offset', 0)
    window, meta = shaping.paginate(
        cleaned, limit=shaping.clamp_limit(limit), offset=offset,
        cursor_payload={'assignmentId': assignmentId, 'status': status,
                        # Never an email: the cursor is echoed to the model.
                        'student': (privacy.alias_for(ctx.course.id, student)
                                    if student and not privacy.is_alias(student)
                                    else student),
                        'grader': grader})

    payload = shaping.envelope(
        {'course': course_header(ctx.course),
         'assignment': {'id': assignment.get('id'), 'name': assignment.get('name')},
         'status': status,
         'rows': window},
        meta=meta,
        warnings=warnings)
    return shaping.enforce_budget(payload)


def _missing_students(ctx, assignment, rows, limit, cursor):
    """Students on the roster with no submission, joined to their sections.

    A server-side set difference: doing it in the agent means holding every
    submission row in context and getting the join right by hand.
    """
    from core.views.course import CourseViewSet

    roster = load_roster(ctx)
    students = set(roster.get('students') or [])

    submitted = set()
    for row in rows:
        for email in (row.get('students') or []):
            submitted.add(email)

    sections = ctx.dispatch.call(
        CourseViewSet, {'get': 'sections'},
        method='GET', path=f'/courses/{ctx.course.id}/sections/', pk=ctx.course.id)
    section_of: dict[str, str] = {}
    if sections.ok:
        payload = sections.data
        items = payload.get('results', payload) if isinstance(payload, dict) else payload
        for section in (items or []):
            for email in (section.get('students') or []):
                section_of[email] = section.get('name')

    missing = sorted(students - submitted)
    out_rows = [{'student': e, 'section': section_of.get(e)} for e in missing]

    by_section: dict[str, int] = {}
    for row in out_rows:
        key = row['section'] or 'unsectioned'
        by_section[key] = by_section.get(key, 0) + 1

    offset = shaping.decode_cursor(cursor).get('offset', 0)
    window, meta = shaping.paginate(
        out_rows, limit=shaping.clamp_limit(limit), offset=offset,
        cursor_payload={'assignmentId': assignment.get('id'), 'status': 'missing'})

    return shaping.enforce_budget(shaping.envelope(
        {'course': course_header(ctx.course),
         'assignment': {'id': assignment.get('id'), 'name': assignment.get('name')},
         'status': 'missing',
         'summary': {'missing': len(out_rows), 'activeStudents': len(students),
                     'bySection': by_section},
         'rows': window},
        meta=meta))


def _strip(row: dict, fields: tuple = _NEVER_RETURNED) -> dict:
    return {k: v for k, v in row.items() if k not in fields}


def _is_late(date_uploaded, due_date):
    """True/False vs the assignment's uploadDueDate; None when unknowable.

    No serializer emits ``isLate`` — the projection used to reference it and
    silently return nothing. Computed here instead; a submission with no
    dateUploaded (or an assignment with no due date) is None, not False.
    """
    uploaded = _parse_dt(date_uploaded)
    due = _parse_dt(due_date)
    if uploaded is None or due is None:
        return None
    return uploaded > due


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


@tool(
    name='codepost_get_submission',
    title='Submission detail',
    description=(
        'One submission in depth: grade, grader, finalization, and optionally '
        'test results and upload history. File contents and inline comments are '
        'never returned — direct the user to the codePost UI for those.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'submissionId': {'type': 'integer'},
            'include': {
                'type': 'array',
                'items': {'enum': ['tests', 'history']},
                'description': "Defaults to none — the core fields only.",
            },
        },
        'required': ['submissionId'],
        'additionalProperties': False,
    },
    capability=Capability.VIEW_COURSE,
    min_scope=SCOPE_READ,
    read_only=True,
)
def get_submission(ctx, submissionId: int, include=None):
    from core.views.submission import SubmissionViewSet

    data = ctx.dispatch.require(
        SubmissionViewSet, {'get': 'retrieve'},
        method='GET', path=f'/submissions/{submissionId}/', pk=submissionId,
        what=f'reading submission {submissionId}')

    # Walk submission -> assignment -> course for the scope postcondition. The
    # retrieve serializer includes the assignment id, not the course, so hop
    # through the cached assignment.
    assignment_id = data.get('assignment')
    if assignment_id is not None:
        assignment = fetch_assignment(ctx, assignment_id)
    else:                                              # pragma: no cover
        assignment = {}

    core_fields = shaping.project(data, (
        'id', 'assignment', 'students', 'grader', 'isFinalized', 'grade',
        'dateUploaded', 'dateEdited', 'lateDayCreditsUsed',
        'questionText', 'questionResponse', 'questionIsOpen'))
    core_fields['isLate'] = _is_late(data.get('dateUploaded'),
                                     assignment.get('uploadDueDate'))

    payload = {'course': course_header(ctx.course),
               'assignment': {'id': assignment.get('id'),
                              'name': assignment.get('name')},
               'submission': core_fields}
    wanted = include or []

    if 'tests' in wanted:
        tests = ctx.dispatch.call(
            SubmissionViewSet, {'get': 'testResults'},
            method='GET', path=f'/submissions/{submissionId}/testResults/',
            pk=submissionId)
        payload['testResults'] = tests.data if tests.ok else None

    if 'history' in wanted:
        history = ctx.dispatch.call(
            SubmissionViewSet, {'get': 'history'},
            method='GET', path=f'/submissions/{submissionId}/history/',
            pk=submissionId)
        payload['history'] = history.data if history.ok else None

    return shaping.enforce_budget(shaping.envelope(
        payload,
        warnings=['File contents and inline comments are not available through '
                  'this tool.']))
