# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""The diagnostic management commands (ai_failures, diagnose, doctor) and a smoke test
that every command still imports and builds its parser."""
import json
from io import StringIO

import factory
import pytest
from django.core.management import call_command, get_commands, load_command_class
from django.db.models.signals import post_save

from core.tests.views.quiz_helpers import _quiz


def _run(name, *args):
    out = StringIO()
    call_command(name, *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def world(db):
    """A course with one assignment+submission, one quiz, a failed suggestion job, a failed
    generated set and an errored provider call."""
    from core.models import AIUsageRecord, GeneratedQuestionSet, QuizSuggestionJob
    from core.tests.factories import AssignmentFactory, CourseFactory
    with factory.django.mute_signals(post_save):
        course = CourseFactory(name='cs111', period='F2026', organization__name='Rutgers')
        assignment = AssignmentFactory(course=course, name='HW1')
        submission = assignment.submissions.first()
        submission.students.add(course.students.first())
        quiz = _quiz(course, title='Lab quiz', assignment=assignment)
        student = course.students.first()
        job = QuizSuggestionJob.objects.create(
            course=course, assignment=assignment, requestedBy=course.courseAdmins.first(),
            status='failed', errorMessage='The model returned output that could not be parsed',
            resultData={'request': {'num_questions': 3, 'question_types': ['code'], 'instructions': ''},
                        'raw_output': '[{"type": "code"}] [{"type": "code"}]'})
        gen_set = GeneratedQuestionSet.objects.create(
            quiz=quiz, student=student, submission=submission, status='failed',
            errorMessage='Could not parse the model output as questions (after 2 attempts).',
            generationMetadata={'raw_output': 'not json', 'provider': 'gemini'})
        usage = AIUsageRecord.objects.create(
            course=course, assignment=assignment, provider='gemini', model='gemini-x',
            request_type='suggested_comments', status='error',
            error_message='AI generation failed: Extra data: line 43 column 2')
    return dict(course=course, assignment=assignment, submission=submission, quiz=quiz,
                student=student, admin=course.courseAdmins.first(), job=job, gen_set=gen_set, usage=usage)


class TestAIFailures:
    def test_text_report_lists_every_failure_kind(self, world):
        out = _run('ai_failures')
        assert 'Failed quiz suggestion jobs: 1' in out
        assert f"#{world['job'].id}" in out and f"--raw {world['job'].id}" in out
        assert 'Personalized sets with errors: 1' in out
        assert f"quiz {world['quiz'].id} 'Lab quiz'" in out
        assert 'Errored provider calls: 1' in out
        assert 'gemini/gemini-x  suggested_comments' in out
        assert 'Extra data' in out

    def test_filters_by_course_and_provider(self, world):
        assert 'No AI failures in this window.' in _run('ai_failures', '--course', str(world['course'].id + 999))
        out = _run('ai_failures', '--provider', 'openai')
        assert 'Errored provider calls: 0' in out
        assert 'Failed quiz suggestion jobs: 1' in out  # provider only filters provider calls

    def test_json_output(self, world):
        data = json.loads(_run('ai_failures', '--json'))
        assert data['suggestion_jobs'][0]['id'] == world['job'].id
        assert data['suggestion_jobs'][0]['has_raw_output'] is True
        assert data['generated_sets'][0]['status'] == 'failed'
        assert data['provider_errors'] == [{'provider': 'gemini', 'model': 'gemini-x',
                                            'request_type': 'suggested_comments', 'count': 1}]

    def test_raw_prints_the_kept_model_output(self, world):
        assert _run('ai_failures', '--raw', str(world['job'].id)).strip() == '[{"type": "code"}] [{"type": "code"}]'

    def test_raw_unknown_job_errors(self, db):
        from django.core.management.base import CommandError
        with pytest.raises(CommandError):
            call_command('ai_failures', '--raw', '999999')


class TestDiagnose:
    def test_submission(self, world):
        out = _run('diagnose', 'submission', str(world['submission'].id))
        assert f"assignment           {world['assignment'].id} 'HW1'" in out
        assert world['student'].email in out
        assert 'Personalized quiz sets' in out and f"set {world['gen_set'].id}" in out

    def test_assignment(self, world):
        out = _run('diagnose', 'assignment', str(world['assignment'].id))
        assert "state           preview" in out
        assert 'submissions     1 total' in out
        assert f"#{world['job'].id}" in out and '[failed]' in out
        assert 'AI calls' in out and 'ERROR: AI generation failed: Extra data' in out

    def test_quiz(self, world):
        out = _run('diagnose', 'quiz', str(world['quiz'].id))
        assert "'Lab quiz'" in out
        assert 'Generated sets by status' in out and 'failed  1' in out
        assert 'Could not parse the model output' in out

    def test_course(self, world):
        out = _run('diagnose', 'course', str(world['course'].id))
        assert 'cs111 F2026' in out
        assert "'HW1'" in out and "'Lab quiz'" in out
        assert 'gemini/gemini-x' in out

    def test_user_by_email_and_id(self, world):
        by_email = _run('diagnose', 'user', world['student'].email)
        by_id = _run('diagnose', 'user', str(world['student'].id))
        assert by_email == by_id
        assert f"student          {world['course'].id:5d}  cs111 F2026" in by_email
        assert "'HW1'" in by_email

    def test_json_output(self, world):
        data = json.loads(_run('diagnose', 'quiz', str(world['quiz'].id), '--json'))
        assert data['kind'] == 'quiz' and data['id'] == world['quiz'].id
        assert [s['title'] for s in data['sections']][0] == 'Quiz'

    @pytest.mark.parametrize('kind', ['submission', 'assignment', 'quiz', 'course', 'user'])
    def test_missing_object_errors(self, db, kind):
        from django.core.management.base import CommandError
        with pytest.raises(CommandError, match='does not exist'):
            call_command('diagnose', kind, '999999')

    def test_non_integer_ref_errors(self, db):
        from django.core.management.base import CommandError
        with pytest.raises(CommandError, match='must be an integer'):
            call_command('diagnose', 'quiz', 'abc')


class TestDoctor:
    """Network-facing probes are stubbed; the command's own logic (aggregation, exit
    status, JSON shape, the beat and secrets checks) runs for real."""

    @pytest.fixture
    def quiet_network(self, monkeypatch):
        from core.management.commands import doctor
        ok = {'status': 'ok', 'label': 'stub', 'detail': None}
        for name in ('_database', '_migrations', '_cache', '_workers', '_disk', '_broker',
                     '_channel_layer', '_docker', '_oauth_client'):
            monkeypatch.setattr(doctor.Command, name, staticmethod(lambda ok=ok: dict(ok)))

    def test_passes_when_every_check_is_ok(self, db, quiet_network, monkeypatch, settings):
        from core.management.commands import doctor
        monkeypatch.setattr(doctor.Command, '_beat', staticmethod(lambda: {'status': 'ok', 'label': 'b', 'detail': None}))
        settings.SECRET_KEY = 'x' * 50
        settings.FIELD_ENCRYPTION_KEY = 'y' * 44
        settings.DEBUG = False
        out = _run('doctor')
        assert 'OK       secrets' in out
        assert 'ERROR' not in out

    def test_exit_status_1_on_failure_and_json_shape(self, db, quiet_network, monkeypatch, settings):
        """Beat is checked for real: an empty TaskResult table means beat never ran."""
        settings.CELERY_TASK_ALWAYS_EAGER = False
        with pytest.raises(SystemExit) as exc:
            call_command('doctor', '--json', stdout=(out := StringIO()))
        assert exc.value.code == 1
        data = json.loads(out.getvalue())
        assert data['overall'] == 'error'
        beat = next(c for c in data['checks'] if c['name'] == 'celery beat')
        assert beat['status'] == 'error'
        assert 'No periodic task has ever recorded a result' in beat['detail']

    def test_beat_ok_when_a_periodic_task_ran_recently(self, db, quiet_network, settings):
        from django_celery_results.models import TaskResult
        settings.CELERY_TASK_ALWAYS_EAGER = False
        TaskResult.objects.create(task_id='t1', task_name='core.tasks.finalize_expired_quiz_attempts',
                                  status='SUCCESS')
        from core.management.commands.doctor import Command
        result = Command._beat()
        assert result['status'] == 'ok'
        assert 'finalize_expired_quiz_attempts' in result['label']

    def test_secrets_dev_defaults_are_a_warning_locally(self, settings):
        from core.management.commands.doctor import Command
        settings.SECRET_KEY = 'your-secret-key-for-dev-only-must-be-at-least-32-bytes'
        settings.DEBUG = True
        settings.DOCKER = False
        result = Command._secrets()
        assert result['status'] == 'warning'
        assert 'SECRET_KEY is the dev default' in result['label']

    def test_redact_hides_broker_password(self):
        from core.management.commands.doctor import _redact
        assert _redact('redis://:hunter2@codepost-redis:6379/0') == 'redis://***@codepost-redis:6379/0'
        assert _redact('redis://codepost-redis:6379') == 'redis://codepost-redis:6379'


def test_every_command_imports_and_builds_its_parser():
    """Catches a command broken by an import or argparse error before anyone runs it."""
    ours = {name: app for name, app in get_commands().items() if app in ('core', 'autograder')}
    assert ours, 'no project commands found'
    for name, app in ours.items():
        command = load_command_class(app, name)
        command.create_parser('manage.py', name)
