# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""One-screen diagnosis of a submission, assignment, quiz, course or user: the object's
state plus everything that happened to it (autograder runs, AI calls, audit events,
quiz attempts, Celery task results). Read-only — never mutates anything.

    manage.py diagnose submission 1234
    manage.py diagnose assignment 42
    manage.py diagnose quiz 7
    manage.py diagnose course 3
    manage.py diagnose user student@rutgers.edu     # or a user id
    manage.py diagnose ... --limit 50 --json
"""
import json

from django.core.management.base import BaseCommand, CommandError

KINDS = ('submission', 'assignment', 'quiz', 'course', 'user')


def _day(dt):
    return f"{dt:%Y-%m-%d}" if dt else '-'


class Command(BaseCommand):
    help = (
        "STRICTLY READ-ONLY one-screen diagnosis of a submission, assignment, quiz, course or "
        "user: state plus related autograder runs, AI calls, audit events, quiz attempts and "
        "Celery task results. Example: diagnose submission 1234"
    )

    def add_arguments(self, parser):
        parser.add_argument('kind', choices=KINDS)
        parser.add_argument('ref', help='Object id (or, for user, an email).')
        parser.add_argument('--limit', type=int, default=20,
                            help='Rows per related-object section (default 20).')
        parser.add_argument('--json', action='store_true', help='Emit JSON instead of text.')

    def handle(self, *args, **options):
        self.limit = options['limit']
        report = getattr(self, f"_{options['kind']}")(options['ref'])
        if options['json']:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        for section in report['sections']:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{section['title']}"))
            rows = section['rows']
            if not rows:
                self.stdout.write("  (none)")
                continue
            if isinstance(rows, dict):
                width = max(len(k) for k in rows)
                for key, value in rows.items():
                    self.stdout.write(f"  {key:<{width}}  {value}")
            else:
                for row in rows:
                    self.stdout.write(f"  {row}")

    # ------------------------------------------------------------------ helpers

    def _section(self, title, rows):
        return {'title': title, 'rows': rows}

    def _audit(self, qs):
        rows = []
        for e in qs.select_related('user').order_by('-created')[:self.limit]:
            who = getattr(e.user, 'email', '-')
            meta = json.dumps(e.meta, default=str)[:160] if e.meta else ''
            rows.append(f"{e.created:%Y-%m-%d %H:%M}  {e.event_type}  by {who}  {meta}")
        return self._section(f"Audit events (latest {self.limit})", rows)

    def _ai_usage(self, qs):
        rows = []
        for r in qs.select_related('user').order_by('-created')[:self.limit]:
            err = f"  ERROR: {(r.error_message or '')[:120]}" if r.status != 'success' else ''
            rows.append(f"{r.created:%Y-%m-%d %H:%M}  {r.request_type:<28} {r.provider}/{r.model}  "
                        f"{r.total_tokens} tok  ${r.estimated_cost}  by {getattr(r.user, 'email', '-')}{err}")
        return self._section(f"AI calls (latest {self.limit})", rows)

    def _task_results(self, task_ids, title):
        from django_celery_results.models import TaskResult
        rows = []
        task_ids = [t for t in task_ids if t]
        if task_ids:
            for t in TaskResult.objects.filter(task_id__in=task_ids).order_by('-date_done'):
                tb = f"\n      {t.traceback.strip().splitlines()[-1]}" if t.traceback else ''
                rows.append(f"{t.task_id}  {t.status}  {t.task_name}  done {t.date_done:%Y-%m-%d %H:%M}"
                            f"{tb}" if t.date_done else f"{t.task_id}  {t.status}  {t.task_name}{tb}")
        return self._section(title, rows)

    @staticmethod
    def _get(queryset, ref, label):
        try:
            obj = queryset.filter(pk=int(ref)).first()
        except (TypeError, ValueError):
            raise CommandError(f"{label} id must be an integer, got {ref!r}.")
        if obj is None:
            raise CommandError(f"{label} {ref} does not exist.")
        return obj

    # ------------------------------------------------------------------ kinds

    def _submission(self, ref):
        from core.models import Submission
        from log.models import TrackedAutograderRun
        sub = self._get(Submission.objects.select_related('assignment', 'assignment__course', 'grader'),
                        ref, 'Submission')
        a = sub.assignment
        state = {
            'submission': sub.id,
            'assignment': f"{a.id} '{a.name}'  state={a.state}  feedback={a.feedbackStatus}",
            'course': f"{a.course_id} {a.course.name} {a.course.period}"
                      + ("  [ARCHIVED — edits are refused]" if a.course.archived else ''),
            'students': ', '.join(sub.students.values_list('email', flat=True)) or '-',
            'grader': getattr(sub.grader, 'email', '-'),
            'uploaded': sub.dateUploaded,
            'finalized': f"{sub.isFinalized}  grade={sub.grade}  gradeFrozen={sub.gradeFrozen}",
            'late day credits': sub.lateDayCreditsUsed,
            'open question': f"{sub.questionIsOpen}  regrade={sub.questionIsRegrade}",
            'test runs completed': sub.testRunsCompleted,
            'files': ', '.join(sub.files.values_list('name', flat=True)[:30]) or '-',
        }
        tests = [f"{t.testCase.id} {t.testCase.description[:50]!r}  passed={t.passed}  error={t.isError}  "
                 f"{t.score}/{t.maxScore}"
                 for t in sub.tests.select_related('testCase').order_by('testCase_id')[:self.limit]]
        runs = [f"{r.started:%Y-%m-%d %H:%M}  {r.test_case_set}  by {getattr(r.run_by, 'email', '-')} "
                f"({r.run_by_role})  {r.duration}s"
                + (f"  ERRORS: {r.errors[:160]}" if r.errors else '')
                for r in TrackedAutograderRun.objects.filter(submission=sub).select_related('run_by')
                .order_by('-started')[:self.limit] if r.started]
        suggested = sub.suggested_comments.values('status').order_by('status')
        ai_rows = {f"suggested comments ({s['status']})": sub.suggested_comments.filter(status=s['status']).count()
                   for s in suggested.distinct()}
        ai_rows['summary'] = 'yes' if hasattr(sub, 'summary') else 'no'
        gen_sets = [f"set {g.id}  quiz {g.quiz_id}  student {g.student_id}  [{g.status}]  {g.errorMessage[:120]}"
                    for g in sub.generated_question_sets.all()]
        return {'kind': 'submission', 'id': sub.id, 'sections': [
            self._section('Submission', state),
            self._section(f'Test results (first {self.limit})', tests),
            self._section(f'Autograder runs (latest {self.limit})', runs),
            self._section('AI grading assistance', ai_rows),
            self._section('Personalized quiz sets', gen_sets),
            self._audit(sub.audit_events),
        ]}

    def _assignment(self, ref):
        from django.db.models import Count, Q
        from core.models import Assignment
        a = self._get(Assignment.objects.select_related('course'), ref, 'Assignment')
        env = getattr(a, 'environment', None)
        subs = a.submissions.aggregate(
            total=Count('id'), finalized=Count('id', filter=Q(isFinalized=True)),
            graded=Count('id', filter=Q(grader__isnull=False)),
            open_questions=Count('id', filter=Q(questionIsOpen=True)))
        state = {
            'assignment': f"{a.id} '{a.name}'  points={a.points}",
            'course': f"{a.course_id} {a.course.name} {a.course.period}"
                      + ("  [ARCHIVED — edits are refused]" if a.course.archived else ''),
            'state': f"{a.state}  publishAt={a.publishAt}  publishedAt={a.publishedAt}",
            'feedback': f"{a.feedbackStatus}  releaseAt={a.releaseFeedbackAt}  releasedAt={a.feedbackReleasedAt}",
            'due': f"{a.uploadDueDate}  maxLateDays={a.maxLateDays}  allowLate={a.allowLateUploads}",
            'student upload': f"{a.allowStudentUpload}  partners={a.allowStudentUploadWithPartners}",
            'autograder': f"runFilesOnSubmit={a.runFilesOnSubmit}  runTestsOnSubmit={a.runTestsOnSubmit}  "
                          f"testsAffectGrade={a.testsAffectGrade}",
            'submissions': f"{subs['total']} total, {subs['graded']} with grader, {subs['finalized']} finalized, "
                           f"{subs['open_questions']} open questions",
            'files': f"{a.files.count()} ({a.files.filter(is_test_resource=True).count()} test resources, "
                     f"{a.files.filter(hidden=True).count()} hidden)",
            'test cases': sum(c.testCases.count() for c in a.testCategories.all()),
            'quizzes': ', '.join(f"{q.id} '{q.title}'" for q in a.quizzes.all()) or '-',
        }
        env_rows = {}
        if env is not None:
            env_rows = {
                'language': f"{env.language}  buildType={env.buildType}  autoDetect={env.auto_detect}",
                'image': f"{env.image_name or '-'}  build={env.build_status}  version={env.current_build_version}  "
                         f"lastBuilt={env.last_built}",
                'runs': f"{env.successful_runs}/{env.total_runs} successful  convergencePending={env.convergence_pending}",
                'network': env.allowNetworkAccess,
                'build log tail': ' '.join((env.build_logs or '')[-300:].split()) or '-',
            }
        events = a.autograder_execution_events.values('trigger', 'success', 'cached', 'error_category').annotate(
            n=Count('id')).order_by('-n')[:self.limit]
        exec_rows = [f"{e['n']:5d}  {e['trigger']:<16} success={e['success']!s:<5} cached={e['cached']!s:<5} "
                     f"{e['error_category'] or ''}" for e in events]
        jobs = [f"#{j.id}  {j.created:%Y-%m-%d %H:%M}  [{j.status}]  created={j.createdCount}  "
                f"{j.errorMessage[:120]}  task={j.taskId or '-'}"
                for j in a.suggestion_jobs.order_by('-created')[:self.limit]]
        return {'kind': 'assignment', 'id': a.id, 'sections': [
            self._section('Assignment', state),
            self._section('Environment', env_rows),
            self._section('Autograder execution events (grouped)', exec_rows),
            self._section(f'Quiz suggestion jobs (latest {self.limit})', jobs),
            self._task_results(a.suggestion_jobs.values_list('taskId', flat=True)[:self.limit],
                               'Celery task results for those jobs'),
            self._ai_usage(a.ai_usage_records),
            self._audit(a.audit_events),
        ]}

    def _quiz(self, ref):
        from django.db.models import Count
        from core.models import Quiz
        q = self._get(Quiz.objects.select_related('course', 'assignment'), ref, 'Quiz')
        state = {
            'quiz': f"{q.id} '{q.title}'  source={q.source}  published={q.isPublished}",
            'course': f"{q.course_id} {q.course.name} {q.course.period}",
            'assignment': f"{q.assignment_id} '{q.assignment.name}'  state={q.assignment.state}"
                          if q.assignment_id else '-',
            'window': f"from {q.availableFrom}  until {q.availableUntil}  close={q.closeEvent}+{q.closeOffsetMinutes}m",
            'trigger': q.assignmentTrigger,
            'questions': f"{q.quizQuestions.count()} fixed, {q.questionGroups.count()} groups, "
                         f"{q.generatedSections.count()} generated sections",
            'generation': f"manual={q.manualGeneration}  autoPublish={q.autoPublishGenerated}",
        }
        sets = {s['status']: s['n'] for s in q.generatedSets.values('status').annotate(n=Count('id'))}
        attempts = {s['status']: s['n'] for s in q.attempts.values('status').annotate(n=Count('id'))}
        failed_sets = [f"set {g.id}  student {g.student_id}  [{g.status}]  {g.errorMessage[:140]}"
                       for g in q.generatedSets.exclude(errorMessage='').order_by('-modified')[:self.limit]]
        sections = [f"{s.id} '{s.name}'  {s.numQuestions} q × {s.pointsPerQuestion} pts  types={s.questionTypes}"
                    for s in q.generatedSections.order_by('sortKey')]
        jobs = [f"#{j.id}  {j.created:%Y-%m-%d %H:%M}  [{j.status}]  {j.errorMessage[:120]}  task={j.taskId or '-'}"
                for j in q.suggestion_jobs.order_by('-created')[:self.limit]]
        return {'kind': 'quiz', 'id': q.id, 'sections': [
            self._section('Quiz', state),
            self._section('Generated sections', sections),
            self._section('Generated sets by status', sets),
            self._section(f'Sets with errors (latest {self.limit})', failed_sets),
            self._section('Attempts by status', attempts),
            self._section(f'Section preview jobs (latest {self.limit})', jobs),
            self._audit(q.audit_events),
        ]}

    def _course(self, ref):
        from django.db.models import Count
        from core.models import Course
        from core.services.ai_service import AIService
        c = self._get(Course.objects.select_related('organization'), ref, 'Course')
        svc = AIService(c)
        state = {
            'course': f"{c.id} {c.name} {c.period}" + ("  [ARCHIVED]" if c.archived else ''),
            'organization': f"{c.organization_id} {getattr(c.organization, 'name', '-')}",
            'roster': f"{c.students.count()} students, {c.graders.count()} graders, "
                      f"{c.courseAdmins.count()} admins, {c.inactive_students.count()} inactive students",
            'sections': c.sections.count(),
            'AI': f"provider={svc.provider or '-'}  model={svc.model or '-'}  configured={svc.is_configured}  "
                  f"disabled={svc.is_globally_disabled}  ownSettings={c.ai_use_own_settings}",
            'api keys': c.api_keys.count(),
            'pending agent actions': c.pending_agent_actions.filter(status='pending').count()
            if hasattr(c.pending_agent_actions.model, 'status') else c.pending_agent_actions.count(),
        }
        assignments = [f"{a.id:5d}  {a.state:<10} {a.feedbackStatus:<10} subs={a.n_subs:<4} '{a.name}'"
                       for a in c.assignments.annotate(n_subs=Count('submissions')).order_by('sortKey', 'id')]
        quizzes = [f"{q.id:5d}  published={q.isPublished!s:<5} attempts={q.n_att:<4} '{q.title}'"
                   for q in c.quizzes.annotate(n_att=Count('attempts')).order_by('id')]
        return {'kind': 'course', 'id': c.id, 'sections': [
            self._section('Course', state),
            self._section('Assignments', assignments),
            self._section('Quizzes', quizzes),
            self._ai_usage(c.ai_usage_records),
            self._audit(c.audit_events),
        ]}

    def _user(self, ref):
        from core.models import User
        user = User.objects.filter(email__iexact=ref).first() if '@' in ref else None
        if user is None:
            user = self._get(User.objects.all(), ref, 'User')
        profile = getattr(user, 'profile', None)
        state = {
            'user': f"{user.id} {user.email}  username={user.username}  active={user.is_active}  "
                    f"staff={user.is_staff}  superuser={user.is_superuser}",
            'last login': user.last_login,
            'profile': (f"org={getattr(profile.organization, 'name', '-')}  orgStaff={profile.isOrgStaff}  "
                        f"canCreateCourses={profile.canCreateCourses}  passwordSet={profile.isPasswordSet}  "
                        f"serviceAccount={profile.isServiceAccount}") if profile else '(no profile)',
            'API token': 'yes' if hasattr(user, 'auth_token') else 'no',
        }
        roles = []
        for label, rel in (('student', 'student_courses'), ('grader', 'grader_courses'),
                           ('admin', 'courseAdmin_courses'), ('inactive student', 'student_inactive_courses')):
            for c in getattr(user, rel).all():
                roles.append(f"{label:<16} {c.id:5d}  {c.name} {c.period}" + ("  [ARCHIVED]" if c.archived else ''))
        subs = [f"{s.id:6d}  assignment {s.assignment_id} '{s.assignment.name}'  uploaded {_day(s.dateUploaded)}  "
                f"finalized={s.isFinalized}  grade={s.grade}"
                for s in user.student_submissions.select_related('assignment').order_by('-dateUploaded')[:self.limit]]
        attempts = [f"{t.id:6d}  quiz {t.quiz_id} '{t.quiz.title}'  #{t.attemptNumber}  [{t.status}]  "
                    f"started {t.startedAt:%Y-%m-%d %H:%M}  score={t.score}/{t.maxScore}"
                    for t in user.quiz_attempts.select_related('quiz').order_by('-startedAt')[:self.limit]]
        sets = [f"set {g.id}  quiz {g.quiz_id}  [{g.status}]  {g.errorMessage[:100]}"
                for g in user.generated_question_sets.order_by('-modified')[:self.limit]]
        return {'kind': 'user', 'id': user.id, 'sections': [
            self._section('User', state),
            self._section('Course roles', roles),
            self._section(f'Submissions (latest {self.limit})', subs),
            self._section(f'Quiz attempts (latest {self.limit})', attempts),
            self._section('Personalized quiz sets', sets),
            self._audit(user.audit_events),
        ]}
