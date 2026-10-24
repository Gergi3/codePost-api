# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Tests for the course gradebook: the students × (assignments + quizzes) grid and its
CSV export. Course admins only; totals count graded work only."""
from decimal import Decimal

import factory
import pytest
from django.db.models.signals import post_save
from django.utils import timezone
from rest_framework import status

from core.tests.views.quiz_helpers import _add, _bank, _dec, _mc, _quiz


@pytest.fixture
def gradebook_setup(db):
    from core.models import Assignment, Submission
    from core.tests.factories import AdminFactory, CourseFactory

    with factory.django.mute_signals(post_save):
        course = CourseFactory(name="cs336", period="f2026", organization__name="Rutgers")
        hw = Assignment.objects.create(course=course, name='HW1', points=Decimal('100'))
        students = list(course.students.all().order_by('email'))
        # students[0]: finalized, grade pinned (Submission.save() recalculates grade
        # unless gradeFrozen). students[1]: submitted but not yet finalized.
        finalized = Submission.objects.create(
            assignment=hw, isFinalized=True, gradeFrozen=True, grade=Decimal('87.50'),
            dateUploaded=timezone.now())
        finalized.students.add(students[0])
        pending = Submission.objects.create(assignment=hw, dateUploaded=timezone.now())
        pending.students.add(students[1])

    return {
        'course': course,
        'hw': hw,
        'admin': course.courseAdmins.first(),
        'grader': course.graders.first(),
        'students': students,
        'outsider': AdminFactory(course='other', organization=course.organization, count=88),
    }


def _get(api_client, user, course, path='gradebook'):
    api_client.force_authenticate(user=user)
    return api_client.get(f'/courses/{course.id}/{path}/')


def _row(data, email):
    return next(r for r in data['rows'] if r['student'] == email)


def _cell(row, kind, col_id):
    key, id_key = (('assignmentCells', 'assignment') if kind == 'assignment'
                   else ('quizCells', 'quiz'))
    return next(c for c in row[key] if c[id_key] == col_id)


class TestGradebookAccess:
    @pytest.mark.parametrize('path', ['gradebook', 'gradebookExport'])
    def test_admin_allowed_others_forbidden(self, api_client, gradebook_setup, path):
        course = gradebook_setup['course']
        assert _get(api_client, gradebook_setup['admin'], course, path).status_code == status.HTTP_200_OK
        for user in (gradebook_setup['grader'], gradebook_setup['students'][0],
                     gradebook_setup['outsider']):
            assert _get(api_client, user, course, path).status_code == status.HTTP_403_FORBIDDEN


class TestGradebookContent:
    def test_rows_cover_active_roster_sorted(self, api_client, gradebook_setup):
        course = gradebook_setup['course']
        resp = _get(api_client, gradebook_setup['admin'], course)
        emails = [r['student'] for r in resp.data['rows']]
        assert emails == sorted(s.email for s in gradebook_setup['students'])
        # Inactive students never appear.
        inactive = {u.email for u in course.inactive_students.all()}
        assert not inactive & set(emails)
        # Column metadata includes both the factory assignment and HW1.
        names = {a['name'] for a in resp.data['assignments']}
        assert {'Loops', 'HW1'} <= names

    def test_section_reported_per_student(self, api_client, gradebook_setup):
        course = gradebook_setup['course']
        s0, s1 = gradebook_setup['students'][0], gradebook_setup['students'][1]
        section = course.sections.first()  # the factory's "P01"
        section.students.add(s0)
        resp = _get(api_client, gradebook_setup['admin'], course)
        assert _row(resp.data, s0.email)['section'] == section.name
        assert _row(resp.data, s1.email)['section'] is None

    def test_assignment_cells_show_grade_regardless_of_finalization(self, api_client, gradebook_setup):
        course, hw = gradebook_setup['course'], gradebook_setup['hw']
        s0, s1 = gradebook_setup['students'][0], gradebook_setup['students'][1]
        resp = _get(api_client, gradebook_setup['admin'], course)
        graded = _cell(_row(resp.data, s0.email), 'assignment', hw.id)
        assert _dec(graded['grade']) == _dec('87.50')
        assert graded['hasSubmission'] is True and graded['isFinalized'] is True
        # Admin view: an unfinalized submission still exposes its computed grade (here the
        # default full points, since it has no deductions) — no more "pending" hiding.
        unfinalized = _cell(_row(resp.data, s1.email), 'assignment', hw.id)
        assert _dec(unfinalized['grade']) == _dec('100')
        assert unfinalized['hasSubmission'] is True and unfinalized['isFinalized'] is False

    def test_quiz_official_score_policy_and_per_student_max(self, api_client, gradebook_setup):
        from core.models import QuizAttempt
        course = gradebook_setup['course']
        s0, s1 = gradebook_setup['students'][0], gradebook_setup['students'][1]
        quiz = _quiz(course, scoringPolicy='highest')
        _add(quiz, _mc(course, _bank(course)))
        for n, score in ((1, '5'), (2, '8')):
            QuizAttempt.objects.create(quiz=quiz, student=s0, attemptNumber=n, status='submitted',
                                       score=_dec(score), maxScore=_dec('10'),
                                       needsManualGrading=False, submittedAt=timezone.now())
        # Another student drew a bigger set — their own maxScore is used.
        QuizAttempt.objects.create(quiz=quiz, student=s1, attemptNumber=1, status='submitted',
                                   score=_dec('6'), maxScore=_dec('12'),
                                   needsManualGrading=False, submittedAt=timezone.now())
        resp = _get(api_client, gradebook_setup['admin'], course)
        c0 = _cell(_row(resp.data, s0.email), 'quiz', quiz.id)
        assert (_dec(c0['score']), _dec(c0['maxScore'])) == (_dec('8'), _dec('10'))
        c1 = _cell(_row(resp.data, s1.email), 'quiz', quiz.id)
        assert (_dec(c1['score']), _dec(c1['maxScore'])) == (_dec('6'), _dec('12'))

    def test_quiz_pending_manual_grading_excluded_from_totals(self, api_client, gradebook_setup):
        from core.models import QuizAttempt
        course = gradebook_setup['course']
        s1 = gradebook_setup['students'][1]
        quiz = _quiz(course)
        _add(quiz, _mc(course, _bank(course)))
        QuizAttempt.objects.create(quiz=quiz, student=s1, attemptNumber=1, status='submitted',
                                   needsManualGrading=True, submittedAt=timezone.now())
        resp = _get(api_client, gradebook_setup['admin'], course)
        row = _row(resp.data, s1.email)
        cell = _cell(row, 'quiz', quiz.id)
        assert cell == {'quiz': quiz.id, 'score': None, 'maxScore': None,
                        'needsGrading': True, 'hasAttempts': True}
        # The ungraded quiz contributes nothing; totals reflect only s1's HW1 grade
        # (100/100, now shown despite being unfinalized).
        assert _dec(row['totalEarned']) == _dec('100')
        assert _dec(row['totalPossible']) == _dec('100')
        assert _dec(row['percent']) == _dec('100')

    def test_draft_quiz_hidden_unless_attempted(self, api_client, gradebook_setup):
        from core.models import QuizAttempt
        course = gradebook_setup['course']
        draft = _quiz(course, title='Draft', isPublished=False)
        attempted_draft = _quiz(course, title='Attempted draft', isPublished=False)
        QuizAttempt.objects.create(quiz=attempted_draft, student=gradebook_setup['students'][0],
                                   attemptNumber=1, status='submitted', score=_dec('1'),
                                   maxScore=_dec('2'), needsManualGrading=False,
                                   submittedAt=timezone.now())
        resp = _get(api_client, gradebook_setup['admin'], course)
        quiz_ids = {q['id'] for q in resp.data['quizzes']}
        assert draft.id not in quiz_ids
        assert attempted_draft.id in quiz_ids

    def test_totals_math(self, api_client, gradebook_setup):
        from core.models import QuizAttempt
        course = gradebook_setup['course']
        s0 = gradebook_setup['students'][0]
        quiz = _quiz(course)
        _add(quiz, _mc(course, _bank(course)))
        QuizAttempt.objects.create(quiz=quiz, student=s0, attemptNumber=1, status='submitted',
                                   score=_dec('8'), maxScore=_dec('10'),
                                   needsManualGrading=False, submittedAt=timezone.now())
        resp = _get(api_client, gradebook_setup['admin'], course)
        row = _row(resp.data, s0.email)
        # 87.50/100 (HW1) + 8/10 (quiz); the Loops column has no submission for s0.
        assert _dec(row['totalEarned']) == _dec('95.50')
        assert _dec(row['totalPossible']) == _dec('110.00')
        assert _dec(row['percent']) == _dec('86.82')

    def test_assignments_query_param_restricts_columns_and_totals(self, api_client, gradebook_setup):
        course, hw = gradebook_setup['course'], gradebook_setup['hw']
        s0 = gradebook_setup['students'][0]
        api_client.force_authenticate(user=gradebook_setup['admin'])
        # Restrict to HW1 only: the factory's "Loops" assignment column drops out, and s0's
        # totals cover HW1 alone (87.50/100), independent of any other column.
        resp = api_client.get(f'/courses/{course.id}/gradebook/?assignments={hw.id}')
        assert resp.status_code == status.HTTP_200_OK
        assert [a['id'] for a in resp.data['assignments']] == [hw.id]
        row = _row(resp.data, s0.email)
        assert _dec(row['totalEarned']) == _dec('87.50')
        assert _dec(row['totalPossible']) == _dec('100.00')
        # An empty value selects no assignment columns at all.
        empty = api_client.get(f'/courses/{course.id}/gradebook/?assignments=')
        assert empty.data['assignments'] == []


class TestGradebookExport:
    def test_csv_shape_and_content(self, api_client, gradebook_setup):
        course, hw = gradebook_setup['course'], gradebook_setup['hw']
        s0 = gradebook_setup['students'][0]
        resp = _get(api_client, gradebook_setup['admin'], course, 'gradebookExport')
        assert resp.status_code == status.HTTP_200_OK
        assert resp['Content-Type'] == 'text/csv'
        assert f'gradebook_course_{course.id}.csv' in resp['Content-Disposition']
        lines = resp.content.decode().strip().splitlines()
        header = lines[0]
        assert header.startswith('Student,Section')
        assert 'HW1 (100.00)' in header
        assert header.rstrip().endswith('Total Earned,Total Possible,Percent')
        s0_line = next(l for l in lines[1:] if l.startswith(s0.email))
        assert '87.50' in s0_line

    def test_csv_export_filters_columns_and_section(self, api_client, gradebook_setup):
        from core.models import QuizAttempt
        course, hw = gradebook_setup['course'], gradebook_setup['hw']
        s0, s1 = gradebook_setup['students'][0], gradebook_setup['students'][1]
        quiz = _quiz(course)
        _add(quiz, _mc(course, _bank(course)))
        QuizAttempt.objects.create(quiz=quiz, student=s0, attemptNumber=1, status='submitted',
                                   score=_dec('8'), maxScore=_dec('10'),
                                   needsManualGrading=False, submittedAt=timezone.now())
        section = course.sections.first()
        section.students.add(s0)

        api_client.force_authenticate(user=gradebook_setup['admin'])
        resp = api_client.get(
            f'/courses/{course.id}/gradebookExport/'
            f'?assignments={hw.id}&quizzes=&section={section.name}')
        lines = resp.content.decode().strip().splitlines()
        # Only the selected assignment column; no quiz columns at all.
        assert lines[0] == 'Student,Section,HW1 (100.00),Total Earned,Total Possible,Percent'
        # Only the sectioned student's row, with totals over the included column only.
        body = lines[1:]
        assert len(body) == 1 and body[0].startswith(s0.email)
        assert body[0].endswith('87.50,87.50,100.00,87.50')
        assert not any(l.startswith(s1.email) for l in body)
