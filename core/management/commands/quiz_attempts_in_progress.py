# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Show in-progress quiz attempts — run before a deploy to see whether anyone's clock is running.

    python manage.py quiz_attempts_in_progress             # one line per quiz with open attempts
    python manage.py quiz_attempts_in_progress --quiz 20   # one line per attempt, with last save time

A deploy restarts the API for a minute or so. The attempt deadline is server-side and keeps
running through that window, so a TIMED quiz with open attempts means students lose working
time; untimed and already-expired attempts are safe to deploy over.
    python manage.py quiz_attempts_in_progress --json      # machine-readable, for deploy scripts
"""
import json

from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Min
from django.utils import timezone

from core.models import QuizAttempt


class Command(BaseCommand):
    help = 'List in-progress quiz attempts (per quiz, or per attempt with --quiz) to judge deploy safety.'

    def add_arguments(self, parser):
        parser.add_argument('--quiz', type=int, default=None,
                            help='Show each in-progress attempt of this quiz with its last save time.')
        parser.add_argument('--json', action='store_true', help='Emit JSON instead of text.')

    def handle(self, *args, **options):
        now = timezone.now()
        if options['quiz'] is not None:
            self._per_attempt(options['quiz'], now, options['json'])
        else:
            self._per_quiz(now, options['json'])

    def _per_quiz(self, now, as_json):
        rows = (QuizAttempt.objects.filter(status='in_progress')
                .values('quiz_id', 'quiz__title', 'quiz__course__name', 'quiz__course__period')
                .annotate(n=Count('id'), timed=Count('deadline'),
                          first_deadline=Min('deadline'), last_deadline=Max('deadline'))
                .order_by('last_deadline'))
        if as_json:
            self.stdout.write(json.dumps([{
                'quiz_id': r['quiz_id'], 'title': r['quiz__title'],
                'course': f"{r['quiz__course__name']} {r['quiz__course__period']}",
                'in_progress': r['n'], 'timed': r['timed'],
                'running_clock': bool(r['last_deadline'] and r['last_deadline'] > now),
                'last_deadline': r['last_deadline'],
            } for r in rows], indent=2, default=str))
            return
        if not rows:
            self.stdout.write('No in-progress attempts.')
            return
        for r in rows:
            if r['last_deadline'] and r['last_deadline'] > now:
                state = 'TIMED, ends by ' + r['last_deadline'].strftime('%Y-%m-%d %H:%M %Z')
            elif r['timed']:
                state = 'expired'
            else:
                state = 'untimed'
            self.stdout.write(
                f"{r['quiz__course__name']} {r['quiz__course__period']} | {r['quiz__title']} "
                f"(quiz {r['quiz_id']}): {r['n']} in progress, {r['timed']} timed — {state}")

    def _per_attempt(self, quiz_id, now, as_json):
        attempts = (QuizAttempt.objects.filter(status='in_progress', quiz_id=quiz_id)
                    .annotate(last=Max('responses__modified')).order_by('-last')
                    .values('id', 'startedAt', 'deadline', 'last'))
        if as_json:
            self.stdout.write(json.dumps([{
                'attempt_id': a['id'], 'started_at': a['startedAt'], 'deadline': a['deadline'],
                'last_save': a['last'],
            } for a in attempts], indent=2, default=str))
            return
        if not attempts:
            self.stdout.write(f'No in-progress attempts for quiz {quiz_id}.')
            return
        for a in attempts:
            if a['last'] is None:
                last = 'never'
            else:
                last = f"{int((now - a['last']).total_seconds() // 60)} min ago"
            deadline = a['deadline'].strftime('%Y-%m-%d %H:%M %Z') if a['deadline'] else 'untimed'
            self.stdout.write(
                f"attempt {a['id']}: started {a['startedAt']:%Y-%m-%d %H:%M}, deadline {deadline}, last save {last}")
