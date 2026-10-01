# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Course-wide analytics reads: gradebook, assignment analytics, audit log."""
from __future__ import annotations

from core.agent import shaping
from core.agent.registry import SCOPE_READ, tool
from core.agent.tools._common import course_header, resolve_student, unalias
from core.permissions.capabilities import Capability

_ANALYTICS_BLOCKS = (
    'gradeDistribution', 'graderWorkload', 'gradingTimeline', 'testResults',
    'rubricUsage', 'scoreByCategory', 'graderConsistency', 'submissionAttempts',
    'timeToGrade', 'lateSubmissions', 'feedbackDepth',
)


@tool(
    name='codepost_get_gradebook',
    title='Course gradebook',
    description=(
        'Course-wide grades: every active student × every assignment and quiz. '
        "Defaults to a statistical summary (per-column means, a distribution, "
        "and an attention list of struggling students) — pass view='rows' only "
        'when you truly need per-student numbers; a large course is thousands '
        'of cells.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'view': {'enum': ['summary', 'rows'], 'default': 'summary'},
            'student': {'type': 'string',
                        'description': "One student's row (alias or email; implies rows)."},
            'section': {'type': 'string', 'description': 'Restrict rows to a section name.'},
            'limit': {'type': 'integer', 'default': 25, 'maximum': 100},
            'cursor': {'type': 'string'},
        },
        'additionalProperties': False,
    },
    capability=Capability.VIEW_ANALYTICS,
    min_scope=SCOPE_READ,
    read_only=True,
)
def get_gradebook(ctx, view: str = 'summary', student: str = '', section: str = '',
                  limit: int = 25, cursor: str = ''):
    from core.views.course import CourseViewSet

    data = ctx.dispatch.require(
        CourseViewSet, {'get': 'gradebook'},
        method='GET', path=f'/courses/{ctx.course.id}/gradebook/', pk=ctx.course.id,
        what='reading the gradebook')

    columns = {'assignments': data.get('assignments') or [],
               'quizzes': data.get('quizzes') or []}
    rows = data.get('rows') or []
    if section:
        rows = [r for r in rows if section in (r.get('section') or '')]
    if student:
        email = resolve_student(ctx, student).lower()
        rows = [r for r in rows if str(r.get('student') or '').lower() == email]
        view = 'rows'

    if view == 'summary':
        return shaping.enforce_budget(shaping.envelope(
            {'course': course_header(ctx.course),
             'columns': columns,
             **_summarise(columns, rows)},
            meta={'students': len(rows)}))

    # rows view: flatten cells to "name: grade" maps so no repeated keys per row
    a_names = {a['id']: a['name'] for a in columns['assignments']}
    q_names = {q['id']: f"Quiz: {q['title']}" for q in columns['quizzes']}
    flat = []
    for r in rows:
        grades = {a_names.get(c['assignment'], str(c['assignment'])): c['grade']
                  for c in (r.get('assignmentCells') or [])}
        grades.update({q_names.get(c['quiz'], str(c['quiz'])): c['score']
                       for c in (r.get('quizCells') or [])})
        flat.append({'student': r.get('student'), 'section': r.get('section'),
                     'grades': grades, 'totalEarned': r.get('totalEarned'),
                     'totalPossible': r.get('totalPossible'),
                     'percent': r.get('percent')})

    offset = shaping.decode_cursor(cursor).get('offset', 0)
    window, meta = shaping.paginate(
        flat, limit=shaping.clamp_limit(limit), offset=offset,
        cursor_payload={'view': 'rows', 'section': section})
    return shaping.enforce_budget(shaping.envelope(
        {'course': course_header(ctx.course), 'columns': columns, 'rows': window},
        meta=meta))


def _summarise(columns, rows):
    """Aggregate the raw grid into what an instructor actually asks about."""
    per_column = []
    for i, a in enumerate(columns['assignments']):
        cells = [r['assignmentCells'][i] for r in rows
                 if len(r.get('assignmentCells') or []) > i]
        grades = [c['grade'] for c in cells if c.get('grade') is not None]
        per_column.append({
            'column': f"assignment:{a['id']}", 'name': a['name'],
            'graded': len(grades),
            'pending': sum(1 for c in cells
                           if c.get('hasSubmission') and c.get('grade') is None),
            'missing': sum(1 for c in cells if not c.get('hasSubmission')),
            'mean': round(sum(grades) / len(grades), 2) if grades else None,
        })
    for i, q in enumerate(columns['quizzes']):
        cells = [r['quizCells'][i] for r in rows
                 if len(r.get('quizCells') or []) > i]
        scores = [c['score'] for c in cells if c.get('score') is not None]
        per_column.append({
            'column': f"quiz:{q['id']}", 'name': q['title'],
            'graded': len(scores),
            'needsGrading': sum(1 for c in cells if c.get('needsGrading')),
            'noAttempt': sum(1 for c in cells if not c.get('hasAttempts')),
            'mean': round(sum(float(s) for s in scores) / len(scores), 2)
                    if scores else None,
        })

    percents = sorted(float(r['percent']) for r in rows if r.get('percent') is not None)
    buckets = {'90-100': 0, '80-90': 0, '70-80': 0, '60-70': 0, '<60': 0}
    for p in percents:
        key = ('90-100' if p >= 90 else '80-90' if p >= 80 else
               '70-80' if p >= 70 else '60-70' if p >= 60 else '<60')
        buckets[key] += 1

    struggling = sorted(
        (r for r in rows if r.get('percent') is not None and float(r['percent']) < 60),
        key=lambda r: float(r['percent']))
    attention = {
        'belowSixtyPercent': {
            'count': len(struggling),
            'sample': [r['student'] for r in struggling[:5]],
            'hint': 'codepost_get_gradebook(view="rows", limit=25) for detail',
        },
    }

    overall = {}
    if percents:
        overall = {
            'meanPercent': round(sum(percents) / len(percents), 1),
            'medianPercent': round(percents[len(percents) // 2], 1),
            'distribution': buckets,
        }
    return {'overall': overall, 'perColumn': per_column, 'attention': attention}


@tool(
    name='codepost_get_assignment_analytics',
    title='Assignment analytics',
    description=(
        'Deep analytics for one assignment. You MUST pick which blocks you '
        'want — the full set is eleven blocks and far too large to return '
        'whole. Available: ' + ', '.join(_ANALYTICS_BLOCKS) + '.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'assignmentId': {'type': 'integer'},
            'blocks': {
                'type': 'array',
                'items': {'enum': list(_ANALYTICS_BLOCKS)},
                'description': "Defaults to ['gradeDistribution', 'lateSubmissions'].",
            },
            'buckets': {'type': 'integer', 'default': 10,
                        'description': 'Histogram buckets for gradeDistribution.'},
        },
        'required': ['assignmentId'],
        'additionalProperties': False,
    },
    capability=Capability.VIEW_ANALYTICS,
    min_scope=SCOPE_READ,
    read_only=True,
)
def get_assignment_analytics(ctx, assignmentId: int, blocks=None, buckets: int = 10):
    from core.views.assignment import AssignmentViewSet

    from core.agent.tools._common import fetch_assignment
    assignment = fetch_assignment(ctx, assignmentId)

    data = ctx.dispatch.require(
        AssignmentViewSet, {'get': 'analytics'},
        method='GET', path=f'/assignments/{assignmentId}/analytics/',
        query=f'buckets={int(buckets)}', pk=assignmentId,
        what=f'reading analytics for assignment {assignmentId}')

    wanted = blocks or ['gradeDistribution', 'lateSubmissions']

    # The service returns [] both when the assignment is worth 0 points and
    # when nothing is finalized — indistinguishable to the caller, so say why.
    warnings = None
    if 'gradeDistribution' in wanted and not data.get('gradeDistribution'):
        if (assignment.get('points') or 0) <= 0:
            warnings = ['gradeDistribution is empty: the assignment is worth 0 '
                        'points, so grades cannot be bucketed.']
        elif not assignment.get('submissionsFinalizedCount'):
            warnings = ['gradeDistribution is empty: no submissions are '
                        'finalized yet, so there are no grades to bucket.']

    return shaping.enforce_budget(shaping.envelope(
        {'course': course_header(ctx.course),
         'assignment': {'id': assignment.get('id'), 'name': assignment.get('name')},
         'analytics': {k: data.get(k) for k in wanted}},
        meta={'included': wanted,
              'available': list(_ANALYTICS_BLOCKS)},
        warnings=warnings))


@tool(
    name='codepost_get_audit_log',
    title='Course audit log',
    description=(
        'Course activity: assignment state changes, feedback views, regrade '
        'requests, quiz events, and agent-initiated writes. Filterable by '
        'event type, student, assignment, and date range; groupBy returns '
        'counts instead of rows.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'eventType': {'type': 'string'},
            'student': {'type': 'string',
                        'description': 'Actor to filter by: a student alias, or '
                                       'a staff email.'},
            'assignmentId': {'type': 'integer'},
            'since': {'type': 'string', 'description': 'ISO datetime lower bound.'},
            'until': {'type': 'string', 'description': 'ISO datetime upper bound.'},
            'groupBy': {'enum': ['none', 'eventType', 'user'], 'default': 'none'},
            'limit': {'type': 'integer', 'default': 25, 'maximum': 100},
        },
        'additionalProperties': False,
    },
    capability=Capability.VIEW_AUDIT_LOG,
    min_scope=SCOPE_READ,
    read_only=True,
)
def get_audit_log(ctx, eventType: str = '', student: str = '', assignmentId=None,
                  since: str = '', until: str = '', groupBy: str = 'none',
                  limit: int = 25):
    from core.views.course import CourseViewSet

    # The endpoint's query params are snake_case (event_type, date_from,
    # date_to) — one of the places the camelCase convention leaks. Translate at
    # the boundary so the model never sees it.
    params = []
    if eventType:
        params.append(f'event_type={eventType}')
    if student:
        # unalias, not resolve_student: the actor may be a grader.
        params.append(f'student={unalias(ctx, student)}')
    if assignmentId is not None:
        params.append(f'assignment={int(assignmentId)}')
    if since:
        params.append(f'date_from={since}')
    if until:
        params.append(f'date_to={until}')

    data = ctx.dispatch.require(
        CourseViewSet, {'get': 'auditLog'},
        method='GET', path=f'/courses/{ctx.course.id}/auditLog/',
        query='&'.join(params), pk=ctx.course.id,
        what='reading the audit log')

    rows = data.get('results', data) if isinstance(data, dict) else data
    rows = rows or []
    total = data.get('count', len(rows)) if isinstance(data, dict) else len(rows)

    if groupBy != 'none':
        key = 'eventType' if groupBy == 'eventType' else 'userEmail'
        counts: dict[str, int] = {}
        for row in rows:
            counts[str(row.get(key))] = counts.get(str(row.get(key)), 0) + 1
        return shaping.enforce_budget(shaping.envelope(
            {'course': course_header(ctx.course), 'groupBy': groupBy,
             'counts': dict(sorted(counts.items(), key=lambda kv: -kv[1]))},
            meta={'totalEvents': total,
                  'note': 'Counts cover the first page of matching events.'}))

    trimmed = [shaping.project(r, ('id', 'eventType', 'userEmail', 'assignmentName',
                                   'quizTitle', 'meta', 'created'))
               for r in rows[:shaping.clamp_limit(limit)]]
    return shaping.enforce_budget(shaping.envelope(
        {'course': course_header(ctx.course), 'events': trimmed},
        meta={'total': total, 'returned': len(trimmed),
              'truncated': total > len(trimmed)}))
