# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Admin actions that re-run AI quiz generation from the Django admin."""
from types import SimpleNamespace

import factory
import pytest
from django.contrib import admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db.models.signals import post_save
from django.test import RequestFactory

from core.tests.views.quiz_helpers import _quiz


def _admin_request(user):
    request = RequestFactory().post('/admin/')
    request.user = user
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def _messages(request):
    return [str(m) for m in request._messages]


@pytest.fixture
def world(db):
    from core.models import GeneratedQuestionSet, QuizSuggestionJob
    from core.tests.factories import AssignmentFactory, CourseFactory
    with factory.django.mute_signals(post_save):
        course = CourseFactory(name='cs111', period='F2026', organization__name='Rutgers')
        assignment = AssignmentFactory(course=course, name='HW1')
        submission = assignment.submissions.first()
        quiz = _quiz(course, title='Lab quiz', assignment=assignment)
        admin_user = course.courseAdmins.first()
        admin_user.is_superuser = admin_user.is_staff = True
        admin_user.save()
        job = QuizSuggestionJob.objects.create(
            course=course, assignment=assignment, requestedBy=admin_user, status='failed',
            errorMessage='could not be parsed',
            resultData={'request': {'num_questions': 3, 'question_types': ['code'], 'instructions': 'harder'}})
        preview_job = QuizSuggestionJob.objects.create(course=course, quiz=quiz, status='failed')
        legacy_job = QuizSuggestionJob.objects.create(course=course, assignment=assignment, status='failed')
        gen_set = GeneratedQuestionSet.objects.create(
            quiz=quiz, student=course.students.first(), submission=submission, status='failed',
            errorMessage='boom')
    return dict(course=course, assignment=assignment, submission=submission, quiz=quiz, admin=admin_user,
                job=job, preview_job=preview_job, legacy_job=legacy_job, gen_set=gen_set)


class TestRetryGeneration:
    def test_replays_the_recorded_request_as_a_new_job(self, world, monkeypatch):
        from core.admin import QuizSuggestionJobAdmin
        from core.models import QuizSuggestionJob
        calls = []
        monkeypatch.setattr('core.tasks.generate_quiz_question_suggestions.delay',
                            lambda **kw: calls.append(kw) or SimpleNamespace(id='task-new'))
        request = _admin_request(world['admin'])
        QuizSuggestionJobAdmin(QuizSuggestionJob, admin.site).retry_generation(
            request, QuizSuggestionJob.objects.filter(pk=world['job'].pk))

        [call] = calls
        new_job = QuizSuggestionJob.objects.get(pk=call['job_id'])
        assert new_job.pk != world['job'].pk
        assert new_job.assignment_id == world['assignment'].id
        assert new_job.requestedBy == world['admin']
        assert new_job.taskId == 'task-new'
        assert call['num_questions'] == 3 and call['question_types'] == ['code'] and call['instructions'] == 'harder'
        assert call['requested_by_id'] == world['admin'].id
        assert _messages(request) == ['Started 1 new generation job(s).']

    def test_skips_preview_jobs_and_jobs_without_a_recorded_request(self, world, monkeypatch):
        from core.admin import QuizSuggestionJobAdmin
        from core.models import QuizSuggestionJob
        calls = []
        monkeypatch.setattr('core.tasks.generate_quiz_question_suggestions.delay',
                            lambda **kw: calls.append(kw) or SimpleNamespace(id='t'))
        request = _admin_request(world['admin'])
        QuizSuggestionJobAdmin(QuizSuggestionJob, admin.site).retry_generation(
            request, QuizSuggestionJob.objects.filter(pk__in=[world['preview_job'].pk, world['legacy_job'].pk]))
        assert calls == []
        assert QuizSuggestionJob.objects.count() == 3
        assert 'Skipped 2' in _messages(request)[0]


class TestRegenerateSelected:
    def test_resets_the_set_and_enqueues_a_forced_run(self, world, monkeypatch):
        from core.admin import GeneratedQuestionSetAdmin
        from core.models import GeneratedQuestionSet
        calls = []
        monkeypatch.setattr('core.tasks.generate_personalized_quiz_sets.delay',
                            lambda *a, **kw: calls.append((a, kw)))
        gen_set = world['gen_set']
        gen_set.approvedBy = world['admin']
        gen_set.save()
        request = _admin_request(world['admin'])
        GeneratedQuestionSetAdmin(GeneratedQuestionSet, admin.site).regenerate_selected(
            request, GeneratedQuestionSet.objects.filter(pk=gen_set.pk))

        assert calls == [((world['submission'].id,),
                          {'quiz_id': world['quiz'].id, 'force': True,
                           'requested_by_id': world['admin'].id, 'student_id': gen_set.student_id})]
        gen_set.refresh_from_db()
        assert gen_set.status == 'pending' and gen_set.approvedBy is None
        assert _messages(request) == ['Queued regeneration for 1 set(s).']

    def test_skips_a_set_whose_student_already_attempted(self, world, monkeypatch):
        from core.admin import GeneratedQuestionSetAdmin
        from core.models import GeneratedQuestionSet, GeneratedQuizQuestion, QuizAttempt, QuizGeneratedSection
        calls = []
        monkeypatch.setattr('core.tasks.generate_personalized_quiz_sets.delay',
                            lambda *a, **kw: calls.append((a, kw)))
        gen_set = world['gen_set']
        with factory.django.mute_signals(post_save):
            section = QuizGeneratedSection.objects.create(quiz=world['quiz'], systemPrompt='p', numQuestions=1)
            GeneratedQuizQuestion.objects.create(set=gen_set, section=section, text='q', questionType='essay')
            QuizAttempt.objects.create(quiz=world['quiz'], student=gen_set.student, status='submitted')
        request = _admin_request(world['admin'])
        GeneratedQuestionSetAdmin(GeneratedQuestionSet, admin.site).regenerate_selected(
            request, GeneratedQuestionSet.objects.filter(pk=gen_set.pk))
        assert calls == []
        gen_set.refresh_from_db()
        assert gen_set.status == 'failed'
        assert 'Skipped 1' in _messages(request)[0]
