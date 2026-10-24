# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Course gradebook: one grid of every active student × every assignment and quiz.

This is an admin-only view (both callers 403 non-admins), so it shows grades
regardless of finalization — unlike the student-facing grading surfaces.

Semantics:
- Rows: the active roster (course.students) ordered by email; students with nothing graded
  still get a row. Inactive students are excluded.
- Assignment columns: every assignment (model ordering). A cell shows the student's best
  submission — finalized preferred, then newest upload, then highest id (mirrors
  quiz_grading.LATEST_SUBMISSION_ORDERING) — and its grade whenever a grade exists; a
  submission still awaiting a grade (or no submission) renders as an em-dash.
- Quiz columns: quizzes that are published or already have submitted attempts. A cell is
  the official score per the quiz's scoringPolicy (quiz_grading.official_score), null until
  a fully graded attempt exists. maxScore is the student's own (random-draw/generated
  sections vary per student).
- Totals count graded cells only: earned/possible accumulate any non-null assignment grade
  and official quiz scores — missing or not-yet-graded work never counts against the student.
  percent is null until something is graded.

Both the gradebook endpoint (JSON grid) and the CSV export are built from this one
function so they can never drift apart.
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from core.models import QuizAttempt, Submission, SubmissionGradeSnapshot
from core.services import quiz_grading

# Sorts below datetimes of real uploads so a submission without dateUploaded loses ties.
_EPOCH = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)


def build_gradebook(course, assignment_ids=None, quiz_ids=None, section=None):
  """The gradebook for ``course``: column metadata plus one row per active student.

  ``assignment_ids`` / ``quiz_ids`` restrict the columns (None means all; an empty set
  means none) and ``section`` restricts the rows to students in that section — totals
  are computed over the included columns only, so a filtered export stays self-consistent.

  Runs a constant number of queries regardless of course size (students, assignments,
  one course-wide submissions query, one course-wide attempts query, quizzes).
  """
  students = list(course.students.all().order_by('email'))
  assignments = list(course.assignments.all())
  if assignment_ids is not None:
    assignments = [a for a in assignments if a.id in assignment_ids]

  # Section name(s) per student email (a student can belong to several sections).
  sections_by_email = {}
  for name, email in course.sections.values_list('name', 'students__email'):
    if email is not None:
      sections_by_email.setdefault(email, []).append(name)
  if section:
    students = [s for s in students if section in sections_by_email.get(s.email, [])]

  # Best submission per (student email, assignment): finalized first, then newest.
  # The values_list joins the students M2M, so partner submissions yield one row each.
  # best[key] = (pref_tuple, grade, is_finalized, sub_id)
  best = {}
  sub_rows = Submission.objects.filter(assignment__course=course).values_list(
      'students__email', 'assignment_id', 'grade', 'isFinalized', 'dateUploaded', 'id')
  for email, assignment_id, grade, is_finalized, date_uploaded, sub_id in sub_rows:
    if email is None:
      continue
    key = (email, assignment_id)
    pref = (bool(is_finalized), date_uploaded or _EPOCH, sub_id)
    if key not in best or pref > best[key][0]:
      best[key] = (pref, grade, bool(is_finalized), sub_id)

  # Grade history: one query over all snapshots in the course, grouped by submission.
  # Yields (sub_id, personalBest, firstMaxAt) for every submission that has snapshots.
  best_sub_ids = {v[3] for v in best.values()}
  snapshot_info = {}  # sub_id -> (personalBest, firstMaxAt)
  if best_sub_ids:
    from django.db.models import Max, Min
    from django.db.models.functions import Coalesce
    # Step 1: personal best grade per submission.
    pb_rows = (SubmissionGradeSnapshot.objects
               .filter(submission_id__in=best_sub_ids)
               .values('submission_id')
               .annotate(personalBest=Max('grade')))
    pb_map = {r['submission_id']: r['personalBest'] for r in pb_rows}
    # Step 2: earliest snapshot created when grade == personalBest.
    for sub_id, personal_best in pb_map.items():
      first_max = (SubmissionGradeSnapshot.objects
                   .filter(submission_id=sub_id, grade=personal_best)
                   .order_by('created')
                   .values_list('created', flat=True)
                   .first())
      snapshot_info[sub_id] = (personal_best, first_max)

  # Every submitted attempt in the course, grouped per (quiz, student) — the whole-course
  # generalization of the batching in QuizViewSet.results (no per-student queries).
  attempts_by = {}
  for attempt in QuizAttempt.objects.filter(quiz__course=course, status='submitted') \
      .select_related('student').order_by('attemptNumber'):
    attempts_by.setdefault((attempt.quiz_id, attempt.student_id), []).append(attempt)
  quiz_ids_with_attempts = {quiz_id for quiz_id, _ in attempts_by}
  quizzes = [q for q in course.quizzes.order_by('title', 'id')
             if q.isPublished or q.id in quiz_ids_with_attempts]
  if quiz_ids is not None:
    quizzes = [q for q in quizzes if q.id in quiz_ids]

  rows = []
  for student in students:
    earned = Decimal('0')
    possible = Decimal('0')
    assignment_cells = []
    first_max_ats = []
    for assignment in assignments:
      found = best.get((student.email, assignment.id))
      is_finalized = found is not None and bool(found[2])
      sub_id = found[3] if found is not None else None
      # Admin-only view: show the best submission's grade whenever one exists,
      # regardless of finalization. A submission still awaiting a grade, or no
      # submission at all, renders as an em-dash.
      grade = found[1] if found is not None else None
      if grade is not None:
        earned += grade
        possible += assignment.points or Decimal('0')
      snap = snapshot_info.get(sub_id) if sub_id is not None else None
      personal_best = snap[0] if snap else None
      first_max_at = snap[1] if snap else None
      if first_max_at is not None:
        first_max_ats.append(first_max_at)
      assignment_cells.append({
          'assignment': assignment.id,
          'grade': grade,
          'hasSubmission': found is not None,
          'isFinalized': is_finalized,
          'submissionId': sub_id,
          'personalBest': personal_best,
          'firstMaxAt': first_max_at,
      })
    quiz_cells = []
    for quiz in quizzes:
      attempts = attempts_by.get((quiz.id, student.id), [])
      official = quiz_grading.official_score(quiz, student, attempts=attempts) if attempts else None
      if official is not None:
        earned += official[0]
        possible += official[1] or Decimal('0')
      quiz_cells.append({
          'quiz': quiz.id,
          'score': official[0] if official else None,
          'maxScore': official[1] if official else None,
          'needsGrading': any(a.needsManualGrading for a in attempts),
          'hasAttempts': bool(attempts),
      })
    percent = (earned / possible * Decimal('100')).quantize(Decimal('0.01')) if possible > 0 else None
    completed_all_max_at = max(first_max_ats) if first_max_ats else None
    rows.append({
        'student': student.email,
        'section': ', '.join(sorted(sections_by_email.get(student.email, []))) or None,
        'assignmentCells': assignment_cells,
        'quizCells': quiz_cells,
        'totalEarned': earned,
        'totalPossible': possible,
        'percent': percent,
        'completedAllMaxAt': completed_all_max_at,
    })

  return {
      'assignments': [{'id': a.id, 'name': a.name, 'points': a.points} for a in assignments],
      'quizzes': [{'id': q.id, 'title': q.title} for q in quizzes],
      'rows': rows,
  }
