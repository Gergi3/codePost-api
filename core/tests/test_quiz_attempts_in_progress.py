# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from datetime import timedelta
from io import StringIO

import factory
import pytest
from django.core.management import call_command
from django.db.models.signals import post_save
from django.utils import timezone

from core.models import QuizAttempt, QuizResponse
from core.tests.views.quiz_helpers import _quiz


@pytest.fixture
def course(db):
    from core.tests.factories import CourseFactory
    with factory.django.mute_signals(post_save):
        return CourseFactory(name='cs439', period='F2026', organization__name='Rutgers')


def _run(*args):
    out = StringIO()
    call_command('quiz_attempts_in_progress', *args, stdout=out)
    return out.getvalue()


def test_reports_nothing_when_no_open_attempts(course):
    quiz = _quiz(course, title='Lab 1')
    QuizAttempt.objects.create(quiz=quiz, student=course.students.first(), status='submitted')
    assert _run() == 'No in-progress attempts.\n'


def test_groups_open_attempts_per_quiz_and_flags_running_clocks(course):
    s1, s2 = list(course.students.all())[:2]
    now = timezone.now()
    timed = _quiz(course, title='Timed')
    QuizAttempt.objects.create(quiz=timed, student=s1, deadline=now + timedelta(minutes=30))
    QuizAttempt.objects.create(quiz=timed, student=s2, deadline=now - timedelta(minutes=5))
    untimed = _quiz(course, title='Untimed')
    QuizAttempt.objects.create(quiz=untimed, student=s1)
    expired = _quiz(course, title='Expired')
    QuizAttempt.objects.create(quiz=expired, student=s1, deadline=now - timedelta(hours=1))

    out = _run()

    assert f'cs439 F2026 | Timed (quiz {timed.id}): 2 in progress, 2 timed — TIMED, ends by' in out
    assert f'Untimed (quiz {untimed.id}): 1 in progress, 0 timed — untimed' in out
    assert f'Expired (quiz {expired.id}): 1 in progress, 1 timed — expired' in out


def test_per_attempt_shows_last_save(course):
    s1, s2 = list(course.students.all())[:2]
    quiz = _quiz(course, title='Lab 1')
    active = QuizAttempt.objects.create(quiz=quiz, student=s1)
    QuizResponse.objects.create(attempt=active, questionSnapshot={'type': 'essay'})
    idle = QuizAttempt.objects.create(quiz=quiz, student=s2)

    out = _run('--quiz', str(quiz.id))

    assert f'attempt {active.id}: started' in out and 'deadline untimed, last save 0 min ago' in out
    assert f'attempt {idle.id}:' in out and 'last save never' in out
    assert _run('--quiz', '999999') == 'No in-progress attempts for quiz 999999.\n'


def test_json_output_for_deploy_scripts(course):
    import json
    quiz = _quiz(course, title='Timed')
    attempt = QuizAttempt.objects.create(quiz=quiz, student=course.students.first(),
                                         deadline=timezone.now() + timedelta(minutes=30))
    [row] = json.loads(_run('--json'))
    assert row['quiz_id'] == quiz.id and row['in_progress'] == 1 and row['running_clock'] is True
    [a] = json.loads(_run('--quiz', str(quiz.id), '--json'))
    assert a['attempt_id'] == attempt.id and a['last_save'] is None
    assert json.loads(_run('--quiz', str(quiz.id + 1), '--json')) == []
