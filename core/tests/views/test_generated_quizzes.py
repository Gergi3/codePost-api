# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Tests for per-student AI-generated quiz questions: section authoring (+ prompt
validation), the submission-triggered generation task (claiming, regenerate-unless-
approved, stale-batch discard, auto-publish), the review/approve/publish API and its
grader-permission gate, student availability gating, and attempt materialization."""
import json
import uuid
from datetime import timedelta
from decimal import Decimal

import factory
import pytest
from django.db.models.signals import post_save
from django.utils import timezone
from rest_framework import status

# Shared across the three quiz test files.
from core.tests.views.quiz_helpers import _enable_ai, _feature_on


QUESTIONS_JSON = json.dumps([
    {'type': 'multiple_choice', 'text': 'What does your helper return?', 'points': 2,
     'description': '```python\ndef helper():\n    return []\n```',
     'choices': [{'text': 'A list', 'is_correct': True}, {'text': 'None', 'is_correct': False}],
     'reference_solution': "Correct: 'A list' — helper() returns [] which is a list."},
    {'type': 'essay', 'text': 'Explain your loop.', 'points': 5, 'choices': [],
     'reference_solution': 'A correct answer covers: what the loop iterates over and why.'},
])


@pytest.fixture
def gen_setup(db):
    from core.tests.factories import CourseFactory, SubmissionFactory
    from core.models import Quiz, QuizGeneratedSection

    with factory.django.mute_signals(post_save):
        course = CourseFactory(name="cos226", period="s2026", organization__name="Princeton")
        assignment = course.assignments.first()
        assignment.state = 'published'
        assignment.save()
        students = list(course.students.all())
        submission = SubmissionFactory(assignment=assignment)
        submission.students.add(students[0])
        submission.dateUploaded = timezone.now()
        submission.save()

    # manualGeneration defaults to True; most of this suite exercises the automatic
    # pipeline, so opt back in — the manual-mode tests flip it on explicitly.
    quiz = Quiz.objects.create(course=course, title='Personalized Quiz', assignment=assignment,
                               isPublished=True, assignmentTrigger='after_submission',
                               manualGeneration=False)
    section = QuizGeneratedSection.objects.create(
        quiz=quiz, name='About your code', systemPrompt='Ask about {submission_files}.',
        numQuestions=2, pointsPerQuestion=Decimal('3'))
    return {
        'course': course,
        'assignment': assignment,
        'quiz': quiz,
        'section': section,
        'submission': submission,
        'admin': course.courseAdmins.first(),
        'grader': course.graders.first(),
        'students': students,
    }


def _mock_ai(monkeypatch, json_text=QUESTIONS_JSON, success=True, side_effect=None):
    from asgiref.sync import sync_to_async
    from core.services.ai_service import GenerationResult

    async def mock_generate(self, section, submission, demo_files=None):
        if side_effect is not None:
            await sync_to_async(side_effect)(section, submission)
        if not success:
            return GenerationResult(text='', success=False, error='model unavailable')
        return GenerationResult(text=json_text, success=True, input_tokens=10, output_tokens=20,
                                resolved_prompt=f'resolved prompt for section {section.id}')

    monkeypatch.setattr(
        'core.services.ai_service.AIService.generate_personalized_quiz_questions', mock_generate)
    _enable_ai(monkeypatch)


def _mock_ai_sequence(monkeypatch, texts):
    """Like ``_mock_ai`` but answers each call with the next entry of ``texts`` (``None``
    = provider failure). Returns the list of calls made, for asserting retry counts."""
    from core.services.ai_service import GenerationResult
    calls = []

    async def mock_generate(self, section, submission, demo_files=None):
        text = texts[len(calls)]
        calls.append(section.id)
        if text is None:
            return GenerationResult(text='', success=False, error='model unavailable')
        return GenerationResult(text=text, success=True, input_tokens=10, output_tokens=20,
                                resolved_prompt=f'resolved prompt for section {section.id}')

    monkeypatch.setattr(
        'core.services.ai_service.AIService.generate_personalized_quiz_questions', mock_generate)
    _enable_ai(monkeypatch)
    return calls


def _run_task(submission, **kwargs):
    from core.tasks import generate_personalized_quiz_sets
    generate_personalized_quiz_sets(submission.id, **kwargs)


def _make_set(quiz, student, submission=None, status='ready', questions=True):
    """Create a generated set (and questions) directly, bypassing the task."""
    from core.models import GeneratedQuestionSet, GeneratedQuizQuestion
    gen_set = GeneratedQuestionSet.objects.create(
        quiz=quiz, student=student, submission=submission, status=status,
        approvedAt=timezone.now() if status == 'approved' else None)
    if questions:
        section = quiz.generatedSections.first()
        GeneratedQuizQuestion.objects.create(
            set=gen_set, section=section, questionType='multiple_choice',
            text='About your code?', points=Decimal('3'), sortKey=0,
            choicesData=[{'text': 'Right', 'isCorrect': True, 'feedback': ''},
                         {'text': 'Wrong', 'isCorrect': False, 'feedback': ''}],
            referenceSolution="Correct: 'Right'.")
        GeneratedQuizQuestion.objects.create(
            set=gen_set, section=section, questionType='essay',
            text='Explain it.', points=Decimal('3'), sortKey=1,
            referenceSolution='Key points a correct explanation should cover.')
    return gen_set


# --------------------------------------------------------------------------- #
# Section authoring
# --------------------------------------------------------------------------- #

# _feature_on lives in quiz_helpers (shared with the other quiz test files).


class TestSectionAuthoring:
    def test_staff_can_create_section(self, api_client, gen_setup, monkeypatch):
        _feature_on(monkeypatch)
        api_client.force_authenticate(user=gen_setup['grader'])
        resp = api_client.post('/quizGeneratedSections/', {
            'quiz': gen_setup['quiz'].id, 'systemPrompt': 'Ask about {assignment_name}.',
            'numQuestions': 3, 'pointsPerQuestion': '2.00',
        }, format='json')
        assert resp.status_code == status.HTTP_201_CREATED

    def test_student_cannot_create_section(self, api_client, gen_setup, monkeypatch):
        _feature_on(monkeypatch)
        api_client.force_authenticate(user=gen_setup['students'][0])
        resp = api_client.post('/quizGeneratedSections/', {
            'quiz': gen_setup['quiz'].id, 'systemPrompt': 'x',
        }, format='json')
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_create_blocked_when_feature_disabled(self, api_client, gen_setup):
        # No AI provider is configured in tests, so the feature resolves to disabled.
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post('/quizGeneratedSections/', {
            'quiz': gen_setup['quiz'].id, 'systemPrompt': 'x',
        }, format='json')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'not enabled' in str(resp.data)

    def test_editing_section_allowed_when_feature_disabled(self, api_client, gen_setup):
        # Editing/deleting existing sections stays possible for cleanup after a
        # course turns the feature off.
        api_client.force_authenticate(user=gen_setup['admin'])
        section = gen_setup['section']
        resp = api_client.patch(f'/quizGeneratedSections/{section.id}/',
                                {'numQuestions': 5}, format='json')
        assert resp.status_code == status.HTTP_200_OK
        delete = api_client.delete(f'/quizGeneratedSections/{section.id}/')
        assert delete.status_code == status.HTTP_204_NO_CONTENT

    def test_cannot_reassign_section_into_foreign_course_quiz(self, api_client, gen_setup):
        """Object permissions authorize against the source quiz's course; the writable
        `quiz` FK must not let staff of course A re-parent a section into course B."""
        from core.models import Quiz
        from core.tests.factories import CourseFactory
        with factory.django.mute_signals(post_save):
            course_b = CourseFactory(name="cs999", period="s2026", organization__name="Princeton")
        quiz_b = Quiz.objects.create(course=course_b, title='B quiz')

        api_client.force_authenticate(user=gen_setup['admin'])  # staff of course A only
        section = gen_setup['section']
        resp = api_client.patch(f'/quizGeneratedSections/{section.id}/',
                                {'quiz': quiz_b.id}, format='json')
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        section.refresh_from_db()
        assert section.quiz_id == gen_setup['quiz'].id  # not relocated

    def test_unknown_variable_rejected(self, api_client, gen_setup, monkeypatch):
        _feature_on(monkeypatch)
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post('/quizGeneratedSections/', {
            'quiz': gen_setup['quiz'].id, 'systemPrompt': 'Use {not_a_variable}.',
        }, format='json')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'Unknown variable' in str(resp.data)

    def test_standalone_quiz_sections_follow_prompt_requirements(self, api_client, gen_setup, monkeypatch):
        # Submission-free prompts are allowed on standalone quizzes (the eager path);
        # prompts drawing on assignment/submission data still require attachment.
        from core.models import Quiz
        _feature_on(monkeypatch)
        standalone = Quiz.objects.create(course=gen_setup['course'], title='Standalone')
        api_client.force_authenticate(user=gen_setup['admin'])
        ok = api_client.post('/quizGeneratedSections/', {
            'quiz': standalone.id,
            'systemPrompt': 'Ask {num_questions} conceptual questions about recursion.',
        }, format='json')
        assert ok.status_code == status.HTTP_201_CREATED
        bad = api_client.post('/quizGeneratedSections/', {
            'quiz': standalone.id, 'systemPrompt': 'Ask about {submission_files}.',
        }, format='json')
        assert bad.status_code == status.HTTP_400_BAD_REQUEST
        assert 'attached to an assignment' in str(bad.data)

    def test_cannot_change_assignment_with_sections(self, api_client, gen_setup):
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.patch(f"/quizzes/{gen_setup['quiz'].id}/",
                                {'assignment': None}, format='json')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_prompt_variables_endpoint(self, api_client, gen_setup):
        from core.tests.factories import AssignmentFileFactory
        with factory.django.mute_signals(post_save):
            AssignmentFileFactory(assignment=gen_setup['assignment'], name='main.py', data='x')
        api_client.force_authenticate(user=gen_setup['grader'])
        resp = api_client.get(f"/quizzes/{gen_setup['quiz'].id}/promptVariables/")
        assert resp.status_code == status.HTTP_200_OK
        tokens = {e['token'] for e in resp.data}
        assert '{submission_files}' in tokens
        assert '{assignment_file:main.py}' in tokens

    def test_prompt_templates_endpoint(self, api_client, gen_setup):
        """Starter templates for the prompt editor: staff-only, each with the fields the
        picker renders."""
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.get(f"/quizzes/{gen_setup['quiz'].id}/promptTemplates/")
        assert resp.status_code == status.HTTP_200_OK
        assert len(resp.data) > 0
        for tpl in resp.data:
            assert {'key', 'label', 'description', 'attachedOnly', 'questionTypes', 'text'} \
                <= set(tpl.keys())

        api_client.force_authenticate(user=gen_setup['students'][0])
        assert api_client.get(f"/quizzes/{gen_setup['quiz'].id}/promptTemplates/") \
            .status_code == status.HTTP_403_FORBIDDEN

    def test_prompt_variables_forbidden_for_students(self, api_client, gen_setup):
        api_client.force_authenticate(user=gen_setup['students'][0])
        resp = api_client.get(f"/quizzes/{gen_setup['quiz'].id}/promptVariables/")
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# Submission signal
# --------------------------------------------------------------------------- #

class TestSubmissionSignal:
    def _fire(self, submission, monkeypatch, created=True, update_fields=None):
        from core.models import Submission
        from core.signals import auto_generate_personalized_quiz
        calls = []
        monkeypatch.setattr('core.tasks.generate_personalized_quiz_sets.apply_async',
                            lambda *a, **kw: calls.append((a, kw)))
        auto_generate_personalized_quiz(Submission, submission, created,
                                        update_fields=update_fields)
        return calls

    def test_enqueues_on_creation(self, gen_setup, monkeypatch):
        assert len(self._fire(gen_setup['submission'], monkeypatch, created=True)) == 1

    def test_enqueues_on_file_upload(self, gen_setup, monkeypatch):
        calls = self._fire(gen_setup['submission'], monkeypatch, created=False,
                           update_fields=frozenset({'dateUploaded'}))
        assert len(calls) == 1

    def test_skips_other_updates(self, gen_setup, monkeypatch):
        calls = self._fire(gen_setup['submission'], monkeypatch, created=False,
                           update_fields=frozenset({'grade'}))
        assert calls == []

    def test_skips_without_generated_sections(self, gen_setup, monkeypatch):
        gen_setup['section'].delete()
        assert self._fire(gen_setup['submission'], monkeypatch, created=True) == []

    def test_skips_draft_quizzes(self, gen_setup, monkeypatch):
        """Uploads never spend AI credits on an unpublished quiz — publishing catches
        missing sets up instead."""
        gen_setup['quiz'].isPublished = False
        gen_setup['quiz'].save()
        assert self._fire(gen_setup['submission'], monkeypatch, created=True) == []

    def test_skips_manual_generation_quiz(self, gen_setup, monkeypatch):
        """Manual-generation quizzes never generate on upload — staff trigger generation
        explicitly (or via the scheduled generationDate run)."""
        gen_setup['quiz'].manualGeneration = True
        gen_setup['quiz'].save()
        assert self._fire(gen_setup['submission'], monkeypatch, created=True) == []


class TestSectionCreatedSignal:
    def _fire(self, section, monkeypatch, created=True):
        from core.models import QuizGeneratedSection
        from core.signals import backfill_generated_sets_on_section_created
        calls = []
        monkeypatch.setattr('core.tasks.backfill_personalized_quiz_sets.delay',
                            lambda *a, **kw: calls.append((a, kw)))
        backfill_generated_sets_on_section_created(QuizGeneratedSection, section, created)
        return calls

    def test_published_quiz_backfills(self, gen_setup, monkeypatch):
        assert len(self._fire(gen_setup['section'], monkeypatch)) == 1

    def test_draft_quiz_skipped(self, gen_setup, monkeypatch):
        """Cloning recreates sections on unpublished clones — a submission-free prompt
        would otherwise fan AI generation out to every enrolled student of a draft."""
        gen_setup['quiz'].isPublished = False
        gen_setup['quiz'].save()
        assert self._fire(gen_setup['section'], monkeypatch) == []

    def test_manual_generation_quiz_skipped(self, gen_setup, monkeypatch):
        gen_setup['quiz'].manualGeneration = True
        gen_setup['quiz'].save()
        assert self._fire(gen_setup['section'], monkeypatch) == []

    def test_publish_catchup_queues_missing_only_backfill(self, api_client, gen_setup, monkeypatch):
        """Publishing a quiz with sections backfills students without a set — missing_only,
        so a republish never regenerates sets a reviewer may have edited."""
        quiz = gen_setup['quiz']
        quiz.isPublished = False
        quiz.save()
        calls = []
        monkeypatch.setattr('core.tasks.backfill_personalized_quiz_sets.delay',
                            lambda *a, **kw: calls.append((a, kw)))
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.patch(f'/quizzes/{quiz.id}/', {'isPublished': True}, format='json')
        assert resp.status_code == status.HTTP_200_OK
        assert len(calls) == 1
        assert calls[0][1]['missing_only'] is True
        # A non-publish edit doesn't re-queue.
        api_client.patch(f'/quizzes/{quiz.id}/', {'title': 'Renamed'}, format='json')
        assert len(calls) == 1

    def test_publish_catchup_skipped_for_manual_generation(self, api_client, gen_setup, monkeypatch):
        """Publishing a manual-generation quiz queues nothing — staff generate explicitly
        (a generationDate in the past fires on the next sweep tick instead)."""
        quiz = gen_setup['quiz']
        quiz.isPublished = False
        quiz.manualGeneration = True
        quiz.save()
        calls = []
        monkeypatch.setattr('core.tasks.backfill_personalized_quiz_sets.delay',
                            lambda *a, **kw: calls.append((a, kw)))
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.patch(f'/quizzes/{quiz.id}/', {'isPublished': True}, format='json')
        assert resp.status_code == status.HTTP_200_OK
        assert calls == []


# --------------------------------------------------------------------------- #
# Generation task
# --------------------------------------------------------------------------- #

class TestParseJsonQuestions:
    """The model is asked for a bare JSON array; the parser tolerates the usual ways that
    contract gets broken so a run isn't failed over formatting."""

    @pytest.mark.parametrize('text', [
        QUESTIONS_JSON,
        f'```json\n{QUESTIONS_JSON}\n```',
        f'Here are the questions:\n\n```json\n{QUESTIONS_JSON}\n```\n\nLet me know if you need more.',
        f'Here are the questions: {QUESTIONS_JSON} Let me know.',
        json.dumps({'questions': json.loads(QUESTIONS_JSON)}),
        f'{QUESTIONS_JSON}\n{QUESTIONS_JSON}',
    ], ids=['bare', 'fenced', 'prose+fenced', 'prose+inline', 'object-wrapper', 'repeated'])
    def test_tolerated_shapes(self, text):
        from core.services.ai_json import parse_json_questions
        assert parse_json_questions(text) == json.loads(QUESTIONS_JSON)

    def test_unescaped_quotes_inside_strings(self):
        """Real failure seen in production: the model left the quotes in a code snippet
        unescaped inside a description string (invalid JSON); the repair keeps them."""
        from core.services.ai_json import parse_json_questions
        text = ('```json\n[\n  {\n    "type": "code",\n    "text": "Filter it.",\n'
                '    "description": "You wrote:\\n\\n```python\\nresult = baby_names[(baby_names["Year"] == 2000) & \\n'
                '        (baby_names["Count"] > 3000)]\\n```",\n'
                '    "reference_solution": "pa = baby_names[(baby_names[\\"State\\"] == \\"PA\\")]"\n  }\n]\n```')
        [q] = parse_json_questions(text)
        assert 'baby_names["Year"] == 2000' in q['description']
        assert q['reference_solution'] == 'pa = baby_names[(baby_names["State"] == "PA")]'

    def test_literal_newline_inside_string(self):
        from core.services.ai_json import parse_json_questions
        assert parse_json_questions('[{"type": "essay", "text": "Line one\nline two"}]') == [
            {'type': 'essay', 'text': 'Line one\nline two'}]

    @pytest.mark.parametrize('text', ['', 'Sorry, I cannot help with that.', '```json\n[oops\n```'])
    def test_no_json_raises(self, text):
        from core.services.ai_json import parse_json_questions
        with pytest.raises(ValueError):
            parse_json_questions(text)


class TestGenerationTask:
    def test_generates_ready_set(self, gen_setup, monkeypatch):
        _mock_ai(monkeypatch)
        _run_task(gen_setup['submission'])
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        assert gen_set.status == 'ready'
        assert gen_set.submission_id == gen_setup['submission'].id
        questions = list(gen_set.questions.all())
        assert len(questions) == 2
        # Points come from the section (not the model output); order is preserved.
        assert all(q.points == Decimal('3') for q in questions)
        assert questions[0].questionType == 'multiple_choice'
        assert questions[0].choicesData[0]['isCorrect'] is True
        # The model's Markdown description (e.g. the referenced code excerpt) is kept.
        assert questions[0].description.startswith('```python')
        assert questions[1].description == ''
        # The grader-only answer key is stored on the row.
        assert questions[0].referenceSolution.startswith("Correct: 'A list'")
        assert questions[1].referenceSolution.startswith('A correct answer covers')
        # The resolved per-section prompt is recorded for staff review.
        section = gen_setup['quiz'].generatedSections.first()
        assert gen_set.generationMetadata['sections'] == [{
            'sectionId': section.id,
            'sectionName': section.name or '',
            'prompt': f'resolved prompt for section {section.id}',
        }]

    def test_missing_reference_solution_does_not_fail_generation(self, gen_setup, monkeypatch):
        # A stale prompt variant (or a model that ignores the instruction) that omits
        # reference_solution must still produce a usable set — just with an empty key.
        _mock_ai(monkeypatch, json_text=json.dumps([
            {'type': 'essay', 'text': 'Explain your loop.', 'points': 5, 'choices': []},
        ]))
        _run_task(gen_setup['submission'])
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        assert gen_set.status == 'ready'
        assert gen_set.questions.get().referenceSolution == ''

    def test_draft_quiz_not_generated_by_submission_run(self, gen_setup, monkeypatch):
        """The signal fan-out (no quiz_id) never generates for drafts; staff actions that
        scope to a quiz_id still can (preparing an unpublished quiz for review)."""
        _mock_ai(monkeypatch)
        gen_setup['quiz'].isPublished = False
        gen_setup['quiz'].save()
        _run_task(gen_setup['submission'])
        assert not gen_setup['quiz'].generatedSets.exists()
        _run_task(gen_setup['submission'], quiz_id=gen_setup['quiz'].id)
        assert gen_setup['quiz'].generatedSets.filter(status='ready').exists()

    def test_manual_quiz_not_generated_by_submission_run(self, gen_setup, monkeypatch):
        """The signal fan-out (no quiz_id) skips manual-generation quizzes; staff actions
        and the scheduled run scope to a quiz_id and still generate."""
        _mock_ai(monkeypatch)
        gen_setup['quiz'].manualGeneration = True
        gen_setup['quiz'].save()
        _run_task(gen_setup['submission'])
        assert not gen_setup['quiz'].generatedSets.exists()
        _run_task(gen_setup['submission'], quiz_id=gen_setup['quiz'].id)
        assert gen_setup['quiz'].generatedSets.filter(status='ready').exists()

    def test_keyless_choice_question_skipped(self, gen_setup, monkeypatch):
        """A generated auto-graded question with no correct choice would mark every student
        wrong (worst under autoPublishGenerated, where nobody reviews it) — it is dropped."""
        _mock_ai(monkeypatch, json.dumps([
            {'type': 'multiple_choice', 'text': 'Keyless?',
             'choices': [{'text': 'a', 'is_correct': False}, {'text': 'b', 'is_correct': False}]},
            {'type': 'multiple_choice', 'text': 'Keyed?',
             'choices': [{'text': 'a', 'is_correct': True}, {'text': 'b'}]},
        ]))
        _run_task(gen_setup['submission'])
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        assert gen_set.status == 'ready'
        assert [q.text for q in gen_set.questions.all()] == ['Keyed?']

    def test_feature_disabled_skips(self, gen_setup, monkeypatch):
        _mock_ai(monkeypatch)
        monkeypatch.setattr('core.services.ai_service.AIService.is_feature_enabled',
                            lambda self, key: False)
        _run_task(gen_setup['submission'])
        assert gen_setup['quiz'].generatedSets.count() == 0

    def test_auto_publish_approves(self, gen_setup, monkeypatch):
        quiz = gen_setup['quiz']
        quiz.autoPublishGenerated = True
        quiz.save()
        _mock_ai(monkeypatch)
        _run_task(gen_setup['submission'])
        gen_set = quiz.generatedSets.get(student=gen_setup['students'][0])
        assert gen_set.status == 'approved'
        assert gen_set.approvedBy is None and gen_set.approvedAt is not None

    def test_approved_set_not_regenerated(self, gen_setup, monkeypatch):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0], status='approved')
        question_ids = set(gen_set.questions.values_list('id', flat=True))
        _mock_ai(monkeypatch)
        _run_task(gen_setup['submission'])
        gen_set.refresh_from_db()
        assert gen_set.status == 'approved'
        assert set(gen_set.questions.values_list('id', flat=True)) == question_ids

    def test_force_regenerates_approved_set(self, gen_setup, monkeypatch):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0],
                            submission=gen_setup['submission'], status='approved')
        _mock_ai(monkeypatch)
        _run_task(gen_setup['submission'], quiz_id=gen_setup['quiz'].id, force=True)
        gen_set.refresh_from_db()
        assert gen_set.status == 'ready'
        assert gen_set.approvedAt is None

    def test_ready_set_regenerated_on_resubmission(self, gen_setup, monkeypatch):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0], status='ready')
        old_ids = set(gen_set.questions.values_list('id', flat=True))
        _mock_ai(monkeypatch)
        _run_task(gen_setup['submission'])
        gen_set.refresh_from_db()
        assert gen_set.status == 'ready'
        assert set(gen_set.questions.values_list('id', flat=True)).isdisjoint(old_ids)

    def test_failed_generation_marks_failed(self, gen_setup, monkeypatch):
        _mock_ai(monkeypatch, success=False)
        _run_task(gen_setup['submission'])
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        assert gen_set.status == 'failed'
        assert 'model unavailable' in gen_set.errorMessage
        assert gen_set.questions.count() == 0

    def test_failed_regeneration_preserves_previous_questions(self, gen_setup, monkeypatch):
        """An AI failure during force-regen keeps the set's existing (possibly hand-edited)
        questions and returns it to review — instead of leaving it failed and empty."""
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0],
                            submission=gen_setup['submission'], status='approved')
        old_ids = set(gen_set.questions.values_list('id', flat=True))
        _mock_ai(monkeypatch, success=False)
        _run_task(gen_setup['submission'], quiz_id=gen_setup['quiz'].id, force=True,
                  student_id=gen_setup['students'][0].id)
        gen_set.refresh_from_db()
        assert gen_set.status == 'ready'    # back to review, never silently re-approved
        assert 'model unavailable' in gen_set.errorMessage
        assert set(gen_set.questions.values_list('id', flat=True)) == old_ids

    def test_unparseable_output_is_retried(self, gen_setup, monkeypatch):
        """A malformed model output is re-asked before the set fails; the successful
        retry leaves no raw_output behind."""
        calls = _mock_ai_sequence(monkeypatch, ['Sorry, I cannot do that.', QUESTIONS_JSON])
        _run_task(gen_setup['submission'])
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        assert gen_set.status == 'ready'
        assert len(calls) == 2
        assert gen_set.questions.count() == 2
        assert gen_set.errorMessage == ''
        assert 'raw_output' not in gen_set.generationMetadata
        # The resolved prompt is recorded once per section, not once per attempt.
        assert len(gen_set.generationMetadata['sections']) == 1

    def test_unparseable_output_after_retries_fails_with_raw_output(self, gen_setup, monkeypatch):
        """When every attempt is unusable the set fails, the error says how many times it
        was tried, and the last model output is kept on the set for inspection."""
        from core.tasks import _GENERATION_ATTEMPTS
        calls = _mock_ai_sequence(monkeypatch, ['not json at all'] * _GENERATION_ATTEMPTS)
        _run_task(gen_setup['submission'])
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        assert gen_set.status == 'failed'
        assert len(calls) == _GENERATION_ATTEMPTS
        assert 'Could not parse' in gen_set.errorMessage
        assert f'{_GENERATION_ATTEMPTS} attempts' in gen_set.errorMessage
        assert gen_set.generationMetadata['raw_output'] == 'not json at all'

    def test_no_usable_questions_is_retried(self, gen_setup, monkeypatch):
        """Valid JSON with nothing usable (e.g. only keyless choice questions) counts as an
        output problem too — re-asked, and the last output kept on failure."""
        from core.tasks import _GENERATION_ATTEMPTS
        keyless = json.dumps([{'type': 'multiple_choice', 'text': 'Keyless?',
                               'choices': [{'text': 'a'}, {'text': 'b'}]}])
        calls = _mock_ai_sequence(monkeypatch, [keyless] * _GENERATION_ATTEMPTS)
        _run_task(gen_setup['submission'])
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        assert gen_set.status == 'failed'
        assert len(calls) == _GENERATION_ATTEMPTS
        assert 'no usable questions' in gen_set.errorMessage
        assert gen_set.generationMetadata['raw_output'] == keyless

    def test_provider_failure_is_not_retried(self, gen_setup, monkeypatch):
        """Provider errors (rate limit, auth, outage) aren't re-asked here — the SDK
        clients retry transient failures themselves — and leave no raw_output."""
        calls = _mock_ai_sequence(monkeypatch, [None, QUESTIONS_JSON])
        _run_task(gen_setup['submission'])
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        assert gen_set.status == 'failed'
        assert len(calls) == 1
        assert 'model unavailable' in gen_set.errorMessage
        assert 'raw_output' not in gen_set.generationMetadata

    def test_stale_batch_discards_results(self, gen_setup, monkeypatch):
        from core.models import GeneratedQuestionSet

        def newer_run_claims(section, submission):
            # While "our" run is generating, a newer run re-claims the set.
            GeneratedQuestionSet.objects.filter(quiz=section.quiz).update(
                generationBatch=uuid.uuid4())

        _mock_ai(monkeypatch, side_effect=newer_run_claims)
        _run_task(gen_setup['submission'])
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        # The stale run must not have written: still generating (as the newer run left it).
        assert gen_set.status == 'generating'
        assert gen_set.questions.count() == 0

    def test_code_question_defaults_environment_language(self, gen_setup, monkeypatch):
        from core.models import Environment
        Environment.objects.create(assignment=gen_setup['assignment'], language='python-3')
        _mock_ai(monkeypatch, json_text=json.dumps([
            {'type': 'code', 'text': 'Refactor your helper.', 'points': 3,
             'starter_code': 'def helper():\n    pass', 'choices': []},
        ]))
        _run_task(gen_setup['submission'])
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        question = gen_set.questions.get()
        assert question.questionType == 'code'
        assert question.language == 'python-3'
        assert question.starterCode == 'def helper():\n    pass'

    def test_group_submission_gets_one_set_per_member(self, gen_setup, monkeypatch):
        gen_setup['submission'].students.add(gen_setup['students'][1])
        _mock_ai(monkeypatch)
        _run_task(gen_setup['submission'])
        sets = list(gen_setup['quiz'].generatedSets.all())
        assert len(sets) == 2
        assert sets[0].generationBatch == sets[1].generationBatch
        assert all(s.questions.count() == 2 for s in sets)


class TestGenerateForStudent:
    """POST /quizzes/{id}/generateForStudent/ — staff-triggered generation for one
    student (testing a prompt / backfilling), scoped so group-mates are untouched."""

    def _eager_task(self, monkeypatch):
        from core.tasks import generate_personalized_quiz_sets
        monkeypatch.setattr('core.tasks.generate_personalized_quiz_sets.delay',
                            lambda *a, **kw: generate_personalized_quiz_sets(*a, **kw))

    def _post(self, api_client, gen_setup, email, force=False):
        return api_client.post(f"/quizzes/{gen_setup['quiz'].id}/generateForStudent/",
                               {'student': email, 'force': force}, format='json')

    def test_generates_for_one_student(self, api_client, gen_setup, monkeypatch):
        _mock_ai(monkeypatch)
        self._eager_task(monkeypatch)
        # Group submission: generating for students[0] must not touch students[1].
        gen_setup['submission'].students.add(gen_setup['students'][1])
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup, gen_setup['students'][0].email)
        assert resp.status_code == status.HTTP_202_ACCEPTED
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        assert gen_set.status == 'ready'
        assert gen_set.questions.count() == 2
        assert not gen_setup['quiz'].generatedSets.filter(
            student=gen_setup['students'][1]).exists()

    def test_approved_needs_force(self, api_client, gen_setup, monkeypatch):
        _mock_ai(monkeypatch)
        self._eager_task(monkeypatch)
        _make_set(gen_setup['quiz'], gen_setup['students'][0],
                  submission=gen_setup['submission'], status='approved')
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup, gen_setup['students'][0].email)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'force' in str(resp.data)
        resp = self._post(api_client, gen_setup, gen_setup['students'][0].email, force=True)
        assert resp.status_code == status.HTTP_202_ACCEPTED
        gen_set = gen_setup['quiz'].generatedSets.get(student=gen_setup['students'][0])
        assert gen_set.status == 'ready'
        assert gen_set.approvedAt is None

    def test_generate_blocked_after_attempt(self, api_client, gen_setup, monkeypatch):
        """Same guard as regenerate: force-regen would delete the questions a submitted
        attempt's grading key references."""
        from core.models import QuizAttempt
        _mock_ai(monkeypatch)
        student = gen_setup['students'][0]
        _make_set(gen_setup['quiz'], student, submission=gen_setup['submission'],
                  status='approved')
        QuizAttempt.objects.create(quiz=gen_setup['quiz'], student=student, attemptNumber=1,
                                   startedAt=timezone.now())
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup, student.email, force=True)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_requires_submission(self, api_client, gen_setup, monkeypatch):
        _mock_ai(monkeypatch)
        api_client.force_authenticate(user=gen_setup['admin'])
        # students[1] never submitted.
        resp = self._post(api_client, gen_setup, gen_setup['students'][1].email)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'no submission' in str(resp.data)

    def test_unknown_or_unenrolled_email_rejected(self, api_client, gen_setup, monkeypatch):
        _mock_ai(monkeypatch)
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup, 'nobody@example.com')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        resp = self._post(api_client, gen_setup, gen_setup['grader'].email)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_feature_disabled_rejected(self, api_client, gen_setup):
        # No AI provider configured in tests → feature resolves to disabled.
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup, gen_setup['students'][0].email)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'not enabled' in str(resp.data)

    def test_generation_is_admin_only_even_with_review_flag(self, api_client, gen_setup, monkeypatch):
        # The review flag grants graders review/edit/approve — generation spends AI credits
        # and needs the separate gradersCanGenerate opt-in (and even that grants only the
        # bounded Generate-missing backfill, tested in TestBackfill).
        _mock_ai(monkeypatch)
        self._eager_task(monkeypatch)
        api_client.force_authenticate(user=gen_setup['grader'])
        resp = self._post(api_client, gen_setup, gen_setup['students'][0].email)
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        gen_setup['quiz'].gradersCanReviewGenerated = True
        gen_setup['quiz'].save()
        resp = self._post(api_client, gen_setup, gen_setup['students'][0].email)
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert api_client.post(f"/quizzes/{gen_setup['quiz'].id}/generateMissing/") \
            .status_code == status.HTTP_403_FORBIDDEN

    def test_student_forbidden(self, api_client, gen_setup, monkeypatch):
        _mock_ai(monkeypatch)
        api_client.force_authenticate(user=gen_setup['students'][0])
        resp = self._post(api_client, gen_setup, gen_setup['students'][0].email)
        assert resp.status_code == status.HTTP_403_FORBIDDEN


class TestGeneratedQuestionEditDelete:
    """Per-question review edits vs delete once the student has attempted."""

    def test_delete_blocked_after_attempt(self, api_client, gen_setup):
        from core.models import QuizAttempt
        student = gen_setup['students'][0]
        gen_set = _make_set(gen_setup['quiz'], student, submission=gen_setup['submission'],
                            status='approved')
        question = gen_set.questions.first()
        QuizAttempt.objects.create(quiz=gen_setup['quiz'], student=student, attemptNumber=1,
                                   startedAt=timezone.now())

        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.delete(f'/generatedQuizQuestions/{question.id}/')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert gen_set.questions.filter(pk=question.id).exists()  # not deleted

    def test_delete_allowed_before_attempt(self, api_client, gen_setup):
        student = gen_setup['students'][0]
        gen_set = _make_set(gen_setup['quiz'], student, submission=gen_setup['submission'],
                            status='ready')
        question = gen_set.questions.first()

        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.delete(f'/generatedQuizQuestions/{question.id}/')
        assert resp.status_code == status.HTTP_204_NO_CONTENT


# --------------------------------------------------------------------------- #
# Eager (submission-free) generation
# --------------------------------------------------------------------------- #

class TestEagerGeneration:
    def _standalone_quiz(self, gen_setup, prompt='Ask conceptual questions.'):
        from core.models import Quiz, QuizGeneratedSection
        quiz = Quiz.objects.create(course=gen_setup['course'], title='Concepts', isPublished=True)
        section = QuizGeneratedSection.objects.create(
            quiz=quiz, name='Concepts', systemPrompt=prompt, numQuestions=2,
            pointsPerQuestion=Decimal('3'))
        return quiz, section

    def test_submissionless_task_generates_ready_set(self, gen_setup, monkeypatch):
        from core.tasks import generate_personalized_quiz_sets
        _mock_ai(monkeypatch)
        quiz, _ = self._standalone_quiz(gen_setup)
        student = gen_setup['students'][0]
        generate_personalized_quiz_sets(quiz_id=quiz.id, student_id=student.id)
        gen_set = quiz.generatedSets.get(student=student)
        assert gen_set.status == 'ready'
        assert gen_set.submission_id is None
        assert gen_set.questions.count() == 2

    def test_backfill_targets_all_enrolled_students_when_submission_free(self, gen_setup, monkeypatch):
        from core.tasks import enqueue_personalized_backfill, generate_personalized_quiz_sets
        _mock_ai(monkeypatch)
        monkeypatch.setattr('core.tasks.generate_personalized_quiz_sets.delay',
                            lambda *a, **kw: generate_personalized_quiz_sets(*a, **kw))
        quiz, _ = self._standalone_quiz(gen_setup)
        enrolled = gen_setup['course'].students.count()
        queued = enqueue_personalized_backfill(quiz)
        assert queued == enrolled
        assert quiz.generatedSets.count() == enrolled
        assert set(quiz.generatedSets.values_list('status', flat=True)) == {'ready'}
        # The submission-seeded quiz still only targets submitters.
        assert enqueue_personalized_backfill(gen_setup['quiz'], dry_run=True) == 1

    def test_student_takes_standalone_generated_quiz_once_approved(self, api_client, gen_setup, monkeypatch):
        from core.tasks import generate_personalized_quiz_sets
        _mock_ai(monkeypatch)
        quiz, _ = self._standalone_quiz(gen_setup)
        student = gen_setup['students'][0]
        generate_personalized_quiz_sets(quiz_id=quiz.id, student_id=student.id)
        gen_set = quiz.generatedSets.get(student=student)
        api_client.force_authenticate(user=student)
        # Locked until the set is approved…
        blocked = api_client.post('/quizAttempts/', {'quiz': quiz.id}, format='json')
        assert blocked.status_code == status.HTTP_403_FORBIDDEN
        gen_set.status = 'approved'
        gen_set.save()
        # …then the standalone quiz opens with the generated questions — no submission ever.
        start = api_client.post('/quizAttempts/', {'quiz': quiz.id}, format='json')
        assert start.status_code == status.HTTP_201_CREATED
        assert len(start.data['responses']) == 2

    def test_upload_only_fills_gaps_on_submission_free_quiz(self, gen_setup, monkeypatch):
        from core.models import Quiz, QuizGeneratedSection
        from core.tasks import generate_personalized_quiz_sets
        _mock_ai(monkeypatch)
        quiz = Quiz.objects.create(course=gen_setup['course'], title='Concepts',
                                   assignment=gen_setup['assignment'], isPublished=True)
        # Mute the section's post_save backfill: this test builds the "already reviewed"
        # state itself below. Without muting, an eager Celery (CELERY_TASK_ALWAYS_EAGER,
        # which CI sets) runs the backfill inline and creates the set first, so _make_set
        # then collides on the (quiz, student) unique constraint.
        with factory.django.mute_signals(post_save):
            QuizGeneratedSection.objects.create(
                quiz=quiz, name='Concepts', systemPrompt='Ask conceptual questions.',
                numQuestions=2, pointsPerQuestion=Decimal('3'))
        student = gen_setup['students'][0]  # on gen_setup['submission']
        existing = _make_set(quiz, student, status='ready')
        # An upload-triggered run must not clobber the reviewed set (its questions don't
        # depend on the submission)…
        generate_personalized_quiz_sets(gen_setup['submission'].id, quiz_id=quiz.id)
        existing.refresh_from_db()
        assert existing.status == 'ready'
        assert existing.questions.count() == 2
        # …but a student without a set (added to the group later) gets one.
        gen_setup['submission'].students.add(gen_setup['students'][1])
        generate_personalized_quiz_sets(gen_setup['submission'].id, quiz_id=quiz.id)
        assert quiz.generatedSets.filter(student=gen_setup['students'][1]).exists()

    def test_generate_for_student_without_submission(self, api_client, gen_setup, monkeypatch):
        from core.tasks import generate_personalized_quiz_sets
        _mock_ai(monkeypatch)
        monkeypatch.setattr('core.tasks.generate_personalized_quiz_sets.delay',
                            lambda *a, **kw: generate_personalized_quiz_sets(*a, **kw))
        quiz, _ = self._standalone_quiz(gen_setup)
        never_submitted = gen_setup['students'][1]
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post(f"/quizzes/{quiz.id}/generateForStudent/",
                               {'student': never_submitted.email}, format='json')
        assert resp.status_code == status.HTTP_202_ACCEPTED
        assert quiz.generatedSets.get(student=never_submitted).status == 'ready'

    def test_backfill_preview_counts_enrolled_for_submission_free(self, api_client, gen_setup):
        quiz, _ = self._standalone_quiz(gen_setup)
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.get(f"/quizzes/{quiz.id}/backfillPreview/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data['needsSubmission'] is False
        assert resp.data['missing'] == gen_setup['course'].students.count()
        # The submission-seeded quiz reports the old semantics.
        seeded = api_client.get(f"/quizzes/{gen_setup['quiz'].id}/backfillPreview/")
        assert seeded.data['needsSubmission'] is True


# --------------------------------------------------------------------------- #
# Review / approve / publish API + permission gate
# --------------------------------------------------------------------------- #

class TestReviewAPI:
    def test_admin_lists_sets(self, api_client, gen_setup):
        _make_set(gen_setup['quiz'], gen_setup['students'][0])
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.get(f"/quizzes/{gen_setup['quiz'].id}/generatedSets/")
        assert resp.status_code == status.HTTP_200_OK
        assert len(resp.data) == 1
        assert resp.data[0]['questionCount'] == 2

    def test_grader_gate_follows_quiz_flag(self, api_client, gen_setup):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0])
        api_client.force_authenticate(user=gen_setup['grader'])
        assert api_client.get(f"/quizzes/{gen_setup['quiz'].id}/generatedSets/") \
            .status_code == status.HTTP_403_FORBIDDEN
        assert api_client.post(f"/generatedQuestionSets/{gen_set.id}/approve/") \
            .status_code == status.HTTP_403_FORBIDDEN

        gen_setup['quiz'].gradersCanReviewGenerated = True
        gen_setup['quiz'].save()
        assert api_client.get(f"/quizzes/{gen_setup['quiz'].id}/generatedSets/") \
            .status_code == status.HTTP_200_OK
        resp = api_client.post(f"/generatedQuestionSets/{gen_set.id}/approve/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data['status'] == 'approved'

    def test_grader_reads_seed_submission_only_while_review_flag_on(self, api_client, gen_setup):
        # Question review needs the seeding submission for context: an unassigned grader
        # may READ it (and its files) only while a generated-section quiz on the assignment
        # allows grader review — and never write to it.
        sub = gen_setup['submission']
        api_client.force_authenticate(user=gen_setup['grader'])
        assert api_client.get(f"/submissions/{sub.id}/").status_code == status.HTTP_403_FORBIDDEN
        gen_setup['quiz'].gradersCanReviewGenerated = True
        gen_setup['quiz'].save()
        assert api_client.get(f"/submissions/{sub.id}/").status_code == status.HTTP_200_OK
        assert api_client.patch(f"/submissions/{sub.id}/", {'grade': 1}, format='json') \
            .status_code == status.HTTP_403_FORBIDDEN

    def test_student_never_accesses_sets(self, api_client, gen_setup):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0])
        api_client.force_authenticate(user=gen_setup['students'][0])
        assert api_client.get(f"/quizzes/{gen_setup['quiz'].id}/generatedSets/") \
            .status_code == status.HTTP_403_FORBIDDEN
        assert api_client.get(f"/generatedQuestionSets/{gen_set.id}/") \
            .status_code == status.HTTP_403_FORBIDDEN

    def test_approve_requires_questions(self, api_client, gen_setup):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0], questions=False)
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post(f"/generatedQuestionSets/{gen_set.id}/approve/")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_approve_sets_approver(self, api_client, gen_setup):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0])
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post(f"/generatedQuestionSets/{gen_set.id}/approve/")
        assert resp.status_code == status.HTTP_200_OK
        gen_set.refresh_from_db()
        assert gen_set.approvedBy == gen_setup['admin']

    def test_unapprove_blocked_after_attempt(self, api_client, gen_setup):
        from core.models import QuizAttempt
        student = gen_setup['students'][0]
        gen_set = _make_set(gen_setup['quiz'], student, status='approved')
        QuizAttempt.objects.create(quiz=gen_setup['quiz'], student=student, attemptNumber=1,
                                   startedAt=timezone.now())
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post(f"/generatedQuestionSets/{gen_set.id}/unapprove/")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_unapprove_reopens_review(self, api_client, gen_setup):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0], status='approved')
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post(f"/generatedQuestionSets/{gen_set.id}/unapprove/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data['status'] == 'ready'
        # Logged like approve/regenerate — the activity log records both directions.
        from core.models import CourseAuditEvent
        assert CourseAuditEvent.objects.filter(
            quiz=gen_setup['quiz'], event_type='quiz_generated_set_unapproved',
            user=gen_setup['admin']).exists()

    def test_stale_generating_set_can_be_reclaimed(self, api_client, gen_setup, monkeypatch):
        """A worker that died mid-generation leaves the set 'generating' forever (tasks have
        no retries) — after a timeout the claim is presumed dead and regenerate works again
        (a zombie that later completes is discarded by the generationBatch protocol)."""
        from datetime import timedelta
        from core.models import GeneratedQuestionSet
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0],
                            submission=gen_setup['submission'], status='generating',
                            questions=False)
        calls = []
        monkeypatch.setattr('core.tasks.generate_personalized_quiz_sets.delay',
                            lambda *a, **kw: calls.append((a, kw)))
        api_client.force_authenticate(user=gen_setup['admin'])
        # A fresh claim is still refused.
        assert api_client.post(f"/generatedQuestionSets/{gen_set.id}/regenerate/") \
            .status_code == status.HTTP_400_BAD_REQUEST
        # A stale one (worker presumed dead) is reclaimed.
        GeneratedQuestionSet.objects.filter(pk=gen_set.id).update(
            modified=timezone.now() - timedelta(minutes=11))
        resp = api_client.post(f"/generatedQuestionSets/{gen_set.id}/regenerate/")
        assert resp.status_code == status.HTTP_202_ACCEPTED
        assert len(calls) == 1

    def test_regenerate_is_admin_only_even_with_review_flag(self, api_client, gen_setup):
        gen_setup['quiz'].gradersCanReviewGenerated = True
        gen_setup['quiz'].save()
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0],
                            submission=gen_setup['submission'])
        api_client.force_authenticate(user=gen_setup['grader'])
        assert api_client.post(f"/generatedQuestionSets/{gen_set.id}/regenerate/") \
            .status_code == status.HTTP_403_FORBIDDEN

    def test_regenerate_requires_submission(self, api_client, gen_setup):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0], submission=None)
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post(f"/generatedQuestionSets/{gen_set.id}/regenerate/")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_regenerate_blocked_after_attempt(self, api_client, gen_setup):
        """Regenerating deletes the questions a submitted attempt's grading key lives on
        (referenceSolution) — blocked once the student attempted, like unapprove."""
        from core.models import QuizAttempt
        student = gen_setup['students'][0]
        gen_set = _make_set(gen_setup['quiz'], student, submission=gen_setup['submission'],
                            status='approved')
        QuizAttempt.objects.create(quiz=gen_setup['quiz'], student=student, attemptNumber=1,
                                   startedAt=timezone.now())
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post(f"/generatedQuestionSets/{gen_set.id}/regenerate/")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert gen_set.questions.count() == 2   # untouched

    def test_regenerate_enqueues_forced_run(self, api_client, gen_setup, monkeypatch):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0],
                            submission=gen_setup['submission'], status='approved')
        calls = []
        monkeypatch.setattr('core.tasks.generate_personalized_quiz_sets.delay',
                            lambda *a, **kw: calls.append((a, kw)))
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post(f"/generatedQuestionSets/{gen_set.id}/regenerate/")
        assert resp.status_code == status.HTTP_202_ACCEPTED
        assert resp.data['status'] == 'pending'
        assert len(calls) == 1 and calls[0][1]['force'] is True

    def test_publish_all_is_admin_only(self, api_client, gen_setup):
        gen_setup['quiz'].gradersCanReviewGenerated = True
        gen_setup['quiz'].save()
        _make_set(gen_setup['quiz'], gen_setup['students'][0])
        api_client.force_authenticate(user=gen_setup['grader'])
        assert api_client.post(f"/quizzes/{gen_setup['quiz'].id}/publishAllGenerated/") \
            .status_code == status.HTTP_403_FORBIDDEN

    def test_publish_all_approves_ready_sets(self, api_client, gen_setup):
        _make_set(gen_setup['quiz'], gen_setup['students'][0])
        _make_set(gen_setup['quiz'], gen_setup['students'][1], questions=False)  # skipped
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post(f"/quizzes/{gen_setup['quiz'].id}/publishAllGenerated/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data == {'approved': 1, 'skipped': 1}

    def test_edit_generated_question(self, api_client, gen_setup):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0])
        question = gen_set.questions.first()
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.patch(f"/generatedQuizQuestions/{question.id}/", {
            'text': 'Improved stem',
            'choicesData': [{'text': 'A', 'isCorrect': True}, {'text': 'B', 'isCorrect': False}],
        }, format='json')
        assert resp.status_code == status.HTTP_200_OK
        question.refresh_from_db()
        assert question.text == 'Improved stem'
        assert question.choicesData[0] == {'text': 'A', 'isCorrect': True, 'feedback': ''}

    def test_edit_cannot_strip_answer_key(self, api_client, gen_setup):
        """A reviewer edit that leaves an auto-graded question with no correct choice is
        rejected — it would grade every student wrong once published."""
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0])
        question = gen_set.questions.first()    # the multiple_choice one
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.patch(f"/generatedQuizQuestions/{question.id}/", {
            'choicesData': [{'text': 'A', 'isCorrect': False}],
        }, format='json')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_edit_answer_key(self, api_client, gen_setup):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0])
        question = gen_set.questions.first()
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.patch(f"/generatedQuizQuestions/{question.id}/",
                                {'referenceSolution': 'Updated key.'}, format='json')
        assert resp.status_code == status.HTTP_200_OK
        question.refresh_from_db()
        assert question.referenceSolution == 'Updated key.'

    def test_edit_gate_follows_quiz_flag(self, api_client, gen_setup):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0])
        question = gen_set.questions.first()
        api_client.force_authenticate(user=gen_setup['grader'])
        resp = api_client.patch(f"/generatedQuizQuestions/{question.id}/",
                                {'text': 'nope'}, format='json')
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_edit_blocked_while_generating(self, api_client, gen_setup):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0], status='generating')
        question = gen_set.questions.first()
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.patch(f"/generatedQuizQuestions/{question.id}/",
                                {'text': 'nope'}, format='json')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_delete_generated_question(self, api_client, gen_setup):
        gen_set = _make_set(gen_setup['quiz'], gen_setup['students'][0])
        question = gen_set.questions.first()
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.delete(f"/generatedQuizQuestions/{question.id}/")
        assert resp.status_code == status.HTTP_204_NO_CONTENT
        assert gen_set.questions.count() == 1


# --------------------------------------------------------------------------- #
# Student availability + attempt materialization
# --------------------------------------------------------------------------- #

class TestStudentTaking:
    def _available(self, api_client, gen_setup):
        resp = api_client.get(f"/quizAttempts/availableQuizzes/?course={gen_setup['course'].id}")
        assert resp.status_code == status.HTTP_200_OK
        return {q['id']: q for q in resp.data}

    def test_locked_until_set_approved(self, api_client, gen_setup):
        student = gen_setup['students'][0]
        api_client.force_authenticate(user=student)
        quizzes = self._available(api_client, gen_setup)
        assert quizzes[gen_setup['quiz'].id]['availability'] == {
            'isOpen': False, 'reason': 'questions_not_ready'}
        # Starting is blocked too.
        resp = api_client.post('/quizAttempts/', {'quiz': gen_setup['quiz'].id}, format='json')
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert 'questions_not_ready' in resp.data['detail']

        _make_set(gen_setup['quiz'], student, status='approved')
        quizzes = self._available(api_client, gen_setup)
        assert quizzes[gen_setup['quiz'].id]['availability'] == {'isOpen': True, 'reason': 'open'}
        assert quizzes[gen_setup['quiz'].id]['questionCount'] == 2

    def test_question_count_estimates_before_approval(self, api_client, gen_setup):
        api_client.force_authenticate(user=gen_setup['students'][0])
        quizzes = self._available(api_client, gen_setup)
        # No approved set yet: the section's configured count.
        assert quizzes[gen_setup['quiz'].id]['questionCount'] == 2

    def test_attempt_materializes_generated_questions(self, api_client, gen_setup):
        from core.models import Question, QuestionBank, QuizQuestion
        course, quiz, student = gen_setup['course'], gen_setup['quiz'], gen_setup['students'][0]
        # One fixed question + the student's approved generated set (MC + essay).
        bank = QuestionBank.objects.create(course=course, name='Bank')
        fixed = Question.objects.create(course=course, bank=bank, questionType='true_false',
                                        text='Fixed?', points=Decimal('1'))
        fixed.choices.create(text='True', isCorrect=True, sortKey=0)
        fixed.choices.create(text='False', isCorrect=False, sortKey=1)
        QuizQuestion.objects.create(quiz=quiz, question=fixed, sortKey=0)
        _make_set(quiz, student, status='approved')

        api_client.force_authenticate(user=student)
        resp = api_client.post('/quizAttempts/', {'quiz': quiz.id}, format='json')
        assert resp.status_code == status.HTTP_201_CREATED
        responses = resp.data['responses']
        assert len(responses) == 3

        by_text = {r['question']['text']: r for r in responses}
        generated_mc = by_text['About your code?']
        # Generated responses carry no live question id, and choices use synthetic ids.
        assert generated_mc['question']['id'] is None
        choice_ids = [c['id'] for c in generated_mc['question']['choices']]
        assert choice_ids == [1, 2]
        # No answer key or provenance leaks pre-reveal.
        assert 'isCorrect' not in generated_mc['question']['choices'][0]
        payload = resp.content.decode()
        for banned in ('generationMetadata', 'promptVariant', 'generationBatch', '"source"',
                       'referenceSolution', "Correct: 'Right'", 'Key points a correct'):
            assert banned not in payload

        # The DB-level answer-key link is set even though it never reaches the student payload.
        from core.models import QuizResponse
        db_response = QuizResponse.objects.get(id=generated_mc['id'])
        assert db_response.generatedQuestion_id is not None
        assert db_response.generatedQuestion.referenceSolution == "Correct: 'Right'."

        # Answer the generated MC correctly and submit: auto-graded from the snapshot.
        attempt_id = resp.data['id']
        save = api_client.patch(f"/quizAttempts/{attempt_id}/saveAnswer/", {
            'response': generated_mc['id'], 'selectedChoices': [1]}, format='json')
        assert save.status_code == status.HTTP_200_OK
        submit = api_client.post(f"/quizAttempts/{attempt_id}/submit/")
        assert submit.status_code == status.HTTP_200_OK
        # Generated MC (3 pts, correct) counts; essay (3 pts) needs manual grading; the
        # unanswered fixed question (1 pt) is wrong.
        assert Decimal(submit.data['score']) == Decimal('3')
        assert Decimal(submit.data['maxScore']) == Decimal('7')

    def test_edits_after_start_do_not_change_attempt(self, api_client, gen_setup):
        student = gen_setup['students'][0]
        gen_set = _make_set(gen_setup['quiz'], student, status='approved')
        api_client.force_authenticate(user=student)
        resp = api_client.post('/quizAttempts/', {'quiz': gen_setup['quiz'].id}, format='json')
        assert resp.status_code == status.HTTP_201_CREATED
        question = gen_set.questions.first()
        question.text = 'Changed after start'
        question.save()
        again = api_client.get(f"/quizAttempts/{resp.data['id']}/")
        texts = {r['question']['text'] for r in again.data['responses']}
        assert 'Changed after start' not in texts


# --------------------------------------------------------------------------- #
# Backfill: sections created after students already submitted
# --------------------------------------------------------------------------- #

class TestBackfill:
    def _eager_tasks(self, monkeypatch):
        """No broker in tests — run the queued tasks inline (same pattern as
        TestGenerateForStudent)."""
        from core.tasks import backfill_personalized_quiz_sets, generate_personalized_quiz_sets
        monkeypatch.setattr('core.tasks.generate_personalized_quiz_sets.delay',
                            lambda *a, **kw: generate_personalized_quiz_sets(*a, **kw))
        monkeypatch.setattr('core.tasks.backfill_personalized_quiz_sets.delay',
                            lambda *a, **kw: backfill_personalized_quiz_sets(*a, **kw))

    def test_section_created_after_submissions_backfills(self, api_client, gen_setup, monkeypatch):
        """A section added late (students already submitted) generates sets for those
        students immediately — they must not sit on 'being prepared' forever."""
        from core.models import GeneratedQuestionSet, Quiz
        _mock_ai(monkeypatch)
        self._eager_tasks(monkeypatch)
        # Published + automatic mode: the section-created backfill deliberately skips
        # drafts (publishing catches those up) and manual-generation quizzes.
        late_quiz = Quiz.objects.create(course=gen_setup['course'], title='Late quiz',
                                        assignment=gen_setup['assignment'], isPublished=True,
                                        manualGeneration=False)
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post('/quizGeneratedSections/', {
            'quiz': late_quiz.id, 'systemPrompt': 'Ask about {submission_files}.',
            'numQuestions': 2, 'pointsPerQuestion': '3.00',
        }, format='json')
        assert resp.status_code == status.HTTP_201_CREATED

        # Celery runs eagerly in tests: the backfill already generated for the fixture's submitter.
        gen_set = GeneratedQuestionSet.objects.get(quiz=late_quiz, student=gen_setup['students'][0])
        assert gen_set.status == 'ready'
        assert gen_set.questions.count() > 0
        # Students without a submission get nothing.
        assert not GeneratedQuestionSet.objects.filter(
            quiz=late_quiz, student=gen_setup['students'][1]).exists()

    def test_generate_missing_targets_only_students_without_sets(self, api_client, gen_setup, monkeypatch):
        from core.models import GeneratedQuestionSet
        from core.tests.factories import SubmissionFactory
        _mock_ai(monkeypatch)
        self._eager_tasks(monkeypatch)
        # A second submitter, added with signals muted (i.e. generation never ran for them).
        with factory.django.mute_signals(post_save):
            sub2 = SubmissionFactory(assignment=gen_setup['assignment'])
            sub2.students.add(gen_setup['students'][1])
            sub2.dateUploaded = timezone.now()
            sub2.save()
        # The first submitter already has an APPROVED set — it must stay untouched.
        existing = _make_set(gen_setup['quiz'], gen_setup['students'][0],
                             submission=gen_setup['submission'], status='approved')

        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.post(f"/quizzes/{gen_setup['quiz'].id}/generateMissing/", {}, format='json')
        assert resp.status_code == status.HTTP_202_ACCEPTED
        assert resp.data['queued'] == 1

        missing = GeneratedQuestionSet.objects.get(quiz=gen_setup['quiz'],
                                                   student=gen_setup['students'][1])
        assert missing.status == 'ready'
        existing.refresh_from_db()
        assert existing.status == 'approved'  # not regenerated

        # Everyone covered now: a second run queues nothing.
        again = api_client.post(f"/quizzes/{gen_setup['quiz'].id}/generateMissing/", {}, format='json')
        assert again.status_code == status.HTTP_202_ACCEPTED
        assert again.data['queued'] == 0

    def test_graders_can_generate_flag(self, api_client, gen_setup, monkeypatch):
        """gradersCanGenerate is the instructor's opt-in for a grader-run generate → review →
        release workflow: it grants Generate missing only — per-student generate/regenerate
        (which can force-regenerate an approved set) stays admin-only, and students never
        generate regardless of the flag."""
        _mock_ai(monkeypatch)
        self._eager_tasks(monkeypatch)
        quiz = gen_setup['quiz']
        quiz.gradersCanGenerate = True
        quiz.gradersCanReviewGenerated = True
        quiz.save()
        api_client.force_authenticate(user=gen_setup['grader'])
        resp = api_client.post(f"/quizzes/{quiz.id}/generateMissing/", {}, format='json')
        assert resp.status_code == status.HTTP_202_ACCEPTED
        assert resp.data['queued'] == 1
        denied = api_client.post(f"/quizzes/{quiz.id}/generateForStudent/",
                                 {'student': gen_setup['students'][0].email}, format='json')
        assert denied.status_code == status.HTTP_403_FORBIDDEN
        api_client.force_authenticate(user=gen_setup['students'][0])
        assert api_client.post(f"/quizzes/{quiz.id}/generateMissing/", {},
                               format='json').status_code == status.HTTP_403_FORBIDDEN

    def test_generate_missing_permissions_and_feature_gate(self, api_client, gen_setup, monkeypatch):
        # Plain graders (without the gradersCanGenerate flag) and students are blocked.
        api_client.force_authenticate(user=gen_setup['grader'])
        assert api_client.post(f"/quizzes/{gen_setup['quiz'].id}/generateMissing/", {},
                               format='json').status_code == status.HTTP_403_FORBIDDEN
        api_client.force_authenticate(user=gen_setup['students'][0])
        assert api_client.post(f"/quizzes/{gen_setup['quiz'].id}/generateMissing/", {},
                               format='json').status_code == status.HTTP_403_FORBIDDEN
        # Admins get a clear 400 while the AI feature is off (no provider in tests).
        api_client.force_authenticate(user=gen_setup['admin'])
        off = api_client.post(f"/quizzes/{gen_setup['quiz'].id}/generateMissing/", {}, format='json')
        assert off.status_code == status.HTTP_400_BAD_REQUEST
        assert 'not enabled' in str(off.data)

    def test_backfill_preview_counts_without_enqueueing(self, api_client, gen_setup):
        from core.models import GeneratedQuestionSet
        from core.tests.factories import SubmissionFactory
        # Two submitters; the first already has an APPROVED set.
        with factory.django.mute_signals(post_save):
            sub2 = SubmissionFactory(assignment=gen_setup['assignment'])
            sub2.students.add(gen_setup['students'][1])
            sub2.dateUploaded = timezone.now()
            sub2.save()
        _make_set(gen_setup['quiz'], gen_setup['students'][0],
                  submission=gen_setup['submission'], status='approved')

        api_client.force_authenticate(user=gen_setup['admin'])
        resp = api_client.get(f"/quizzes/{gen_setup['quiz'].id}/backfillPreview/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data == {'wouldGenerate': 1, 'missing': 1, 'needsSubmission': True}
        # Pure preview: nothing was queued or created.
        assert GeneratedQuestionSet.objects.filter(quiz=gen_setup['quiz']).count() == 1

        # A non-approved (ready) set still counts for wouldGenerate (a new section would
        # refresh it) but no longer for missing.
        _make_set(gen_setup['quiz'], gen_setup['students'][1], submission=sub2,
                  status='ready', questions=False)
        resp = api_client.get(f"/quizzes/{gen_setup['quiz'].id}/backfillPreview/")
        assert resp.data == {'wouldGenerate': 1, 'missing': 0, 'needsSubmission': True}

        # Graders without the review flag can't peek.
        api_client.force_authenticate(user=gen_setup['grader'])
        assert api_client.get(f"/quizzes/{gen_setup['quiz'].id}/backfillPreview/").status_code \
            == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# Scheduled generation sweep (manualGeneration + generationDate)
# --------------------------------------------------------------------------- #


class TestScheduledGeneration:
    def _run(self, monkeypatch):
        from core.tasks import run_scheduled_quiz_generation
        calls = []
        monkeypatch.setattr('core.tasks.backfill_personalized_quiz_sets.delay',
                            lambda *a, **kw: calls.append((a, kw)))
        return run_scheduled_quiz_generation(), calls

    def _arm(self, quiz, minutes_ago=5):
        quiz.manualGeneration = True
        quiz.generationDate = timezone.now() - timedelta(minutes=minutes_ago)
        quiz.save()

    def test_runs_when_due(self, gen_setup, monkeypatch):
        self._arm(gen_setup['quiz'])
        count, calls = self._run(monkeypatch)
        assert count == 1
        assert calls == [((gen_setup['quiz'].id,), {'missing_only': True})]
        gen_setup['quiz'].refresh_from_db()
        assert gen_setup['quiz'].scheduledGenerationRanAt is not None

    def test_one_shot(self, gen_setup, monkeypatch):
        self._arm(gen_setup['quiz'])
        self._run(monkeypatch)
        count, calls = self._run(monkeypatch)
        assert count == 0 and calls == []

    def test_skips_before_date(self, gen_setup, monkeypatch):
        self._arm(gen_setup['quiz'], minutes_ago=-60)  # an hour in the future
        count, calls = self._run(monkeypatch)
        assert count == 0 and calls == []
        gen_setup['quiz'].refresh_from_db()
        assert gen_setup['quiz'].scheduledGenerationRanAt is None

    def test_rearm_when_date_moved_forward(self, gen_setup, monkeypatch):
        """Moving generationDate past the stamp re-arms the one-shot — missing_only keeps
        the re-run scoped to students who gained a submission since."""
        self._arm(gen_setup['quiz'])
        self._run(monkeypatch)
        gen_setup['quiz'].refresh_from_db()
        first_stamp = gen_setup['quiz'].scheduledGenerationRanAt
        # A date at/before the stamp stays quiet; one after it fires again.
        gen_setup['quiz'].generationDate = first_stamp
        gen_setup['quiz'].save()
        assert self._run(monkeypatch) == (0, [])
        # After the stamp but not in the future — the sweep only fires once the date passes.
        gen_setup['quiz'].generationDate = timezone.now()
        gen_setup['quiz'].save()
        count, calls = self._run(monkeypatch)
        assert count == 1 and len(calls) == 1

    def test_skips_draft_non_manual_archived_and_sectionless(self, gen_setup, monkeypatch):
        quiz = gen_setup['quiz']
        past = timezone.now() - timedelta(minutes=5)

        quiz.generationDate = past  # date set but automatic mode
        quiz.save()
        assert self._run(monkeypatch) == (0, [])

        self._arm(quiz)
        quiz.isPublished = False
        quiz.save()
        assert self._run(monkeypatch) == (0, [])

        quiz.isPublished = True
        quiz.save()
        gen_setup['course'].archived = True
        gen_setup['course'].save()
        assert self._run(monkeypatch) == (0, [])

        gen_setup['course'].archived = False
        gen_setup['course'].save()
        gen_setup['section'].delete()
        assert self._run(monkeypatch) == (0, [])

    def test_failure_is_isolated(self, gen_setup, monkeypatch):
        """One quiz whose enqueue blows up must not strand the other due quizzes."""
        from core.models import Quiz, QuizGeneratedSection
        from core.tasks import run_scheduled_quiz_generation
        self._arm(gen_setup['quiz'])
        other = Quiz.objects.create(course=gen_setup['course'], title='Zecond quiz',
                                    assignment=gen_setup['assignment'], isPublished=True)
        QuizGeneratedSection.objects.create(quiz=other, systemPrompt='Ask about {submission_files}.')
        self._arm(other)

        calls = []

        def flaky_delay(quiz_id, **kw):
            if quiz_id == gen_setup['quiz'].id:
                raise RuntimeError('broker down')
            calls.append(quiz_id)
        monkeypatch.setattr('core.tasks.backfill_personalized_quiz_sets.delay', flaky_delay)
        count = run_scheduled_quiz_generation()
        assert count == 1
        assert calls == [other.id]
        other.refresh_from_db()
        assert other.scheduledGenerationRanAt is not None


# --------------------------------------------------------------------------- #
# Prompt test previews (the section modal's Test button)
# --------------------------------------------------------------------------- #


class TestPreviewGeneratedSection:
    def _run_task_inline(self, monkeypatch):
        from core.tasks import preview_generated_section as task_fn

        def run_inline(*a, **kw):
            task_fn(*a, **kw)
            return type('R', (), {'id': 'task-inline'})()
        monkeypatch.setattr('core.tasks.preview_generated_section.delay', run_inline)

    def _mock_ai_capture(self, monkeypatch, json_text=QUESTIONS_JSON, calls=None):
        """Like _mock_ai, but records (section, submission, demo_files) per call."""
        from core.services.ai_service import GenerationResult

        async def mock_generate(self, section, submission, demo_files=None):
            if calls is not None:
                calls.append((section, submission, demo_files))
            return GenerationResult(text=json_text, success=True, input_tokens=10,
                                    output_tokens=20, resolved_prompt='the resolved prompt')

        monkeypatch.setattr(
            'core.services.ai_service.AIService.generate_personalized_quiz_questions',
            mock_generate)
        _enable_ai(monkeypatch)

    def _post(self, api_client, quiz, **payload):
        body = {'systemPrompt': 'Ask about {submission_files}.', 'numQuestions': 2}
        body.update(payload)
        return api_client.post(f'/quizzes/{quiz.id}/previewGeneratedSection/', body,
                               format='json')

    def test_random_seed_completes_with_example_questions(self, api_client, gen_setup, monkeypatch):
        calls = []
        self._mock_ai_capture(monkeypatch, calls=calls)
        self._run_task_inline(monkeypatch)

        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup['quiz'])
        assert resp.status_code == status.HTTP_202_ACCEPTED
        assert resp.data['quiz'] == gen_setup['quiz'].id

        poll = api_client.get(f"/quizSuggestionJobs/{resp.data['id']}/")
        assert poll.status_code == status.HTTP_200_OK
        assert poll.data['status'] == 'completed'
        assert poll.data['createdCount'] == 2
        data = poll.data['resultData']
        assert data['seed'] == 'random'
        assert data['sampleStudent'] == gen_setup['students'][0].email
        assert data['resolvedPrompt'] == 'the resolved prompt'
        assert [q['questionType'] for q in data['questions']] == ['multiple_choice', 'essay']
        assert data['questions'][0]['choicesData'][0] == {
            'text': 'A list', 'isCorrect': True, 'feedback': ''}
        # The generation ran against the submitter's real submission, unsaved section.
        section, submission, demo = calls[0]
        assert section.pk is None
        assert submission == gen_setup['submission']
        assert demo is None

        # Students never see preview runs.
        api_client.force_authenticate(user=gen_setup['students'][0])
        assert api_client.get(
            f"/quizSuggestionJobs/{resp.data['id']}/").status_code == status.HTTP_403_FORBIDDEN

    def test_demo_seed_works_without_any_submission(self, api_client, gen_setup, monkeypatch):
        gen_setup['submission'].delete()
        calls = []
        self._mock_ai_capture(monkeypatch, calls=calls)
        self._run_task_inline(monkeypatch)

        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup['quiz'], seed='demo',
                          demoFiles=[{'name': 'main.py', 'content': 'print(1)'}])
        assert resp.status_code == status.HTTP_202_ACCEPTED

        poll = api_client.get(f"/quizSuggestionJobs/{resp.data['id']}/")
        assert poll.data['status'] == 'completed'
        assert poll.data['resultData']['seed'] == 'demo'
        assert poll.data['resultData']['sampleStudent'] is None
        _, submission, demo = calls[0]
        assert submission is None
        assert demo == ({'name': 'main.py', 'content': 'print(1)'},)

    def test_nothing_is_persisted(self, api_client, gen_setup, monkeypatch):
        from core.models import (
            GeneratedQuestionSet, GeneratedQuizQuestion, QuizGeneratedSection,
            SuggestedQuizQuestion,
        )
        self._mock_ai_capture(monkeypatch)
        self._run_task_inline(monkeypatch)

        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup['quiz'])
        assert resp.status_code == status.HTTP_202_ACCEPTED
        assert QuizGeneratedSection.objects.count() == 1  # the fixture's saved section only
        assert GeneratedQuestionSet.objects.count() == 0
        assert GeneratedQuizQuestion.objects.count() == 0
        assert SuggestedQuizQuestion.objects.count() == 0

    def test_grader_and_student_cannot_preview(self, api_client, gen_setup, monkeypatch):
        self._mock_ai_capture(monkeypatch)
        for user in (gen_setup['grader'], gen_setup['students'][0]):
            api_client.force_authenticate(user=user)
            assert self._post(api_client, gen_setup['quiz']).status_code == status.HTTP_403_FORBIDDEN

    def test_unknown_variable_maps_to_prompt_field_error(self, api_client, gen_setup, monkeypatch):
        self._mock_ai_capture(monkeypatch)
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup['quiz'],
                          systemPrompt='Use {submission_files:bad_arg}.')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'systemPrompt' in resp.data

    def test_empty_prompt_maps_to_prompt_field_error(self, api_client, gen_setup, monkeypatch):
        self._mock_ai_capture(monkeypatch)
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup['quiz'], systemPrompt='   ')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'systemPrompt' in resp.data

    def test_random_seed_requires_a_submission(self, api_client, gen_setup, monkeypatch):
        gen_setup['submission'].delete()
        self._mock_ai_capture(monkeypatch)
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup['quiz'])
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'No student has submitted yet' in resp.data['error']

    def test_demo_seed_requires_files(self, api_client, gen_setup, monkeypatch):
        self._mock_ai_capture(monkeypatch)
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup['quiz'], seed='demo', demoFiles=[])
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'demo file' in resp.data['error']

    def test_blocked_when_feature_disabled(self, api_client, gen_setup):
        # No AI provider is configured in tests, so the feature resolves to disabled.
        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup['quiz'])
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'not enabled' in resp.data['error']

    def test_parse_failure_fails_job_but_keeps_resolved_prompt(self, api_client, gen_setup,
                                                               monkeypatch):
        self._mock_ai_capture(monkeypatch, json_text='this is not json')
        self._run_task_inline(monkeypatch)

        api_client.force_authenticate(user=gen_setup['admin'])
        resp = self._post(api_client, gen_setup['quiz'])
        poll = api_client.get(f"/quizSuggestionJobs/{resp.data['id']}/")
        assert poll.data['status'] == 'failed'
        assert 'parsed' in poll.data['errorMessage']
        # The instructor can still inspect what the model was given.
        assert poll.data['resultData']['resolvedPrompt'] == 'the resolved prompt'
        assert poll.data['resultData']['questions'] == []
