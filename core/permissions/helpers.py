# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from core.models import Course, Section, STUDENT_DOWNLOAD_STATES, STUDENT_VISIBLE_STATES

from rest_framework.response import Response
from rest_framework import status

NOT_AUTHORIZED = "You are not logged in. Please log in."
FORBIDDEN = "You do not have permission to perform this action."
NOT_FOUND = "The object you requested could not be found."
NOT_ACCEPTABLE = "The request is invalid."

def returnNotAuthorized():
  return Response(NOT_AUTHORIZED, status.HTTP_401_UNAUTHORIZED)

def returnForbidden():
  return Response(FORBIDDEN, status.HTTP_403_FORBIDDEN)

def returnInvalid():
  return Response(NOT_ACCEPTABLE, status.HTTP_406_NOT_ACCEPTABLE);

def returnNotFound(message=None):
  if message is None:
    return Response(NOT_FOUND, status.HTTP_404_NOT_FOUND)
  else:
    return Response(message, status.HTTP_404_NOT_FOUND)

def isAuthenticated(user) -> bool:
  return user.is_authenticated

def isOrganizationMember(user, organization):
  return (user.profile.organization == organization)

def isStudent(user, course):
  return course in user.student_courses.all()

def isGrader(user, course):
  return course in user.grader_courses.all() or isSuperGrader(user, course)

def isSuperGrader(user, course):
  """
  Check if the user is a super grader.
  """
  return course in user.superGrader_courses.all()

def isRubricEditor(user, course):
  """
  Check if the user is allowed to edit rubrics in the course.
  """
  return course in user.rubricEditor_courses.all()

def isQuizGrader(user, course):
  """
  Check if the user has the explicit quiz-grader role in the course — the opt-in list
  used when the course's gradersCanGradeQuizzes flag is off (by default the flag is on
  and every grader can grade quizzes; see canGradeQuiz). (Course admins can always grade
  quizzes; check isCourseAdmin separately.)
  """
  return course in user.quizGrader_courses.all()

def isCourseAdmin(user, course):
  """
  Check if the user is a course admin.
  """
  return user.is_superuser or course in user.courseAdmin_courses.all()

def isCourseStaff(user, course):
  """
  Check if the user is a staff member of the course.
  """
  return isGrader(user, course) or isSuperGrader(user, course) or isCourseAdmin(user, course)

def isCourseMember(user, course):
  return isStudent(user, course) or isCourseStaff(user, course)

# --- Assignment lifecycle predicates -------------------------------------------------
# The single source of truth for what a student may do with an assignment, keyed off
# Assignment.state (+ hideFrom). Callers must have already established the user is a
# student of the course. State is checked first so the hideFrom query only runs for
# assignments the student could otherwise access.

def isAssignmentHiddenFromStudent(user, assignment):
  return assignment.hideFrom.filter(students=user).exists()

def studentCanSeeAssignment(user, assignment):
  """visible/preview/published/closed: the assignment appears in the student console."""
  return (assignment.state in STUDENT_VISIBLE_STATES
          and not isAssignmentHiddenFromStudent(user, assignment))

def studentCanDownloadAssignment(user, assignment):
  """preview/published/closed: starter files are available."""
  return (assignment.state in STUDENT_DOWNLOAD_STATES
          and not isAssignmentHiddenFromStudent(user, assignment))

def studentCanSubmitToAssignment(user, assignment):
  """published only — and not past the deadline (derived close)."""
  return (assignment.effective_state() == 'published'
          and not isAssignmentHiddenFromStudent(user, assignment))

# --- Feedback-axis predicates --------------------------------------------------------
# The single source of truth for what a student may see of grading, keyed off
# Assignment.feedbackStatus (hidden/live/per_student/released) + the orthogonal
# hideGrades modifier. Callers must have already established the caller is the
# submission's student / a course student.

def feedbackOpenForSubmission(submission):
  """Comments/rubric/feedback axis open for this submission's student.

  live: open immediately regardless of finalization.
  released: open only once the submission is finalized.
  per_student: open only once THIS submission is finalized."""
  status = submission.assignment.feedbackStatus
  if status == 'live':
    return True
  if status in ('released', 'per_student'):
    return submission.isFinalized
  return False

def gradesVisibleForSubmission(submission):
  """Numeric grades: feedback open AND not masked by hideGrades."""
  return feedbackOpenForSubmission(submission) and not submission.assignment.hideGrades

def testResultsVisibleForSubmission(submission):
  """Full test results / autograder logs: live immediately; released/per_student only
  once the submission is finalized (the pre-existing conjunction, per_student-aware)."""
  status = submission.assignment.feedbackStatus
  if status == 'live':
    return True
  return status in ('released', 'per_student') and submission.isFinalized

def assignmentFeedbackOpen(assignment, user=None):
  """Assignment-level feedback gate (rubric visibility, serializer choice).

  live/released open for every student; per_student opens for a user once they have a
  finalized submission (one bounded query, only on the per_student path)."""
  status = assignment.feedbackStatus
  if status in ('live', 'released'):
    return True
  if status == 'per_student' and user is not None:
    return assignment.submissions.filter(students=user, isFinalized=True).exists()
  return False

def isSectionLeader(user, section):
  return user in section.leaders.all()

def isStudentOfSub(user, submission):
  return user in submission.students.all()

def isStaffOfSub(user, submission):
  """
  Staff of submission includes:
  - The grader assigned to the submission
  - Any course admin of the course the submission belongs to
  - Any super grader of the course the submission belongs to
  - Any section leader of a section that contains any student of the submission
  """
  
  
  if (user == submission.grader):
    return True
  elif isCourseAdmin(user, submission.assignment.course):
    return True
  elif isSuperGrader(user, submission.assignment.course):
    return True
  else:
    # Since this check is the most computationally expensive, only do it
    # if we need to
    course = submission.assignment.course
    if not isGrader(user, course):
      return False

    # This is expensive, but only performed if the user is a grader of the course
    sections = Section.objects.filter(course=course, leaders__in=[user])
    students = submission.students.all()
    for section in sections:
      for student in students:
        if student in section.students.all():
          return True
  return False

def isSectionLeaderOfStudent(user, course, student):
    if not isGrader(user, course):
      return False

    # This is expensive, but only performed if the user is a grader of the course
    sections = Section.objects.filter(course=course, leaders__in=[user])
    for section in sections:
      if student in section.students.all():
        return True
    return False

def canViewUnanonymizedSubmissions(user, course):
  if isCourseStaff(user, course):
    if isCourseAdmin(user, course):
      return True
    elif isSuperGrader(user, course):
      return True
    else:
      sections = course.sections.all()
      for section in sections:
        if isSectionLeader(user, section):
          return True
      return False
  else:
    return False

def hasCourseCreationPrivilege(user):
  return user.profile.canCreateCourses

def should_use_student_captions(user, course):
    return course.useStudentCaptions and not isCourseAdmin(user, course)

def can_elevate_permissions(user):
    code_in_place = Course.objects.get(id=925) # NOTE: unlikely to work when testing locally
    return code_in_place in user.grader_courses.all()
