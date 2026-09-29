# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Course-level read tools."""
from __future__ import annotations

from core.agent import shaping
from core.agent.registry import SCOPE_READ, tool
from core.agent.tools._common import (ASSIGNMENT_COUNT_FIELDS,
                                      ASSIGNMENT_SUMMARY_FIELDS, course_header,
                                      load_assignments)
from core.permissions.capabilities import (CAPABILITY_DESCRIPTIONS, Capability,
                                           compute_course_capabilities)


@tool(
    name='codepost_get_course_overview',
    title='Course overview',
    description=(
        'Orient yourself in the course. Returns the course settings, every '
        'assignment with its lifecycle state and grading counts, and the list '
        'of things this API key is allowed to do.\n\n'
        'CALL THIS FIRST. Assignment names cannot be looked up any other way — '
        'every other tool takes a numeric assignmentId that you get from here. '
        'There is no course id argument anywhere: the API key is pinned to one '
        'course.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'includeAssignmentCounts': {
                'type': 'boolean', 'default': True,
                'description': ('Include per-assignment submission counts. Costs two '
                                'extra queries per assignment; set false if you only '
                                'need names and states.'),
            },
        },
        'additionalProperties': False,
    },
    capability=Capability.VIEW_COURSE,
    min_scope=SCOPE_READ,
    read_only=True,
)
def get_course_overview(ctx, includeAssignmentCounts: bool = True):
    assignments = load_assignments(ctx)

    fields = ASSIGNMENT_SUMMARY_FIELDS + (
        ASSIGNMENT_COUNT_FIELDS if includeAssignmentCounts else ())
    rows = [shaping.project(a, fields) for a in assignments.values()]
    rows.sort(key=lambda r: (r.get('sortKey') or 0, r.get('id') or 0))

    # Only the granted capabilities, each with its own prose. This doubles as
    # the model's permission manual — it stops the agent proposing work the
    # credential can't do, and gives it the words to explain why.
    caps = compute_course_capabilities(ctx.user, ctx.course, is_course_scoped=True)
    granted = {
        key: CAPABILITY_DESCRIPTIONS.get(Capability(key), '')
        for key, allowed in caps.items() if allowed
    }

    payload = shaping.envelope(
        {
            'course': course_header(ctx.course),
            'keyScope': ctx.scope,
            'assignments': rows,
            'capabilities': granted,
        },
        meta={'assignmentCount': len(rows)},
        warnings=(['This course is archived; all writes are blocked.']
                  if ctx.course.archived else None),
    )
    return shaping.enforce_budget(payload)


@tool(
    name='codepost_get_roster',
    title='Course roster',
    description=(
        'The people in this course by role. Students appear as stable aliases '
        '(student-…), staff as emails. This is the only way to resolve a '
        'student name or partial email to an alias: search matches the real '
        'address server-side and returns the alias.\n\n'
        "Defaults to counts. Pass view='emails' to get the actual entries."
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'view': {'enum': ['counts', 'emails'], 'default': 'counts'},
            'roles': {
                'type': 'array',
                'items': {'enum': ['students', 'graders', 'courseAdmins',
                                   'superGraders', 'rubricEditors', 'quizGraders',
                                   'inactiveStudents', 'notActivated']},
                'description': 'Defaults to students, graders and courseAdmins.',
            },
            'search': {
                'type': 'string',
                'description': 'Case-insensitive substring match over addresses.',
            },
            'limit': {'type': 'integer', 'default': 100, 'maximum': 200},
            'cursor': {'type': 'string'},
        },
        'additionalProperties': False,
    },
    capability=Capability.VIEW_ROSTER,
    min_scope=SCOPE_READ,
    read_only=True,
)
def get_roster(ctx, view: str = 'counts', roles=None, search: str = '',
               limit: int = 100, cursor: str = ''):
    from core.agent.tools._common import camelize_roster, load_roster

    roster = camelize_roster(load_roster(ctx))
    wanted = roles or ['students', 'graders', 'courseAdmins']

    counts = {role: len(roster.get(role) or []) for role in wanted}
    data = {'course': course_header(ctx.course), 'counts': counts}
    meta: dict = {}

    if view == 'emails':
        rows = []
        for role in wanted:
            for email in (roster.get(role) or []):
                if search and search.lower() not in str(email).lower():
                    continue
                rows.append({'email': email, 'role': role})
        rows.sort(key=lambda r: (r['role'], r['email']))

        offset = shaping.decode_cursor(cursor).get('offset', 0)
        window, meta = shaping.paginate(
            rows, limit=shaping.clamp_limit(limit), offset=offset,
            cursor_payload={'view': view, 'roles': wanted, 'search': search})
        data['members'] = window

    return shaping.enforce_budget(shaping.envelope(data, meta=meta))


@tool(
    name='codepost_list_courses',
    title='List your courses',
    description=(
        'The courses this credential can manage, with each course id. Call this '
        'first, confirm with the user which course they mean if more than one '
        'fits, then pass that courseId to every other tool.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'includeArchived': {'type': 'boolean', 'default': False},
        },
        'additionalProperties': False,
    },
    min_scope=SCOPE_READ,
    read_only=True,
    # Course selection only exists for personal-token connections — a course
    # key IS a course, so pinned connections never see this tool.
    course_bound=False,
    unscoped_only=True,
)
def list_courses(ctx, includeArchived: bool = False):
    # The one sanctioned ORM read in the tool layer: it touches nothing but the
    # authenticated caller's own membership rows (the same M2M relations every
    # roster serializer walks), so there is no resource permission to enforce —
    # and GET /courses/ can't serve here: its list() returns only courseAdmin
    # courses, silently dropping grader/superGrader/quizGrader memberships.
    roles = (
        ('courseAdmin', ctx.user.courseAdmin_courses),
        ('superGrader', ctx.user.superGrader_courses),
        ('grader', ctx.user.grader_courses),
        ('rubricEditor', ctx.user.rubricEditor_courses),
        ('quizGrader', ctx.user.quizGrader_courses),
    )

    by_id: dict[int, dict] = {}
    for role, relation in roles:
        for course in relation.all():
            entry = by_id.setdefault(course.id, {
                'id': course.id,
                'name': course.name,
                'period': course.period,
                'archived': course.archived,
                'roles': [],
            })
            entry['roles'].append(role)

    rows = [c for c in by_id.values() if includeArchived or not c['archived']]
    rows.sort(key=lambda c: (c['archived'], c['name'], c['period']))

    return shaping.enforce_budget(shaping.envelope(
        {'courses': rows},
        meta={'total': len(rows)},
        warnings=(None if rows else
                  ['This account staffs no courses. The user may need a course '
                   'admin to add them, or a course API key instead.'])))
