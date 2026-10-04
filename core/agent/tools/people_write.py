# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""People & grading operations: roster, sections, grader assignment, rubric.

The wholesale roster replace (``PATCH /courses/{id}/roster/``) is deliberately
unreachable from every tool here — an agent sending a partial list would
silently unenroll a class. Only the incremental add/remove actions are wrapped.
"""
from __future__ import annotations

from core.agent import errors, guardrails, shaping
from core.agent.registry import SCOPE_ADMIN, SCOPE_WRITE, tool
from core.agent.tools._common import (course_header, fetch_assignment, load_roster,
                                      unalias)
from core.permissions.capabilities import Capability

_ROLES = ('students', 'graders', 'courseAdmins', 'superGraders',
          'rubricEditors', 'quizGraders')

_ROLE_LIST_SCHEMA = {
    'type': 'object',
    'properties': {role: {'type': 'array', 'items': {'type': 'string'}}
                   for role in _ROLES},
    'additionalProperties': False,
}


@tool(
    name='codepost_update_roster',
    title='Update roster',
    description=(
        'Add or remove people from the course roster by role. Students may '
        'be given as aliases or emails; staff by email.\n\n'
        'ADDING someone with no codePost account creates one (and may email '
        'them, per course settings) — the dry run names which emails are new. '
        'REMOVING deactivates (it never deletes accounts or work) and requires '
        'an admin-scope key plus the confirmation token from the preview.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'add': _ROLE_LIST_SCHEMA,
            'remove': _ROLE_LIST_SCHEMA,
            'dryRun': {'type': 'boolean', 'default': True},
            'confirmToken': {'type': 'string',
                             'description': 'Required to apply removals.'},
        },
        'required': [],
        'additionalProperties': False,
    },
    capability=Capability.MANAGE_ROSTER,
    min_scope=SCOPE_WRITE, tier=2,
    read_only=False, destructive=True, idempotent=True,
)
def update_roster(ctx, add=None, remove=None, dryRun: bool = True,
                  confirmToken: str = ''):
    from django.contrib.auth.models import User

    from core.views.course import CourseViewSet

    # Canonicalize aliases to emails BEFORE the plan/args are built, so the
    # Tier-2 confirm token agrees whether the model sends aliases or emails on
    # either call. Brand-new typed addresses pass through unalias unchanged.
    add = {k: [unalias(ctx, e) for e in v] for k, v in (add or {}).items() if v}
    remove = {k: [unalias(ctx, e) for e in v] for k, v in (remove or {}).items() if v}
    if not add and not remove:
        raise errors.ToolError(
            'PRECONDITION_NOT_MET', 'Nothing to add or remove.',
            remedy='Pass add and/or remove role lists.', retryable=True)

    # Removals above write scope: deactivating people is the sharp edge.
    if remove and ctx.scope != SCOPE_ADMIN:
        raise errors.insufficient_key_scope('codepost_update_roster (remove)',
                                            SCOPE_ADMIN, ctx.scope)

    all_add = sorted({e for lst in add.values() for e in lst})
    existing = set(User.objects.filter(email__in=all_add)
                   .values_list('email', flat=True))
    new_accounts = [e for e in all_add if e not in existing]

    # Never let a removal orphan the course.
    roster = load_roster(ctx)
    current_admins = set(roster.get('courseAdmins') or [])
    removing_admins = set((remove.get('courseAdmins') or []))
    if removing_admins and not (current_admins - removing_admins):
        raise errors.ToolError(
            'PRECONDITION_NOT_MET',
            'This removal would leave the course with no courseAdmin.',
            remedy='Add another admin first, or remove fewer people.')

    plan = {
        'add': add, 'remove': remove,
        'newAccountsCreated': new_accounts,
        'welcomeEmails': bool(new_accounts) or ctx.course.emailNewUsers,
        'note': 'Removal deactivates members; their work and accounts remain.',
    }
    args = {'add': add, 'remove': remove}

    if dryRun:
        if remove:
            raise guardrails.confirmation_required(
                'codepost_update_roster', args, plan,
                course_id=ctx.course.id, user_id=ctx.user.pk,
                message=f'This removes {sum(len(v) for v in remove.values())} '
                        'roster entries (deactivation, not deletion). Review '
                        'and confirm.')
        return shaping.envelope(
            {'course': course_header(ctx.course), 'plan': plan},
            meta={'dryRun': True, 'hint': 'Re-call with dryRun=false to apply.'})

    if remove:
        guardrails.verify_token(confirmToken, 'codepost_update_roster', args,
                                plan, course_id=ctx.course.id,
                                user_id=ctx.user.pk)

    if add:
        ctx.dispatch.require(
            CourseViewSet, {'patch': 'addToRoster'},
            method='PATCH', path=f'/courses/{ctx.course.id}/addToRoster/',
            data=add, pk=ctx.course.id, what='adding to the roster')
    if remove:
        ctx.dispatch.require(
            CourseViewSet, {'patch': 'removeFromRoster'},
            method='PATCH', path=f'/courses/{ctx.course.id}/removeFromRoster/',
            data=remove, pk=ctx.course.id, what='removing from the roster')

    ctx._roster = None                       # roster cache is now stale
    ctx._aliases = None
    return shaping.envelope(
        {'course': course_header(ctx.course),
         'added': add, 'removed': remove,
         'newAccountsCreated': new_accounts},
        warnings=(['New accounts were created and may have received a welcome '
                   'email.'] if new_accounts else None))


@tool(
    name='codepost_manage_sections',
    title='Course sections',
    description=(
        'Create, rename or delete sections, and set their students and '
        'leaders.\n\n'
        'IMPORTANT: assigning students to a section REMOVES them from every '
        'other section in the course (a student has one section). The dry run '
        'for setMembers names who would move.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'op': {'enum': ['create', 'rename', 'delete', 'setMembers']},
            'sectionId': {'type': 'integer',
                          'description': 'For rename/delete/setMembers.'},
            'name': {'type': 'string', 'description': 'For create/rename.'},
            'students': {'type': 'array', 'items': {'type': 'string'},
                         'description': 'Student aliases or emails (setMembers).'},
            'leaders': {'type': 'array', 'items': {'type': 'string'},
                        'description': 'Staff emails (create/setMembers).'},
            'dryRun': {'type': 'boolean', 'default': True},
        },
        'required': ['op'],
        'additionalProperties': False,
    },
    capability=Capability.MANAGE_SECTIONS,
    min_scope=SCOPE_WRITE, tier=1,
    read_only=False, destructive=False, idempotent=True,
)
def manage_sections(ctx, op: str, sectionId=None, name: str = '', students=None,
                    leaders=None, dryRun: bool = True):
    from core.views.section import SectionViewSet

    if students:
        students = [unalias(ctx, s) for s in students]
    if op in ('rename', 'delete', 'setMembers') and sectionId is None:
        raise errors.ToolError(
            'PRECONDITION_NOT_MET', f"op='{op}' needs a sectionId.",
            remedy='Section ids come from codepost_get_roster or the course '
                   'overview.', retryable=True)

    if op == 'create':
        if not name:
            raise errors.ToolError(
                'PRECONDITION_NOT_MET', "op='create' needs a name.",
                remedy='Provide one.', retryable=True)
        if dryRun:
            return shaping.envelope(
                {'course': course_header(ctx.course),
                 'plan': {'create': name, 'students': students or [],
                          'leaders': leaders or []}},
                meta={'dryRun': True,
                      'hint': 'Re-call with dryRun=false to apply.'},
                warnings=(_move_warning(ctx, students) if students else None))
        data = ctx.dispatch.require(
            SectionViewSet, {'post': 'create'},
            method='POST', path='/sections/',
            data={'course': ctx.course.id, 'name': name,
                  'students': students or [], 'leaders': leaders or []},
            what=f"creating section '{name}'")
        return shaping.envelope(
            {'course': course_header(ctx.course),
             'section': shaping.project(data, ('id', 'name', 'students', 'leaders'))})

    if op == 'delete':
        if dryRun:
            return shaping.envelope(
                {'course': course_header(ctx.course),
                 'plan': {'delete': sectionId}},
                meta={'dryRun': True,
                      'hint': 'Re-call with dryRun=false to apply.'},
                warnings=['Deleting a section unassigns its students; it does '
                          'not remove them from the course.'])
        ctx.dispatch.require(
            SectionViewSet, {'delete': 'destroy'},
            method='DELETE', path=f'/sections/{sectionId}/', pk=sectionId,
            what=f'deleting section {sectionId}')
        return shaping.envelope(
            {'course': course_header(ctx.course), 'deleted': sectionId})

    # rename / setMembers → PATCH. The section serializer's cross-section
    # removal reads course from the payload (KeyError without it — the web UI
    # always sends it), so include it like the UI does.
    body = {'course': ctx.course.id}
    if op == 'rename':
        if not name:
            raise errors.ToolError(
                'PRECONDITION_NOT_MET', "op='rename' needs a name.",
                remedy='Provide one.', retryable=True)
        body['name'] = name
    else:  # setMembers
        if students is not None:
            body['students'] = students
        if leaders is not None:
            body['leaders'] = leaders
        if len(body) == 1:
            raise errors.ToolError(
                'PRECONDITION_NOT_MET',
                "op='setMembers' needs students and/or leaders.",
                remedy='Provide the full member list for this section.',
                retryable=True)

    if dryRun:
        warnings = _move_warning(ctx, students) if op == 'setMembers' else None
        return shaping.envelope(
            {'course': course_header(ctx.course),
             'plan': {op: body, 'sectionId': sectionId}},
            meta={'dryRun': True, 'hint': 'Re-call with dryRun=false to apply.'},
            warnings=warnings)

    data = ctx.dispatch.require(
        SectionViewSet, {'patch': 'partial_update'},
        method='PATCH', path=f'/sections/{sectionId}/', data=body, pk=sectionId,
        what=f'updating section {sectionId}')
    return shaping.envelope(
        {'course': course_header(ctx.course),
         'section': shaping.project(data, ('id', 'name', 'students', 'leaders'))},
        warnings=(['Students listed here were removed from any other section '
                   'they were in.'] if op == 'setMembers' and students else None))


def _move_warning(ctx, students):
    """Name the students this section assignment would pull out of others."""
    from core.views.course import CourseViewSet

    if not students:
        return None
    result = ctx.dispatch.call(
        CourseViewSet, {'get': 'sections'},
        method='GET', path=f'/courses/{ctx.course.id}/sections/', pk=ctx.course.id)
    if not result.ok:
        return None
    payload = result.data
    items = payload.get('results', payload) if isinstance(payload, dict) else payload
    moving = []
    for section in (items or []):
        for email in (section.get('students') or []):
            if email in students:
                moving.append(f"{email} (from '{section.get('name')}')")
    if moving:
        return [f'These students will MOVE out of their current section: '
                f'{moving}']
    return None


@tool(
    name='codepost_update_submission_grading',
    title='Assign graders / finalize',
    description=(
        'Assign a grader to submissions, finalize or unfinalize them, release '
        "them back to the Draw queue, or distribute an assignment's unclaimed "
        'submissions evenly among graders.\n\n'
        "op='assign': set grader (and optionally isFinalized) on the listed "
        "submissionIds. op='unassign': clear the grader on the listed "
        'submissionIds so graders can Draw them again; finalized submissions '
        "are refused (unfinalize them first). op='distribute': split every "
        'unclaimed submission of an assignment across the given grader emails. '
        'Finalizing requires the submission to have a grader and a grade.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'op': {'enum': ['assign', 'unassign', 'distribute'],
                   'default': 'assign'},
            'submissionIds': {'type': 'array', 'items': {'type': 'integer'},
                              'description': "For op='assign' / 'unassign'."},
            'grader': {'type': 'string', 'description': 'Grader email (assign).'},
            'isFinalized': {'type': 'boolean'},
            'assignmentId': {'type': 'integer',
                             'description': "For op='distribute'."},
            'graders': {'type': 'array', 'items': {'type': 'string'},
                        'description': "Grader emails (op='distribute')."},
            'dryRun': {'type': 'boolean', 'default': True},
        },
        'required': [],
        'additionalProperties': False,
    },
    capability=Capability.CLAIM_SUBMISSIONS,
    min_scope=SCOPE_WRITE, tier=1,
    read_only=False, destructive=False, idempotent=True,
)
def update_submission_grading(ctx, op: str = 'assign', submissionIds=None,
                              grader: str = '', isFinalized=None,
                              assignmentId=None, graders=None,
                              dryRun: bool = True):
    from core.views.assignment import AssignmentViewSet
    from core.views.submission import SubmissionViewSet

    if op == 'distribute':
        if assignmentId is None or not graders:
            raise errors.ToolError(
                'PRECONDITION_NOT_MET',
                "op='distribute' needs assignmentId and a graders list.",
                remedy='Provide both.', retryable=True)
        rows = ctx.dispatch.require(
            AssignmentViewSet, {'get': 'submissions'},
            method='GET', path=f'/assignments/{assignmentId}/submissions/',
            query='compact=1', pk=assignmentId,
            what='listing submissions to distribute')
        unclaimed = [r['id'] for r in (rows or []) if not r.get('grader')]

        share = {g: [] for g in graders}
        for i, sid in enumerate(unclaimed):
            share[graders[i % len(graders)]].append(sid)
        plan = {'assignmentId': assignmentId,
                'unclaimed': len(unclaimed),
                'perGrader': {g: len(ids) for g, ids in share.items()}}
        if dryRun:
            return shaping.envelope(
                {'course': course_header(ctx.course), 'plan': plan},
                meta={'dryRun': True,
                      'hint': 'Re-call with dryRun=false to apply.'})

        failures = []
        for g, ids in share.items():
            for sid in ids:
                result = ctx.dispatch.call(
                    SubmissionViewSet, {'patch': 'partial_update'},
                    method='PATCH', path=f'/submissions/{sid}/',
                    data={'grader': g}, pk=sid)
                if not result.ok:
                    failures.append({'submissionId': sid, 'grader': g,
                                     'detail': errors._stringify(result.data)})
        return shaping.envelope(
            {'course': course_header(ctx.course), 'distributed': plan,
             'failures': failures},
            warnings=(['Some assignments failed; re-call op="assign" for '
                       'those ids.'] if failures else None))

    # op in ('assign', 'unassign')
    if not submissionIds:
        raise errors.ToolError(
            'PRECONDITION_NOT_MET', f"op='{op}' needs submissionIds.",
            remedy='Get ids from codepost_list_submissions.', retryable=True)
    warnings = []
    if op == 'unassign':
        # The API refuses a finalized submission without a grader, so finalized
        # ids surface as failures rather than silently losing their grader.
        body = {'grader': None}
        warnings.append('Finalized submissions keep their grader and are '
                        'reported as failures; unfinalize them first.')
    else:
        body = {}
        if grader:
            body['grader'] = grader
        if isFinalized is not None:
            body['isFinalized'] = isFinalized
        if not body:
            raise errors.ToolError(
                'PRECONDITION_NOT_MET', 'Pass grader and/or isFinalized.',
                remedy='Nothing to change otherwise.', retryable=True)
        if isFinalized is False:
            warnings.append('Unfinalizing under per-student feedback revokes '
                            "a student's already-visible feedback.")

    if dryRun:
        return shaping.envelope(
            {'course': course_header(ctx.course),
             'plan': {'submissions': submissionIds, 'changes': body}},
            meta={'dryRun': True, 'hint': 'Re-call with dryRun=false to apply.'},
            warnings=warnings or None)

    applied, failures = [], []
    for sid in submissionIds:
        result = ctx.dispatch.call(
            SubmissionViewSet, {'patch': 'partial_update'},
            method='PATCH', path=f'/submissions/{sid}/', data=body, pk=sid)
        if result.ok:
            applied.append(sid)
        else:
            failures.append({'submissionId': sid,
                             'detail': errors._stringify(result.data)})
    if failures and not applied:
        raise errors.ToolError(
            'PARTIAL_FAILURE', 'No submissions could be updated.',
            remedy='Fix the reported problems and call again.', retryable=True,
            context={'failures': failures})
    return shaping.envelope(
        {'course': course_header(ctx.course), 'applied': applied,
         'failures': failures},
        warnings=(['Some updates failed — see failures.'] if failures else None))


@tool(
    name='codepost_edit_rubric',
    title='Edit rubric',
    description=(
        "Create, update or delete an assignment's rubric categories and "
        'comments in one batch. Get current ids from codepost_get_rubric. '
        'Operations run in a safe order (category creates first, deletes '
        'last) and report per-item results. Editing while feedback is open '
        'changes what students see immediately.'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'assignmentId': {'type': 'integer'},
            'categories': {
                'type': 'array',
                'items': {'type': 'object', 'properties': {
                    'op': {'enum': ['create', 'update', 'delete']},
                    'id': {'type': 'integer'},
                    'name': {'type': 'string'},
                    'pointLimit': {'type': 'number'},
                    'sortKey': {'type': 'integer'},
                    'helpText': {'type': 'string'},
                    'atMostOnce': {'type': 'boolean'},
                }, 'required': ['op']},
            },
            'comments': {
                'type': 'array',
                'items': {'type': 'object', 'properties': {
                    'op': {'enum': ['create', 'update', 'delete']},
                    'id': {'type': 'integer'},
                    'categoryId': {'type': 'integer'},
                    'text': {'type': 'string'},
                    'pointDelta': {'type': 'number'},
                    'explanation': {'type': 'string'},
                    'sortKey': {'type': 'integer'},
                }, 'required': ['op']},
            },
        },
        'required': ['assignmentId'],
        'additionalProperties': False,
    },
    capability=Capability.EDIT_RUBRIC,
    min_scope=SCOPE_WRITE, tier=1,
    read_only=False, destructive=True, idempotent=False,
)
def edit_rubric(ctx, assignmentId: int, categories=None, comments=None):
    from core.views.rubricCategory import RubricCategoryViewSet
    from core.views.rubricComment import RubricCommentViewSet

    assignment = fetch_assignment(ctx, assignmentId)
    categories = categories or []
    comments = comments or []
    if not categories and not comments:
        raise errors.ToolError(
            'PRECONDITION_NOT_MET', 'Nothing to change.',
            remedy='Pass categories and/or comments operations.', retryable=True)

    report = {'categories': [], 'comments': []}

    def run(view_cls, item, *, kind, create_body=None, path_root=''):
        op = item['op']
        if op == 'create':
            result = ctx.dispatch.call(view_cls, {'post': 'create'},
                                       method='POST', path=f'/{path_root}/',
                                       data=create_body)
        elif op == 'update':
            body = {k: v for k, v in item.items()
                    if k not in ('op', 'id', 'categoryId')}
            if kind == 'comment' and item.get('categoryId') is not None:
                body['category'] = item['categoryId']
            result = ctx.dispatch.call(view_cls, {'patch': 'partial_update'},
                                       method='PATCH',
                                       path=f"/{path_root}/{item['id']}/",
                                       data=body, pk=item['id'])
        else:
            result = ctx.dispatch.call(view_cls, {'delete': 'destroy'},
                                       method='DELETE',
                                       path=f"/{path_root}/{item['id']}/",
                                       pk=item['id'])
        entry = {'op': op, 'ok': result.ok}
        if result.ok and isinstance(result.data, dict):
            entry['id'] = result.data.get('id', item.get('id'))
        elif not result.ok:
            entry['id'] = item.get('id')
            entry['error'] = errors._stringify(result.data)
        report['categories' if kind == 'category' else 'comments'].append(entry)
        return entry

    # Order: category creates → comment ops → category updates → deletes last.
    for item in categories:
        if item['op'] == 'create':
            run(RubricCategoryViewSet, item, kind='category',
                path_root='rubricCategories',
                create_body={'assignment': assignmentId,
                             **{k: v for k, v in item.items() if k != 'op'}})
    for item in comments:
        if item['op'] == 'create':
            body = {k: v for k, v in item.items()
                    if k not in ('op', 'categoryId')}
            body['category'] = item.get('categoryId')
            run(RubricCommentViewSet, item, kind='comment',
                path_root='rubricComments', create_body=body)
        else:
            run(RubricCommentViewSet, item, kind='comment',
                path_root='rubricComments')
    for item in categories:
        if item['op'] == 'update':
            run(RubricCategoryViewSet, item, kind='category',
                path_root='rubricCategories')
    for item in categories:
        if item['op'] == 'delete':
            run(RubricCategoryViewSet, item, kind='category',
                path_root='rubricCategories')

    failures = [e for lst in report.values() for e in lst if not e['ok']]
    warnings = []
    if assignment.get('feedbackStatus') in ('live', 'released', 'per_student'):
        warnings.append('The feedback axis is open — students can see these '
                        'rubric changes now.')
    if failures:
        warnings.append(f'{len(failures)} operation(s) failed; see the report.')
    return shaping.enforce_budget(shaping.envelope(
        {'course': course_header(ctx.course),
         'assignment': {'id': assignmentId, 'name': assignment.get('name')},
         'report': report},
        warnings=warnings or None))
