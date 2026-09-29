# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Attention & accommodation tools.

``codepost_course_todo`` is composed client-side on purpose: the platform
``/dashboard/`` endpoints are ``IsAdminUser`` (platform staff) and unusable by
instructors, so "what needs my attention" is assembled from the course-level
reads an admin can actually make.
"""
from __future__ import annotations

import datetime

from core.agent import errors, shaping
from core.agent.registry import SCOPE_READ, SCOPE_WRITE, tool
from core.agent.tools._common import (course_header, load_assignments,
                                      resolve_student)
from core.permissions.capabilities import Capability


@tool(
    name='codepost_course_todo',
    title='What needs attention',
    description=(
        'A composed to-do view of the course: upcoming deadlines, grading debt '
        'per assignment, open regrade requests, quizzes waiting on manual '
        'grading, and broken autograder builds. Counts and ids only — drill in '
        'with the specific tools it names.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'horizonDays': {
                'type': 'integer', 'default': 7,
                'description': 'How far ahead to look for deadlines.',
            },
        },
        'additionalProperties': False,
    },
    capability=Capability.VIEW_ANALYTICS,
    min_scope=SCOPE_READ,
    read_only=True,
)
def course_todo(ctx, horizonDays: int = 7):
    from core.views.assignment import AssignmentViewSet
    from core.views.course import CourseViewSet

    now = datetime.datetime.now(datetime.timezone.utc)
    horizon = now + datetime.timedelta(days=max(1, int(horizonDays)))

    assignments = load_assignments(ctx)
    deadlines, grading_debt, regrades, broken_builds = [], [], [], []

    for a in assignments.values():
        aid, name = a.get('id'), a.get('name')

        due = _parse_dt(a.get('uploadDueDate'))
        if due and now <= due <= horizon:
            deadlines.append({'assignmentId': aid, 'name': name,
                              'kind': 'uploadDueDate', 'at': a.get('uploadDueDate')})
        regrade_due = _parse_dt(a.get('regradeDeadline'))
        if regrade_due and now <= regrade_due <= horizon:
            deadlines.append({'assignmentId': aid, 'name': name,
                              'kind': 'regradeDeadline', 'at': a.get('regradeDeadline')})

        total = a.get('submissionsCount') or 0
        finalized = a.get('submissionsFinalizedCount') or 0
        unclaimed = a.get('submissionsUnclaimedCount') or 0
        if a.get('state') in ('published', 'closed') and total and finalized < total:
            grading_debt.append({
                'assignmentId': aid, 'name': name,
                'unfinalized': total - finalized, 'unclaimed': unclaimed,
                'hint': f'codepost_list_submissions(assignmentId={aid}, '
                        f'status="unfinalized")'})

        # Open regrades: one compact scan per published assignment. Bounded by
        # assignment count; each scan is the same call the web UI makes.
        if a.get('state') in ('published', 'closed'):
            result = ctx.dispatch.call(
                AssignmentViewSet, {'get': 'submissions'},
                method='GET', path=f'/assignments/{aid}/submissions/',
                query='compact=1', pk=aid)
            if result.ok and isinstance(result.data, list):
                open_ids = [r['id'] for r in result.data if r.get('questionIsOpen')]
                if open_ids:
                    regrades.append({
                        'assignmentId': aid, 'name': name, 'open': len(open_ids),
                        'hint': f'codepost_manage_regrades(op="list", '
                                f'assignmentId={aid})'})

    quizzes_needing_grading = []
    quiz_result = ctx.dispatch.call(
        CourseViewSet, {'get': 'quizzes'},
        method='GET', path=f'/courses/{ctx.course.id}/quizzes/', pk=ctx.course.id)
    if quiz_result.ok:
        from core.agent.tools.quizzes import needs_grading_count
        for quiz in (quiz_result.data or []):
            # needsGrading is not a serializer field — it must be computed
            # from the quiz's results (one dispatch per published quiz).
            if not quiz.get('isPublished'):
                continue
            pending = needs_grading_count(ctx, quiz.get('id'))
            if pending:
                quizzes_needing_grading.append({
                    'quizId': quiz.get('id'), 'title': quiz.get('title'),
                    'needsGrading': pending,
                    'hint': f'codepost_get_quiz_status(view="results", '
                            f'quizId={quiz.get("id")})'})

    # Broken autograder builds (build_status choices: 3 = Failed)
    for a in assignments.values():
        env_id = a.get('environment')
        if not env_id:
            continue
        from autograder.views.environment import EnvironmentViewSet
        env = ctx.dispatch.call(
            EnvironmentViewSet, {'get': 'retrieve'},
            method='GET', path=f'/autograder/environments/{env_id}/', pk=env_id)
        if env.ok and env.data.get('buildStatus') == 3:
            broken_builds.append({'assignmentId': a.get('id'), 'name': a.get('name'),
                                  'environmentId': env_id,
                                  'hint': f'codepost_run_autograder(op="status", '
                                          f'assignmentId={a.get("id")})'})

    data = {
        'course': course_header(ctx.course),
        'horizonDays': horizonDays,
        'deadlines': sorted(deadlines, key=lambda d: d['at'] or ''),
        'gradingDebt': grading_debt,
        'openRegrades': regrades,
        'quizzesNeedingGrading': quizzes_needing_grading,
        'brokenAutograderBuilds': broken_builds,
    }
    quiet = not any((deadlines, grading_debt, regrades, quizzes_needing_grading,
                     broken_builds))
    return shaping.enforce_budget(shaping.envelope(
        data, meta={'note': 'Nothing needs attention.'} if quiet else {}))


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


@tool(
    name='codepost_manage_regrades',
    title='Regrade requests',
    description=(
        "List and answer students' regrade requests on an assignment.\n\n"
        "op='list' shows the open requests with each student's question. "
        "op='respond' writes the reply — IMPORTANT: the student cannot see the "
        "reply until the request is closed (close=true publishes it; close=false "
        'saves a hidden draft). Always show the user the reply text before '
        'sending with close=true.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'op': {'enum': ['list', 'respond'], 'default': 'list'},
            'assignmentId': {'type': 'integer',
                             'description': "Required for op='list'."},
            'submissionId': {'type': 'integer',
                             'description': "Required for op='respond'."},
            'response': {'type': 'string',
                         'description': 'The reply text (respond only).'},
            'close': {
                'type': 'boolean', 'default': False,
                'description': 'true publishes the reply and closes the request; '
                               'false saves a draft the student cannot see yet.'},
        },
        'required': [],
        'additionalProperties': False,
    },
    capability=Capability.MANAGE_REGRADES,
    min_scope=SCOPE_WRITE, tier=1,
    read_only=False, destructive=False, idempotent=True,
)
def manage_regrades(ctx, op: str = 'list', assignmentId=None, submissionId=None,
                    response: str = '', close: bool = False):
    from core.views.assignment import AssignmentViewSet
    from core.views.submission import SubmissionViewSet

    if op == 'list':
        if assignmentId is None:
            raise errors.ToolError(
                'PRECONDITION_NOT_MET', "op='list' needs an assignmentId.",
                remedy='Get one from codepost_get_course_overview.', retryable=True)
        rows = ctx.dispatch.require(
            AssignmentViewSet, {'get': 'submissions'},
            method='GET', path=f'/assignments/{assignmentId}/submissions/',
            query='compact=1', pk=assignmentId,
            what=f'listing submissions for assignment {assignmentId}')
        open_rows = [
            shaping.project(r, ('id', 'students', 'grader', 'grade',
                                'questionText', 'questionIsRegrade',
                                'questionDate', 'questionResponder'))
            for r in (rows or []) if r.get('questionIsOpen')]
        return shaping.enforce_budget(shaping.envelope(
            {'course': course_header(ctx.course),
             'assignmentId': assignmentId,
             'openRequests': open_rows},
            meta={'total': len(open_rows)},
            warnings=(None if open_rows else ['No open regrade requests.'])))

    # op == 'respond'
    if submissionId is None or not response:
        raise errors.ToolError(
            'PRECONDITION_NOT_MET',
            "op='respond' needs submissionId and a non-empty response.",
            remedy="List the open requests first with op='list'.", retryable=True)

    body = {'questionResponse': response, 'questionIsOpen': not close}
    ctx.dispatch.require(
        SubmissionViewSet, {'patch': 'partial_update'},
        method='PATCH', path=f'/submissions/{submissionId}/', data=body,
        pk=submissionId, what=f'responding to regrade on submission {submissionId}')

    # One serializer path (staff who is also the submission's student) returns
    # 200 with the body unchanged — verify the write actually landed.
    check = ctx.dispatch.require(
        SubmissionViewSet, {'get': 'retrieve'},
        method='GET', path=f'/submissions/{submissionId}/', pk=submissionId,
        what=f'verifying the regrade response on submission {submissionId}')
    if check.get('questionResponse') != response:
        raise errors.ToolError(
            'PRECONDITION_NOT_MET',
            'The response did not persist — this credential cannot answer '
            'regrades on this submission (it may be your own).',
            remedy='Report this to the user.')

    return shaping.envelope(
        {'course': course_header(ctx.course),
         'submissionId': submissionId,
         'closed': close,
         'studentCanSeeReply': close},
        warnings=(None if close else
                  ['Saved as a DRAFT — the student cannot see the reply until '
                   'you respond again with close=true.']))


@tool(
    name='codepost_set_quiz_accommodation',
    title='Quiz accommodations',
    description=(
        'Per-student quiz accommodations for this course: a time multiplier '
        'applied to every timed quiz (1.5 turns 40 minutes into 60) and/or a '
        'Safe Exam Browser exemption. Applies course-wide, takes effect '
        'immediately (even mid-attempt), and does NOT move quiz close times.\n\n'
        "op='list' shows current accommodations. op='set' creates or updates "
        "one. To revoke, set timeMultiplier=1 with sebExempt=false — that "
        'removes the accommodation entirely.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'op': {'enum': ['list', 'set'], 'default': 'list'},
            'student': {'type': 'string',
                        'description': "The student's alias (from codepost_get_roster) "
                                       "or email; must be on the roster."},
            'timeMultiplier': {'type': 'number',
                               'description': 'At least 1. 1.5 = time-and-a-half.'},
            'sebExempt': {'type': 'boolean',
                          'description': 'Exempt from Safe Exam Browser. Omit to '
                                         'keep the current value.'},
        },
        'required': [],
        'additionalProperties': False,
    },
    capability=Capability.GRADE_QUIZ,
    min_scope=SCOPE_WRITE, tier=1,
    read_only=False, destructive=False, idempotent=True,
)
def set_quiz_accommodation(ctx, op: str = 'list', student: str = '',
                           timeMultiplier=None, sebExempt=None):
    from core.views.course import CourseViewSet

    if op == 'list':
        rows = ctx.dispatch.require(
            CourseViewSet, {'get': 'quizAccommodations'},
            method='GET', path=f'/courses/{ctx.course.id}/quizAccommodations/',
            pk=ctx.course.id, what='listing quiz accommodations')
        return shaping.enforce_budget(shaping.envelope(
            {'course': course_header(ctx.course),
             'accommodations': rows or []},
            meta={'total': len(rows or [])}))

    if not student or timeMultiplier is None:
        raise errors.ToolError(
            'PRECONDITION_NOT_MET',
            "op='set' needs student (alias) and timeMultiplier.",
            remedy='Use codepost_get_roster to find the alias.',
            retryable=True)

    student = resolve_student(ctx, student)
    body = {'student': student, 'timeMultiplier': timeMultiplier}
    if sebExempt is not None:
        body['sebExempt'] = sebExempt
    ctx.dispatch.require(
        CourseViewSet, {'patch': 'setQuizAccommodation'},
        method='PATCH', path=f'/courses/{ctx.course.id}/setQuizAccommodation/',
        data=body, pk=ctx.course.id,
        what=f'setting a quiz accommodation for {student}')

    # A multiplier of 1 with sebExempt falsy DELETES the row server-side (the
    # only revoke path — there is no DELETE endpoint), and the 200 echoes the
    # values regardless. Re-read so the answer reflects what is stored.
    rows = ctx.dispatch.require(
        CourseViewSet, {'get': 'quizAccommodations'},
        method='GET', path=f'/courses/{ctx.course.id}/quizAccommodations/',
        pk=ctx.course.id, what='re-reading quiz accommodations')
    current = next((r for r in (rows or []) if r.get('student') == student), None)

    revoked = current is None
    return shaping.envelope(
        {'course': course_header(ctx.course),
         'student': student,
         'accommodation': current,
         'revoked': revoked},
        warnings=(['Accommodation removed (timeMultiplier 1 with no SEB '
                   'exemption stores nothing).'] if revoked else
                  ['Applies to every timed quiz in this course, including '
                   'attempts already in progress. Quiz close times are NOT '
                   'extended.']))
