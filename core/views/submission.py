# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import re
from core.models import Submission, File
from core.models import User

from core.serializers.submission import AnonymousSubmissionSerializer, SubmissionSerializer, StudentSubmissionSerializer, StudentSubmissionFilesOnlySerializer, SubmissionConsoleDataSerializer, StudentConsoleDataSerializer
from core.serializers.submissionHistory import SubmissionHistorySerializer
from core.serializers.submissionTest import SubmissionTestSerializer
from core.serializers.submissionVariantRun import SubmissionVariantRunSerializer
from core.serializers.file import SubmissionFileEditSaveSerializer, SubmissionFileEditSerializer

from rest_framework.response import Response
from rest_framework.decorators import action
from drf_spectacular.utils import extend_schema, OpenApiParameter, inline_serializer
from drf_spectacular.types import OpenApiTypes
from rest_framework import serializers, status

from core.serializers.actionResponses import (
  SubmissionCheckPermissionResponseSerializer,
  SubmissionTestResultsResponseSerializer,
  SubmissionPartnerLinkResponseSerializer,
)
from core.serializers.suggested_comment import SuggestedCommentSerializer
from core.serializers.submission_summary import SubmissionSummarySerializer

from core.views.template import ListProtectedViewSet
from rest_framework.permissions import IsAuthenticated
from core.permissions.permissions import SubmissionPermissions

from core.permissions.helpers import returnForbidden, returnNotFound, returnInvalid
from core.permissions.helpers import isStudent, isCourseStaff, isCourseAdmin, isStudentOfSub, isStaffOfSub, canViewUnanonymizedSubmissions, isSectionLeaderOfStudent, isSuperGrader
from core.permissions.helpers import feedbackOpenForSubmission, testResultsVisibleForSubmission
from core.permissions.capabilities import Capability, check_capability, compute_submission_capabilities, require_capability
from core.services.audit import record_audit_event

from core.models import SubmissionFileEdit, LearningObjective
from django.utils.timezone import now


from autograder.run import filterExposedSubmissionTests

from core.permissions.tokens import submission_token_generator

from core.emails import StudentFeedbackNotificationEmail, StudentPartnersAddedEmail
from django.db.models import Q, F
from django.db import transaction

# Shown to students in place of raw compiler output when their submission fails to
# compile. Instructors/graders still see the full compiler stderr (in the row logs).
STUDENT_COMPILE_FAILURE_MESSAGE = (
    "Your code didn't compile. Fix the compilation errors shown in your files and resubmit."
)

# Lightweight compile/syntax markers for the student-facing boundary. We also treat
# "errored but produced no per-test results" as a compile/couldn't-run failure, since a
# submission that compiled would have run at least one test.
_COMPILE_MARKERS = re.compile(
    r"\berror:|cannot find symbol|compilation failed|failed to compile|';' expected|"
    r"reached end of file while parsing|\bSyntaxError\b|\bIndentationError\b|invalid syntax|"
    r"unexpected EOF|unexpected token",
    re.IGNORECASE,
)


def _filter_log_to_student_files(logs: str, student_filenames: set) -> str:
    """Keep only log lines that reference the student's own files.

    Compiler output interleaves lines about the student's files (safe, useful —
    e.g. "Car.java:5: error: ';' expected") with lines about instructor TEST files
    (e.g. "TowAwayTest.java: cannot find symbol") and generic framework noise. We
    keep a line only if it names one of the student's own files, so students see
    their own errors without leaking hidden-test names/paths. A compiler message
    spans multiple lines (the error line, the offending source line, a caret, and
    sometimes "symbol:"/"location:" lines); we keep the continuation lines that
    follow a kept error line until the next file-referencing line.
    """
    if not logs or not student_filenames:
        return ''
    kept = []
    keeping = False
    for line in logs.splitlines():
        # Does this line name a file? (path ending in one of our known basenames)
        mentions_student = any(fn in line for fn in student_filenames)
        # A line that references a .java/.py/etc. file path we DON'T own ends a keep run.
        mentions_other_file = ('.java:' in line or '.py:' in line) and not mentions_student
        # Runner/VM noise that can follow the javac block but isn't the student's error.
        is_runner_noise = line.lstrip().startswith((
            'Error: Could not', 'Caused by:', 'Exception in thread', 'at ', 'java.lang.'))
        if mentions_student:
            keeping = True
            kept.append(line)
        elif mentions_other_file or is_runner_noise:
            keeping = False  # switched away from the student's compiler diagnostic
        elif keeping:
            # Continuation of a kept compiler message (source echo, caret, symbol/location).
            kept.append(line)
    return '\n'.join(kept).strip()


def _sanitize_student_test_row(row: dict, student_filenames: set | None = None) -> dict:
    """Shape a serialized SubmissionTest for a STUDENT viewer.

    - A genuine COMPILE failure (compiler-error markers present in the log) collapses
      to the generic "didn't compile" message PLUS the compiler lines that reference
      the student's OWN files (their real errors), with instructor-test lines stripped.
    - Any other errored row (OOM, timeout, a test that didn't run) keeps its own
      per-test error/message; its raw execution log is dropped (grader-only, and it
      can enumerate hidden test names). It is NOT mislabeled as a compile failure.
    Staff rows must NOT be passed through here.
    """
    student_filenames = student_filenames or set()
    logs = row.get('logs') or ''
    looks_like_compile = _COMPILE_MARKERS.search(logs) is not None

    if looks_like_compile:
        # Real compile failure. Show the generic banner + only the student's own
        # compiler lines (never instructor test-file lines).
        student_detail = _filter_log_to_student_files(logs, student_filenames)
        error_text = STUDENT_COMPILE_FAILURE_MESSAGE
        if student_detail:
            error_text = f"{STUDENT_COMPILE_FAILURE_MESSAGE}\n\n{student_detail}"
        row['logs'] = ''
        row['isError'] = True
        row['results'] = [{
            'name': 'Compilation',
            'passed': False,
            'score': 0,
            'maxScore': row.get('maxScore') or 0,
            'status': 'compile_error',
            'error': error_text,
        }]
    else:
        # Not a compile failure: OOM / timeout / assertion / didn't-run. Keep the
        # per-test results (they carry the student-appropriate message); only drop
        # the raw execution log, which is grader-only and can list hidden tests.
        row['logs'] = ''
        results = row.get('results')
        has_test_results = bool(results) if isinstance(results, (list, dict)) else False
        # An errored row with no per-test results and no compile markers is a test
        # that was prevented from running (e.g. the JVM ran out of memory or the
        # whole run was aborted). Give the student a clear, non-leaky message instead
        # of a blank row — but DON'T call it a compile error.
        if bool(row.get('isError')) and not has_test_results:
            row['results'] = [{
                'name': 'Did not run',
                'passed': False,
                'score': 0,
                'maxScore': row.get('maxScore') or 0,
                'status': 'error',
                'error': "This test did not run. The program may have run out of memory, "
                         "timed out, or crashed before this test executed.",
            }]
    return row


def get_student_serializer_class(submission, files_only=False):
    """
    Get the appropriate serializer for a student viewing their submission.
    
    Args:
        submission: The submission object
        files_only: If True, return serializer with only files (no comments/grades)
    """
    if files_only:
        return StudentSubmissionFilesOnlySerializer
    
    # StudentSubmissionSerializer handles all cases:
    # - Masks grade while the feedback axis is closed (or hideGrades)
    # - Returns files without comments while the feedback axis is closed
    # - Preserves real isFinalized status so frontend can show submission correctly
    return StudentSubmissionSerializer



class SubmissionViewSet(ListProtectedViewSet):
  """
  list:
  Return a list of all the submissions.

  create:
  Create a new submission.

  retrieve:
  Return the given submission.

  update:
  Update a submission.

  partial_update:
  Update a submission.

  delete:
  Delete a submission.
  """
  queryset = Submission.objects.all()
  serializer_class = SubmissionSerializer
  permission_classes = (IsAuthenticated, SubmissionPermissions)

  def get_serializer_class(self):
    # During schema generation, return default serializer
    if getattr(self, 'swagger_fake_view', False):
        return SubmissionSerializer
        
    if self.action in ['retrieve', 'update', 'partial_update']:
        user = self.request.user
        submission = self.get_object()
        assignment = submission.assignment
        course = submission.assignment.course

        # Check if files-only mode is requested
        files_only = self.request.query_params.get('filesOnly', 'false').lower() == 'true'

        # NOTE: we need to write this logic in descending order of privilege. For example, if a user
        # is both an admin and a student, we don't want to restrict that user's access to submissions
        # of which that user is a student before the associated assignment is released

        if isCourseAdmin(user, course):
            return SubmissionSerializer
        elif isCourseStaff(user, course):
          if isStudentOfSub(user, submission):
            return get_student_serializer_class(submission, files_only=files_only)
          elif (not assignment.anonymousGrading) or canViewUnanonymizedSubmissions(user, course):
            return SubmissionSerializer
          else:
            return AnonymousSubmissionSerializer
        else:
          # user is *only* a student
          return get_student_serializer_class(submission, files_only=files_only)

    else:
        return SubmissionSerializer

  def perform_create(self, serializer):
    submission = serializer.save()
    course = submission.assignment.course
    students = submission.students.all()
    user = students.first() if students.exists() else self.request.user
    record_audit_event(
        course=course,
        event_type='submission_attempt',
        user=user,
        assignment=submission.assignment,
        submission=submission,
    )

  def create(self, request, *args, **kwargs):
    try:
        return super().create(request, *args, **kwargs)
    except Exception as exc:
        # Log the failed submission attempt if we can determine the assignment
        assignment_id = request.data.get('assignment')
        if assignment_id:
            try:
                from core.models import Assignment
                assignment = Assignment.objects.select_related('course').get(id=assignment_id)
                record_audit_event(
                    course=assignment.course,
                    event_type='submission_failed',
                    user=request.user,
                    assignment=assignment,
                    meta={'error': str(exc)},
                )
            except Exception:
                pass
        raise

  @extend_schema(responses=SubmissionCheckPermissionResponseSerializer)
  @action(detail=True, methods=['get'])
  def checkPermission(self, request, pk=None):
    user = request.user
    try:
      submission = Submission.objects.get(id=pk)
    except Submission.DoesNotExist:
      return returnNotFound()

    caps = compute_submission_capabilities(user, submission)

    canRead = caps.get('view_submission', False)
    canWrite = caps.get('grade_submission', False)
    canViewFeedback = caps.get('view_feedback', False)

    toRet = {
      'read': canRead,
      'write': canWrite,
      'filesOnly': canRead and not canViewFeedback,
    }

    # Record audit event for student viewing their submission
    if canRead and not canWrite:
      course = submission.assignment.course
      if canViewFeedback:
          record_audit_event(
              course=course,
              event_type='feedback_view',
              user=user,
              assignment=submission.assignment,
              submission=submission,
          )
      else:
          record_audit_event(
              course=course,
              event_type='file_view',
              user=user,
              assignment=submission.assignment,
              submission=submission,
          )

    # Attach fine-grained capabilities alongside legacy fields
    toRet['capabilities'] = caps

    return Response(toRet)


# Optional argument: student username
  @extend_schema(responses=SubmissionHistorySerializer(many=True))
  @action(detail=True, methods=['GET', 'PATCH'])
  def history(self, request, pk=None):
    user = request.user
    submission = Submission.objects.get(id=pk)
    course = submission.assignment.course

    student = self.request.query_params.get('student', None)

    isThisStudent = isStudent(user, course) and user.email == student
    submissionHistories = submission.histories.all().prefetch_related('student')

    # If you want all the submission's histories, you need to be an admin or supergrader of submission, and the request must be a get request
    studentParam = None
    if student is None:
      if request.method != "GET" or not isCourseAdmin(user, course) and not isStaffOfSub(user,submission):
        return returnForbidden()
    else:
    # Retrieve student
      try:
        studentParam = User.objects.filter(Q(username=student) | Q(email=student)).first()
        if studentParam is None:
          raise User.DoesNotExist
      except User.DoesNotExist:
        
        if isCourseAdmin(user, course):
          return returnNotFound(message="The user does not exist")
        else:
          return returnForbidden()
        

    # If you want to filter by the student, and want to post/patch/delete, you need to be the student
    # If you want to filter by the student, and get, then you need to be an admin, supergrader, or sectioNleader of student
    if student is not None:
      if request.method != "GET" and not isThisStudent:
        return returnForbidden()
      if not isThisStudent and not isCourseAdmin(user, course) and not isSuperGrader(user, course) and not isSectionLeaderOfStudent(user, course, studentParam):
        return returnForbidden()

    histories = submissionHistories.filter(student=studentParam) if studentParam is not None else submissionHistories
    if (request.method == "PATCH") and 'hasViewed' in request.data:
        newFields = {"student": studentParam.email, "hasViewed": request.data['hasViewed']}  # type: ignore[union-attr]  # studentParam checked above

        serializer = SubmissionHistorySerializer(histories[0], newFields, many=False, context={"request": request})
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
    else:
        serializer = SubmissionHistorySerializer(histories, many=True, context={"request": request})
        
    return Response(serializer.data)


################################# Regrade Functions ##########################################

  @extend_schema(responses=StudentSubmissionSerializer)
  @action(detail=True, methods=['PATCH'])
  def submitRegrade(self, request, pk=None):
    user = request.user
    submission = Submission.objects.get(id=pk)
    course = submission.assignment.course

    require_capability(user, 'request_regrade', submission)

    if not submission.isFinalized:
      raise serializers.ValidationError("Your submission has not been graded yet. You can submit a regrade request after your submission has been graded.")

    if submission.questionResponder or submission.questionResponse:
      raise serializers.ValidationError("Your request is currently being reviewed by a grader. Changes cannot be made at this time.")

    if submission.assignment.regradeDeadline and now() > submission.assignment.regradeDeadline:
      raise serializers.ValidationError("The regrade request deadline for this assignment has passed.")

    # Current design decision is to not allow students to update their regrade request after submit
    if submission.questionText:
      raise serializers.ValidationError("You have already submitted a regrade request for this submission.")

    if 'questionText' not in request.data:
      raise serializers.ValidationError("questionText field is not provided.")

    if not request.data['questionText'].strip():
      raise serializers.ValidationError("questionText cannot be empty.")

    submission.questionIsOpen = True
    submission.questionText = request.data['questionText']
    if(request.data['questionIsRegrade']):
      submission.questionIsRegrade = True
    else:
      submission.questionIsRegrade = False

    submission.questionDate = now()
    submission.save()

    record_audit_event(
        course=course,
        event_type='regrade_request',
        user=user,
        assignment=submission.assignment,
        submission=submission,
        meta={'questionText': request.data['questionText'], 'isRegrade': bool(request.data.get('questionIsRegrade'))},
    )

    serializer = StudentSubmissionSerializer(submission, many=False, context={"request": request})

    return Response(serializer.data)

  @extend_schema(responses=StudentSubmissionSerializer)
  @action(detail=True, methods=['PATCH'])
  def deleteRegrade(self, request, pk=None):
    user = request.user
    submission = Submission.objects.get(id=pk)
    course = submission.assignment.course

    require_capability(user, 'request_regrade', submission)

    if not submission.isFinalized:
      raise serializers.ValidationError("Your submission has not been graded yet.")

    if submission.questionResponder or submission.questionResponse:
      raise serializers.ValidationError("Your request is currently being reviewed by a grader. Changes cannot be made at this time.")

    submission.questionIsOpen = False
    submission.questionText = ''
    submission.questionDate = None
    submission.save()

    record_audit_event(
        course=course,
        event_type='regrade_deleted',
        user=user,
        assignment=submission.assignment,
        submission=submission,
    )

    serializer = StudentSubmissionSerializer(submission, many=False, context={"request": request})

    return Response(serializer.data)


  #################################################################################
  #  DEPRACATED: Included here for backwards-compatibility
  #################################################################################

  @extend_schema(responses=SubmissionTestSerializer(many=True))
  @action(detail=True, methods=["GET"])
  def submissionTests(self, request, pk=None):
    #  Only accessed by students
    user = request.user
    submission = self.get_object()
    assignment = submission.assignment
    isStudentMode = self.request.query_params.get('isStudentMode', "False") == "True"

    # If admin and admin is not in studentmode
    if isStaffOfSub(user, submission) and not isStudentMode:
            tests = submission.tests.all()
    # If student of the submission
    elif isStudentOfSub(user, submission):
        # Graded reveal: full test results per the feedback axis (finalized subs)
        if testResultsVisibleForSubmission(submission):
            tests = submission.tests.all()
        else:
            maxFailedTests = assignment.environment and assignment.environment.maxExposedFailedTests
            tests = filterExposedSubmissionTests(list(submission.tests.all()), maxFailedTests)[0]
    else:
        return returnForbidden()

    serializer = SubmissionTestSerializer(tests, many=True, context={"request": request})
    return Response(serializer.data)

  @extend_schema(responses=SubmissionVariantRunSerializer(many=True))
  @action(detail=True, methods=["GET"])
  def variantRuns(self, request, pk=None):
    """Variant-robustness reruns (see AssignmentDataSet.autogradeAllVariants) — staff-only,
    never shown to students."""
    user = request.user
    submission = self.get_object()
    if not isStaffOfSub(user, submission):
      return returnForbidden()
    runs = submission.variant_runs.select_related('dataset').all()
    serializer = SubmissionVariantRunSerializer(runs, many=True, context={"request": request})
    return Response(serializer.data)

  #################################################################################

  @extend_schema(responses=SubmissionTestResultsResponseSerializer)
  @action(detail=True, methods=["GET"])
  def testResults(self, request, pk=None):
    #  Only accessed by students
    user = request.user
    submission = Submission.objects.get(id=pk)
    assignment = submission.assignment
    isStudentMode = self.request.query_params.get('isStudentMode', "False") == "True"

    # Ensure the user can at least view this submission
    require_capability(user, 'view_submission', submission)

    # contents of _tests.txt
    logCode = ''
    def retrieve_log_code(submission):
        try:
            logFile = File.objects.get(submission=submission, name="_tests.txt")
            return logFile.data
        except:
            return ''
        
    

    # If admin and admin is not in studentmode
    if isStaffOfSub(user, submission) and not isStudentMode:
            tests = submission.tests.all()
            logCode = retrieve_log_code(submission)
    # If student of the submission
    elif isStudentOfSub(user, submission):
        # Graded reveal: full test results per the feedback axis (finalized subs)
        if testResultsVisibleForSubmission(submission):
            tests = submission.tests.all()
            logCode = retrieve_log_code(submission)
        else:
            try:
                maxFailedTests = assignment.environment and assignment.environment.maxExposedFailedTests
            except:
                maxFailedTests = None

            tests = filterExposedSubmissionTests(list(submission.tests.all()), maxFailedTests)[0]

            try:
                logCode = retrieve_log_code(submission)
            except:
                logCode = ''

    else:
        return returnForbidden()

    submissionTests = SubmissionTestSerializer(tests, many=True, context={"request": request}).data

    # "Student view" = anyone who shouldn't see hidden-test internals: actual students,
    # plus staff who explicitly opted into isStudentMode.
    isStudentView = not (isStaffOfSub(user, submission) and not isStudentMode)

    # For student views, replace each hidden-test row with one synthetic per-category
    # summary so the student gets feedback (pass count + point impact) without seeing
    # the underlying test names, logs, or explanations.
    if isStudentView:
        tests_by_id = {t.id: t for t in tests}
        # The student's own submitted filenames (basenames). Used to keep only the
        # compiler lines that reference their own files when surfacing a compile error.
        student_filenames = set()
        try:
            for f in submission.files.all():
                if f.name:
                    student_filenames.add(f.name)
        except Exception:
            student_filenames = set()
        hidden_by_category: dict[int, list] = {}
        visible_rows = []
        for st_data in submissionTests:
            st = tests_by_id.get(st_data.get('id'))
            if st is not None and st.testCase.hidden:
                hidden_by_category.setdefault(st.testCase.testCategory_id, []).append(st)
            else:
                # Student-facing shaping: generic compile message + drop raw execution
                # logs (grader-only). Assertion/runtime per-test messages are preserved.
                visible_rows.append(_sanitize_student_test_row(st_data, student_filenames))

        synthetic_rows = []
        for cat_id, hidden_tests in hidden_by_category.items():
            passed_count = sum(1 for t in hidden_tests if t.passed)
            total_count = len(hidden_tests)
            points_earned = sum((t.score or 0) for t in hidden_tests)
            points_total = sum((t.maxScore or 0) for t in hidden_tests)
            synthetic_rows.append({
                # Negative id sentinels distinguish synthetic summary rows from real
                # SubmissionTests; clients should treat hiddenSummary != null as the
                # source of truth, not the id.
                'id': -cat_id,
                'submission': submission.id,
                'testCase': None,
                'testCategory': cat_id,
                'logs': '',
                'passed': passed_count == total_count and total_count > 0,
                'created': None,
                'modified': None,
                'isError': False,
                'score': points_earned,
                'maxScore': points_total,
                'results': None,
                'hiddenSummary': {
                    'label': 'Hidden tests',
                    'passedCount': passed_count,
                    'totalCount': total_count,
                    'pointsEarned': points_earned,
                    'pointsTotal': points_total,
                },
            })
        submissionTests = visible_rows + synthetic_rows

    # Compute learning objective summary for all views.
    # Prefetch testCases.id once per objective rather than re-querying inside the loop.
    objectives = LearningObjective.objects.filter(assignment=assignment).prefetch_related('testCases')
    objective_summary = []
    for obj in objectives:
        linked_test_ids = {tc.id for tc in obj.testCases.all()}
        # Submission test results restricted to this objective's linked test cases.
        linked_results = [t for t in tests if t.testCase.id in linked_test_ids]

        # Skip objectives that have no linked test results in this submission — they would
        # display as permanently "not met / 0.0" with no signal, which is misleading.
        if not linked_results:
            continue

        passed_count = sum(1 for t in linked_results if t.passed)
        total_count = len(linked_results)
        all_passed = passed_count == total_count
        any_failed = passed_count < total_count

        # Compute score + met. Each mode is intentionally distinct:
        #   - all:             binary; met iff every linked test passes.
        #   - any:             binary; met iff at least one linked test passes.
        #   - percentage:      fractional pass rate; met iff > 50% pass.
        #   - points_weighted: fractional points share; met iff > 50% of points earned.
        if obj.aggregationMode == 'any':
            met = passed_count > 0
            score = 1.0 if met else 0.0
        elif obj.aggregationMode == 'percentage':
            score = passed_count / total_count
            met = score > 0.5
        elif obj.aggregationMode == 'points_weighted':
            total_points = sum(t.testCase.pointsPass for t in linked_results)
            if total_points > 0:
                earned_points = sum(t.testCase.pointsPass for t in linked_results if t.passed)
                score = earned_points / total_points
            else:
                score = 0.0
            met = score > 0.5
        else:  # 'all' (default)
            met = all_passed
            score = 1.0 if met else 0.0

        # For student views, apply visibility mode filtering
        if isStudentView:
            visible = False
            if obj.visibilityMode == 'always':
                visible = True
            elif obj.visibilityMode == 'on_pass' and all_passed:
                visible = True
            elif obj.visibilityMode == 'on_fail' and any_failed:
                visible = True
            # 'never' stays False
        else:
            # Staff always see all objectives
            visible = True

        if visible:
            objective_summary.append({
                'id': obj.id,
                'shortId': obj.shortId,
                'name': obj.name,
                'description': obj.description,
                'met': met,
                'score': round(score, 4),
                'aggregationMode': obj.aggregationMode,
            })

    return Response({'submissionTests': submissionTests, 'logs': logCode, 'learningObjectives': objective_summary})

  #################################################################################

  @extend_schema(responses=SubmissionPartnerLinkResponseSerializer)
  @action(detail=True, methods=["GET"])
  def generatePartnerLink(self, request, pk=None):
    user = request.user
    # get_object() runs SubmissionPermissions — the caller must already be on the submission.
    submission = self.get_object()
    assignment = submission.assignment

    if not assignment.allowStudentUploadWithPartners:
        return returnForbidden()

    require_capability(user, 'manage_partners', submission)

    if submission.isFinalized:
        return returnForbidden()

    token = submission_token_generator.make_token(submission)

    return Response({
      'id': submission.id,
      'token': token,
    })

  @extend_schema(
    responses=OpenApiTypes.STR,
    parameters=[
      OpenApiParameter(
        name="token",
        type=OpenApiTypes.STR,
        location=OpenApiParameter.QUERY,
        required=True,
      ),
    ],
  )
  @action(detail=True, methods=["GET"])
  def validatePartnerLink(self, request, pk=None):
    user = request.user
    submission = Submission.objects.get(id=pk)
    assignment = submission.assignment
    course = submission.assignment.course
    token = request.query_params.get('token', None)

    if not assignment.allowStudentUploadWithPartners:
        return returnInvalid()

    if not token:
        return returnInvalid()

    # The invitee must be a student who can submit to this assignment themselves:
    # assignment published and open, not hidden from their section. (isStudent excludes
    # the capability's admin arm — staff are not partner material.) Opaque 406 (not 403)
    # so a hidden assignment's existence is not leaked.
    if not isStudent(user, course) or not check_capability(user, Capability.UPLOAD_SUBMISSION, assignment):
        return returnInvalid()

    is_valid = submission_token_generator.check_token(submission, token)
    if is_valid:
        # Atomic re-check: two concurrent accepts (or an accept racing studentUpload)
        # must not attach the user to a second submission for this assignment.
        with transaction.atomic():
            already = Submission.objects.filter(
                assignment=submission.assignment, students__in=[user]).exists()
            if already:
                return returnInvalid()
            submission.students.add(user)

        for student in submission.students.all():
            StudentPartnersAddedEmail(student).send_email(
                new_partner_email=user.email,
                submission=submission,
            )
            # send_email_updated_partners(student.email, user.email, ", ".join(list(submission.students.all().values_list('email', flat=True))), submission.assignment, course)

        return Response("ok", status.HTTP_200_OK)
    else:
        return returnInvalid()

  @extend_schema(
    responses=StudentSubmissionSerializer,
    parameters=[
      OpenApiParameter(
        name="token",
        type=OpenApiTypes.STR,
        location=OpenApiParameter.QUERY,
        required=True,
      ),
    ],
  )
  @action(detail=True, methods=["GET"])
  def validatePartnerLinkAndReturn(self, request, pk=None):
    user = request.user
    submission = Submission.objects.get(id=pk)
    assignment = submission.assignment
    course = submission.assignment.course
    token = request.query_params.get('token', None)

    if not assignment.allowStudentUploadWithPartners:
        return returnInvalid()

    if not token:
        return returnInvalid()

    # Same invitee gate as validatePartnerLink (opaque 406 — see comment there).
    if not isStudent(user, course) or not check_capability(user, Capability.UPLOAD_SUBMISSION, assignment):
        return returnInvalid()

    current_submission = Submission.objects.filter(assignment=submission.assignment, students__in=[user])

    if len(current_submission) > 0:
        return returnInvalid()

    is_valid = submission_token_generator.check_token(submission, token)
    if is_valid:
        serializer = StudentSubmissionSerializer(submission, many=False, context={"request": request})
        return Response(serializer.data)
    else:
        return returnInvalid()

  @extend_schema(responses=OpenApiTypes.STR)
  @action(detail=True, methods=["GET"])
  def removePartner(self, request, pk=None):
    user = request.user
    # get_object() runs SubmissionPermissions — the caller must already be on the submission.
    submission = self.get_object()

    require_capability(user, 'manage_partners', submission)

    submission.students.remove(user)

    return Response('ok')

  @extend_schema(responses=OpenApiTypes.STR)
  @action(detail=True, methods=["POST"])
  def notifyStudents(self, request, pk=None):
    user = request.user
    submission = Submission.objects.get(id=pk)

    require_capability(user, 'notify_students_feedback', submission)

    if not feedbackOpenForSubmission(submission):
        return Response('Feedback must be released', status.HTTP_406_NOT_ACCEPTABLE)

    if not submission.isFinalized:
        return Response('Submission must be finalized', status.HTTP_406_NOT_ACCEPTABLE)

    # QUESTION: We could make the setting a requirement. Or we could make this globally accessible for staff.
    # if not submission.assignment.course.enableStudentFeedbackNotifications:
    #     return Response('Course EnableStudentFeedbackNotifications must be turned on', status.HTTP_406_NOT_ACCEPTABLE)

    _view_submission_url = 'https://compedu.stanford.edu/codeinplace/v1/#/submissions' if submission.assignment.course.id == 925 else 'https://codepost.io/code/{}'.format(submission.id)

    

    for student in submission.students.all():
        StudentFeedbackNotificationEmail(student).send_email(submission)

    return Response('Notifications sent!', status.HTTP_200_OK)

  @extend_schema(
      responses=SubmissionConsoleDataSerializer,
      description="Return the full nested submission data for the code console in a single request. "
                  "Includes files with nested comments (and rubricComment data). "
                  "Eliminates the N+1 fetch waterfall.",
  )
  @action(detail=True, methods=['GET'])
  def consoleData(self, request, pk=None):
    """
    Bulk endpoint for the code console. Returns submission → files → comments
    in one response, replacing dozens of individual API calls.
    """
    user = request.user
    try:
      submission = Submission.objects.select_related(
          'assignment__course',
          'grader',
      ).prefetch_related(
          'students',
          'files__comments__author',
          'files__comments__rubricComment',
          'files__comments__tags',
          'files__edit',
          'tests',
      ).get(id=pk)
    except Submission.DoesNotExist:
      return returnNotFound()

    course = submission.assignment.course

    # Use the same permission/serializer logic as retrieve
    context = {'request': request}
    if isCourseAdmin(user, course):
      serializer_class = SubmissionConsoleDataSerializer
    elif isCourseStaff(user, course):
      if isStudentOfSub(user, submission):
        serializer_class = StudentConsoleDataSerializer
      elif (not submission.assignment.anonymousGrading) or canViewUnanonymizedSubmissions(user, course):
        serializer_class = SubmissionConsoleDataSerializer
      else:
        serializer_class = SubmissionConsoleDataSerializer
        context['anonymize'] = True
    elif isStudentOfSub(user, submission):
      serializer_class = StudentConsoleDataSerializer
    else:
      return returnForbidden()

    serializer = serializer_class(submission, context=context)
    return Response(serializer.data)

  @extend_schema(
      request=SubmissionFileEditSaveSerializer,
      responses=SubmissionFileEditSerializer,
      description=(
        "Create or update a persisted edit for a submission file. "
        "Course admins may always save edits; graders may save only when the "
        "assignment's `gradersCanEditSubmissions` flag is True."
      ),
  )
  @action(detail=True, methods=['PATCH'], permission_classes=[IsAuthenticated])
  def saveFileEdit(self, request, pk=None):
    submission: Submission = self.get_object()
    user = request.user

    require_capability(user, Capability.GRADE_SUBMISSION, submission)

    course = submission.assignment.course
    if not isCourseAdmin(user, course) and not submission.assignment.gradersCanEditSubmissions:
      return returnForbidden()

    serializer = SubmissionFileEditSaveSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    file_id = serializer.validated_data['fileId']
    data = serializer.validated_data['data']

    try:
      submission_file = submission.files.get(id=file_id)
    except submission.files.model.DoesNotExist:
      raise serializers.ValidationError({'fileId': 'That file does not belong to this submission.'})

    with transaction.atomic():
      edit, _created = SubmissionFileEdit.objects.update_or_create(
          file=submission_file,
          defaults={'data': data, 'lastEditedBy': user},
      )

    return Response(SubmissionFileEditSerializer(edit).data, status.HTTP_200_OK)

  @extend_schema(
      responses=SuggestedCommentSerializer(many=True),
      description="List all pending AI-suggested comments for this submission. Staff only.",
  )
  @action(detail=True, methods=["GET"])
  def suggestedComments(self, request, pk=None):
    """Return pending suggested comments for this submission."""
    from django.utils import timezone
    from core.models import SuggestedComment
    from core.serializers.suggested_comment import SuggestedCommentSerializer

    submission = self.get_object()
    user = request.user

    require_capability(user, 'view_ai_assistance', submission)

    suggestions = SuggestedComment.objects.filter(
        submission=submission, status='pending'
    ).select_related('file', 'rubricComment')

    # Stamp first_viewed_at on suggestions that haven't been viewed yet
    not_yet_viewed = suggestions.filter(firstViewedAt__isnull=True)
    if not_yet_viewed.exists():
        not_yet_viewed.update(firstViewedAt=timezone.now())
        # Re-fetch to include the updated timestamps
        suggestions = SuggestedComment.objects.filter(
            submission=submission, status='pending'
        ).select_related('file', 'rubricComment')

    return Response(SuggestedCommentSerializer(suggestions, many=True).data)

  @extend_schema(
      responses=SubmissionSummarySerializer,
      description="Get the AI-generated summary for this submission. Staff only.",
  )
  @action(detail=True, methods=["GET"])
  def summary(self, request, pk=None):
    """Return the AI-generated summary for this submission."""
    from core.models import SubmissionSummary
    from core.serializers.submission_summary import SubmissionSummarySerializer

    submission = self.get_object()
    user = request.user

    require_capability(user, 'view_ai_assistance', submission)

    try:
        summary_obj = SubmissionSummary.objects.get(submission=submission)
    except SubmissionSummary.DoesNotExist:
        return returnNotFound()

    return Response(SubmissionSummarySerializer(summary_obj).data)

  @extend_schema(
      responses={202: inline_serializer('GenerateAIAssistanceResponse', fields={
          'status': serializers.CharField(),
          'submissionId': serializers.IntegerField(),
      })},
      description="Manually trigger or regenerate AI summary and suggested comments. Staff only.",
  )
  @action(detail=True, methods=["POST"], permission_classes=[IsAuthenticated])
  def generateAIAssistance(self, request, pk=None):
    """Trigger AI grading assistance (summary + suggested comments) for this submission."""
    submission = Submission.objects.get(id=pk)
    user = request.user

    require_capability(user, 'trigger_ai_assistance', submission)

    from core.tasks import generate_ai_grading_assistance
    generate_ai_grading_assistance.delay(submission.id)

    return Response({'status': 'queued', 'submissionId': submission.id}, status.HTTP_202_ACCEPTED)

  @extend_schema(
      request=inline_serializer('GenerateFileSuggestionsRequest', fields={
          'fileId': serializers.IntegerField(help_text="ID of the submission file to generate suggestions for."),
      }),
      responses=SuggestedCommentSerializer(many=True),
      description="Generate AI-suggested comments for a specific file in this submission. Runs synchronously. Staff only.",
  )
  @action(detail=True, methods=["POST"], permission_classes=[IsAuthenticated])
  def generateFileSuggestions(self, request, pk=None):
    """Generate AI suggestions for a single file within this submission."""
    import json
    import uuid
    from asgiref.sync import async_to_sync
    from core.models import SuggestedComment, SubmissionFile, SystemPromptVariant
    from core.serializers.suggested_comment import SuggestedCommentSerializer as SCSer
    from core.services.ai_service import AIService

    submission = self.get_object()
    user = request.user

    require_capability(user, 'trigger_ai_assistance', submission)

    file_id = request.data.get('fileId')
    if not file_id:
        return Response({'error': 'fileId is required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        file_obj = SubmissionFile.objects.get(id=file_id, submission=submission)
    except SubmissionFile.DoesNotExist:
        return returnNotFound()

    course = submission.assignment.course
    service = AIService(course, submission.assignment)

    if not service.is_configured:
        return Response({'error': 'AI is not configured for this course.'}, status=status.HTTP_400_BAD_REQUEST)

    if not service.is_feature_enabled('suggested_comments'):
        return Response({'error': 'Suggested comments are disabled for this course.'}, status=status.HTTP_400_BAD_REQUEST)

    service.set_request_context(user=user, request_type='file_suggestions')

    # Check for A/B experiment
    experiment = AIService.check_experiment('suggested_comments')
    is_custom = bool(submission.assignment.ai_system_prompt)

    if experiment:
        # A/B mode: generate from both variants, return both for user comparison
        results_a = async_to_sync(service.generate_file_suggestions)(
            submission, file_obj, variant_id_override=experiment.variant_a_id,
        )
        results_b = async_to_sync(service.generate_file_suggestions)(
            submission, file_obj, variant_id_override=experiment.variant_b_id,
        )

        def _extract_text(results):
            parts = []
            for r in results:
                if r.success and r.text:
                    parts.append(r.text)
            return parts

        return Response({
            'isAbTest': True,
            'experimentId': experiment.id,
            'variantAId': experiment.variant_a_id,
            'variantBId': experiment.variant_b_id,
            'isCustomContext': is_custom,
            'resultA': _extract_text(results_a),
            'resultB': _extract_text(results_b),
        })

    results = async_to_sync(service.generate_file_suggestions)(submission, file_obj)

    # Clear existing pending suggestions for this file to prevent duplicates
    SuggestedComment.objects.filter(
        submission=submission, file=file_obj, status='pending'
    ).delete()

    batch_id = uuid.uuid4()
    created = []
    for result in results:
        if result.success and result.text:
            try:
                suggestions = json.loads(result.text)
            except (json.JSONDecodeError, TypeError):
                return Response(
                    {'error': 'AI returned malformed output. Please try again.'},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            # Resolve the prompt variant FK
            prompt_variant = None
            if result.variant_id is not None:
                prompt_variant = SystemPromptVariant.objects.filter(pk=result.variant_id).first()

            is_notebook = file_obj.name.endswith('.ipynb')
            for s in suggestions:
                if s.get('file_id') != file_obj.id:
                    continue
                # For notebooks, convert 1-based cell numbers to 0-based indices
                start_line = s.get('start_line', 0)
                end_line = s.get('end_line', 0)
                if is_notebook:
                    start_line = max(0, start_line - 1)
                    end_line = max(0, end_line - 1)
                created.append(SuggestedComment.objects.create(
                    submission=submission,
                    file=file_obj,
                    text=s.get('text', ''),
                    startLine=start_line,
                    endLine=end_line,
                    startChar=s.get('start_char', 0),
                    endChar=s.get('end_char', 0),
                    rubricComment_id=s.get('rubric_comment_id'),
                    pointDelta=s.get('point_delta'),
                    generationMetadata={
                        'provider': service.provider,
                        'model': service.model,
                        'input_tokens': result.input_tokens,
                        'output_tokens': result.output_tokens,
                        'variant_id': result.variant_id,
                    },
                    promptVariant=prompt_variant,
                    generationBatch=batch_id,
                ))
            service.record_usage(result, user, request_type='file_suggestions')
        elif not result.success:
            return Response({'error': result.error or 'AI generation failed.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Include variant_id and custom-context flag in response metadata
    variant_id = None
    is_custom = bool(submission.assignment.ai_system_prompt)
    for r in results:
        if r.variant_id is not None:
            variant_id = r.variant_id
            break

    response_data = SCSer(created, many=True).data
    return Response({
        'suggestions': response_data,
        'promptVariantId': variant_id,
        'isCustomContext': is_custom,
    }, status=status.HTTP_201_CREATED)

  @extend_schema(
      request=None,
      responses=SubmissionSummarySerializer,
      description="Generate or regenerate the AI summary for this submission. Runs synchronously. Staff only.",
  )
  @action(detail=True, methods=["POST"], permission_classes=[IsAuthenticated])
  def generateSummary(self, request, pk=None):
    """Generate or regenerate the AI summary for this submission on demand."""
    from asgiref.sync import async_to_sync
    from core.models import SubmissionSummary
    from core.serializers.submission_summary import SubmissionSummarySerializer as SSSer
    from core.services.ai_service import AIService

    submission = self.get_object()
    user = request.user

    require_capability(user, 'trigger_ai_assistance', submission)

    course = submission.assignment.course
    service = AIService(course, submission.assignment)

    if not service.is_configured:
        return Response({'error': 'AI is not configured for this course.'}, status=status.HTTP_400_BAD_REQUEST)

    if not service.is_feature_enabled('submission_summary'):
        return Response({'error': 'Submission summaries are disabled for this course.'}, status=status.HTTP_400_BAD_REQUEST)

    service.set_request_context(user=user, request_type='submission_summary')

    # Check for A/B experiment
    experiment = AIService.check_experiment('submission_summary')
    is_custom = bool(submission.assignment.ai_system_prompt)

    if experiment:
        result_a = async_to_sync(service.generate_submission_summary)(
            submission, variant_id_override=experiment.variant_a_id,
        )
        result_b = async_to_sync(service.generate_submission_summary)(
            submission, variant_id_override=experiment.variant_b_id,
        )
        return Response({
            'isAbTest': True,
            'experimentId': experiment.id,
            'variantAId': experiment.variant_a_id,
            'variantBId': experiment.variant_b_id,
            'isCustomContext': is_custom,
            'resultA': {'text': result_a.text, 'success': result_a.success, 'error': result_a.error},
            'resultB': {'text': result_b.text, 'success': result_b.success, 'error': result_b.error},
        })

    result = async_to_sync(service.generate_submission_summary)(submission)

    if not result.success:
        return Response({'error': result.error or 'AI generation failed.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    summary_obj, created = SubmissionSummary.objects.update_or_create(
        submission=submission,
        defaults={
            'text': result.text,
            'generationMetadata': {
                'provider': service.provider,
                'model': service.model,
                'input_tokens': result.input_tokens,
                'output_tokens': result.output_tokens,
                'variant_id': result.variant_id,
            },
        },
    )

    # Track regeneration: if the summary already existed, increment the counter
    if not created:
        SubmissionSummary.objects.filter(pk=summary_obj.pk).update(
            regenerationCount=F('regenerationCount') + 1,
        )
        summary_obj.refresh_from_db()

    service.record_usage(result, user, request_type='submission_summary')

    response_data = SSSer(summary_obj).data
    response_data['promptVariantId'] = result.variant_id
    response_data['isCustomContext'] = is_custom
    return Response(response_data, status=status.HTTP_201_CREATED)
