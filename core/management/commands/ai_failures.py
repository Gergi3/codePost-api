# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""List recent AI generation failures: failed quiz-suggestion jobs, failed personalized
question sets, and errored provider calls (AIUsageRecord). Read-only.

    manage.py ai_failures                    # last 7 days
    manage.py ai_failures --days 1 --course 42
    manage.py ai_failures --raw 1234         # print a failed suggestion job's raw model output
    manage.py ai_failures --json
"""
import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone


class Command(BaseCommand):
    help = (
        "List recent AI generation failures (quiz suggestion jobs, personalized question sets, "
        "errored provider calls). Read-only. --raw <job-id> prints a failed suggestion job's "
        "raw model output for inspection."
    )

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7, help='Look back this many days (default 7).')
        parser.add_argument('--course', type=int, default=None, help='Limit to one course id.')
        parser.add_argument('--provider', default=None,
                            help='Limit errored provider calls to one provider (gemini, openai, ...).')
        parser.add_argument('--raw', type=int, default=None, metavar='JOB_ID',
                            help='Print the raw model output kept on a failed QuizSuggestionJob, then exit.')
        parser.add_argument('--json', action='store_true', help='Emit JSON instead of text.')

    def handle(self, *args, **options):
        from core.models import AIUsageRecord, GeneratedQuestionSet, QuizSuggestionJob

        if options['raw'] is not None:
            job = QuizSuggestionJob.objects.filter(pk=options['raw']).first()
            if job is None:
                raise CommandError(f"QuizSuggestionJob {options['raw']} does not exist.")
            raw = (job.resultData or {}).get('raw_output')
            self.stdout.write(raw if raw else '(no raw output recorded on this job)')
            return

        since = timezone.now() - timedelta(days=options['days'])
        course_id = options['course']

        jobs = QuizSuggestionJob.objects.filter(status='failed', created__gte=since).select_related(
            'course', 'assignment', 'requestedBy').order_by('-created')
        sets = GeneratedQuestionSet.objects.filter(created__gte=since).exclude(errorMessage='').select_related(
            'quiz', 'quiz__course', 'student').order_by('-created')
        calls = AIUsageRecord.objects.filter(status='error', created__gte=since)
        if course_id:
            jobs = jobs.filter(course_id=course_id)
            sets = sets.filter(quiz__course_id=course_id)
            calls = calls.filter(course_id=course_id)
        if options['provider']:
            calls = calls.filter(provider=options['provider'])

        call_groups = list(calls.values('provider', 'model', 'request_type').annotate(
            count=Count('id')).order_by('-count'))
        recent_calls = list(calls.select_related('course').order_by('-created')[:10])

        if options['json']:
            self.stdout.write(json.dumps({
                'since': since.isoformat(),
                'suggestion_jobs': [{
                    'id': j.id, 'created': j.created.isoformat(), 'course_id': j.course_id,
                    'assignment_id': j.assignment_id, 'source_question_id': j.sourceQuestion_id,
                    'requested_by': getattr(j.requestedBy, 'email', None), 'error': j.errorMessage,
                    'has_raw_output': bool((j.resultData or {}).get('raw_output')),
                } for j in jobs],
                'generated_sets': [{
                    'id': s.id, 'created': s.created.isoformat(), 'course_id': s.quiz.course_id,
                    'quiz_id': s.quiz_id, 'student_id': s.student_id, 'status': s.status,
                    'error': s.errorMessage,
                    'has_raw_output': bool((s.generationMetadata or {}).get('raw_output')),
                } for s in sets],
                'provider_errors': call_groups,
                'recent_provider_errors': [{
                    'id': c.id, 'created': c.created.isoformat(), 'course_id': c.course_id,
                    'provider': c.provider, 'model': c.model, 'request_type': c.request_type,
                    'error': c.error_message,
                } for c in recent_calls],
            }, indent=2, default=str))
            return

        scope = f"last {options['days']} day(s)" + (f", course {course_id}" if course_id else '')
        self.stdout.write(self.style.MIGRATE_HEADING(f"AI generation failures — {scope}"))

        self.stdout.write(self.style.MIGRATE_LABEL(f"\nFailed quiz suggestion jobs: {jobs.count()}"))
        for j in jobs:
            seed = f"assignment {j.assignment_id}" if j.assignment_id else f"question {j.sourceQuestion_id}"
            raw = ' [raw output kept: --raw %d]' % j.id if (j.resultData or {}).get('raw_output') else ''
            self.stdout.write(f"  #{j.id}  {j.created:%Y-%m-%d %H:%M}  course {j.course_id}  {seed}  "
                              f"by {getattr(j.requestedBy, 'email', '?')}{raw}")
            self.stdout.write(f"      {_one_line(j.errorMessage)}")

        self.stdout.write(self.style.MIGRATE_LABEL(f"\nPersonalized sets with errors: {sets.count()}"))
        for s in sets:
            raw = ' [raw output kept: admin → Generated question sets]' if (
                s.generationMetadata or {}).get('raw_output') else ''
            self.stdout.write(f"  #{s.id}  {s.created:%Y-%m-%d %H:%M}  course {s.quiz.course_id}  "
                              f"quiz {s.quiz_id} '{s.quiz.title}'  student {s.student_id}  [{s.status}]{raw}")
            self.stdout.write(f"      {_one_line(s.errorMessage)}")

        self.stdout.write(self.style.MIGRATE_LABEL(f"\nErrored provider calls: {calls.count()}"))
        for g in call_groups:
            self.stdout.write(f"  {g['count']:5d}  {g['provider']}/{g['model']}  {g['request_type']}")
        if recent_calls:
            self.stdout.write("  Most recent:")
            for c in recent_calls:
                self.stdout.write(f"    #{c.id}  {c.created:%Y-%m-%d %H:%M}  course {c.course_id}  "
                                  f"{c.provider}/{c.model}  {c.request_type}: {_one_line(c.error_message)}")
        if not jobs.exists() and not sets.exists() and not calls.exists():
            self.stdout.write(self.style.SUCCESS("\nNo AI failures in this window."))


def _one_line(text, limit=200):
    text = ' '.join((text or '').split())
    return text[:limit] + ('…' if len(text) > limit else '')
