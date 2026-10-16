# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from datetime import timedelta
from typing import TYPE_CHECKING, cast
from core.logging import logEvent
from core.constants import MAX_FILE_SIZE
from core.models import Assignment, AssignmentFile, RubricCategory, RubricComment, TestCase, Submission, Course, SubmissionFile, LearningObjective
from rest_framework import serializers
from rest_framework.request import Request
from core.serializers.assignment import AssignmentSerializer, AssignmentStudentSerializer, AssignmentSerializerWithStatisticsAndSummary, AssignmentStudentSerializerNoStats, AssignmentStudentSerializerWithStats, AssignmentCloneSerializer, AssignmentGenerateTestSerializer, AssignmentGenerateTestResponseSerializer
from core.serializers.assignmentDataSet import AssignmentDataSetSerializer
from core.serializers.submission import AnonymousSubmissionSerializer, SubmissionSerializer, StudentSubmissionSerializer, SubmissionSerializerWithoutFiles, SubmissionWithTestsSerializer
from core.serializers.rubricCategory import RubricCategorySerializer, RubricCategoryStudentSerializer
from core.serializers.rubricComment import RubricCommentSerializer
from core.serializers.quiz import QuizSerializer
from core.serializers.suggestedQuizQuestion import SuggestedQuizQuestionSerializer
from core.serializers.quizSuggestionJob import QuizSuggestionJobSerializer
from core.serializers.submissionHistory import SubmissionHistorySerializer
from core.serializers.comment import CommentSerializer

from core.serializers.testCase import TestCaseStudentSerializer
from core.serializers.testCategory import TestCategorySerializer, TestCategoryStudentSerializer
from core.serializers.learningObjective import LearningObjectiveSerializer
from core.serializers.file import FileValidationSerializerWithoutSubmission, SubmissionFileStudentUploadSerializer


from core.models import User
from django.core.exceptions import ObjectDoesNotExist

from core.models import Section, SubmissionHistory, Comment

from core.views.template import ListProtectedViewSet
from core.services.audit import record_audit_event

from rest_framework import status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from core.pagination import DefaultPagination, LargeObjectsPagination
from drf_spectacular.utils import extend_schema, OpenApiParameter, inline_serializer

from core.serializers.actionResponses import (
  AssignmentQueueLengthResponseSerializer,
  AssignmentRubricResponseSerializer,
  AssignmentStudentTestsResponseSerializer,
  BeforeStudentUploadResponseSerializer,
  AssignmentDownloadResponseSerializer,
  AssignmentStudentUploadGetResponseSerializer,
  AssignmentAnalyticsResponseSerializer,
)
from core.services.assignment_analytics import (
  get_grade_distribution,
  get_grader_workload,
  get_grading_timeline,
  get_test_results_summary,
  get_rubric_usage,
  get_score_by_category,
  get_grader_consistency,
  get_submission_attempts,
  get_time_to_grade,
  get_late_submission_stats,
  get_feedback_depth,
)


from core.permissions.permissions import AssignmentPermissions, RubricCommentPermissions
from core.permissions.helpers import returnNotAuthorized, returnForbidden, returnNotFound, returnInvalid
from core.permissions.helpers import isAuthenticated
from core.permissions.helpers import isStudent, isGrader, isCourseAdmin, isCourseMember, isCourseStaff, isSuperGrader, canViewUnanonymizedSubmissions
from core.permissions.helpers import assignmentFeedbackOpen
from core.permissions.capabilities import compute_assignment_capabilities, CAPABILITY_DESCRIPTIONS, Capability, require_capability
from core.serializers.actionResponses import CapabilitiesResponseSerializer

from django.utils import timezone

from django.db.models import Count, Q, Max, Min, Avg, Value, DecimalField, FloatField, Prefetch, FETCH_PEERS
from django.db.models.functions import Coalesce

from core.utils import copy_assignment
from core.handlers.late_submission_handler import LateSubmissionHandler
import io
import zipfile
import base64
from core.emails import StudentUploadReceiptEmail

import logging
logger = logging.getLogger(__name__)

def encoded_zip(files: list[AssignmentFile], datasets: list | None = None) -> str:
  """
  Create zip from files in memory. `datasets` (AssignmentDataSet rows, already filtered
  to what the requesting user may see) are added under a `data/` prefix so their names
  never collide with assignment file names.
  """
  zip_buffer = io.BytesIO()

  with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
    for file in files:
      data = file.data

      # Data URI content ("data:<mime>;base64,...") is binary — decode before adding to zip.
      if data.startswith('data:'):
          try:
              _header, encoded = data.split(',', 1)
              data = base64.b64decode(encoded)
          except Exception:
              pass

      zip_file.writestr(file.name, data)

    # Datasets use a real FileField (binary storage), not the text `data` field above.
    for dataset in (datasets or []):
      if not dataset.file:
        continue
      try:
        with dataset.file.open('rb') as f:
          filename = dataset.file.name.split('/')[-1]
          zip_file.writestr(f'data/{filename}', f.read())
      except Exception:
        logger.warning(f"[AssignmentDownload] Failed to zip dataset {dataset.id}")

  return base64.b64encode(zip_buffer.getvalue()).decode()


class AssignmentViewSet(ListProtectedViewSet):
  """
  list:
  Return a list of all the assignments.

  create:
  Create a new assignment instance.

  retrieve:
  Return the given assignment.

  update:
  Update an assignment.

  partial_update:
  Update an assignment.

  delete:
  Delete an assignment
  """
  if TYPE_CHECKING:
    request: Request
  
  queryset = Assignment.objects.all()
  permission_classes = (IsAuthenticated, AssignmentPermissions)
  serializer_class = AssignmentSerializer

  def get_object(self) -> Assignment:
    return super().get_object()

  # return an assignment serializer with statistics if the user is allowed to see them
  def get_serializer_class(self):
    # During schema generation, return default serializer
    if getattr(self, 'swagger_fake_view', False):
        return AssignmentSerializer
        
    if self.action == 'retrieve':
      user = self.request.user
      assignment = self.get_object()
      course = assignment.course

      if isCourseAdmin(user, course):
        return AssignmentSerializerWithStatisticsAndSummary

      # if a grader who isn't an admin, return without statistics
      elif isGrader(user, course):
        return AssignmentSerializer

      # user is a student only
      else:
        # Graded reveal: the stats-bearing serializers unlock once the feedback axis
        # opens for this student (live/released, or per_student with a finalized sub)
        if not assignmentFeedbackOpen(assignment, user):
          return AssignmentStudentSerializer
        elif (not course.showStudentsStatistics):
          return AssignmentStudentSerializerNoStats
        else:
          return AssignmentStudentSerializerWithStats
    else:
      return AssignmentSerializer

  def get_queryset(self):
    queryset = Assignment.objects.all()
    if self.action == 'retrieve':
      queryset = queryset.annotate(
          submissions_count_anno=Count('submissions', distinct=True),
          submissions_finalized_count_anno=Count('submissions', filter=Q(submissions__isFinalized=True), distinct=True),
          submissions_inprogress_count_anno=Count('submissions', filter=Q(submissions__isFinalized=False) & ~Q(submissions__grader=None), distinct=True),
          submissions_unclaimed_count_anno=Count('submissions', filter=Q(submissions__grader=None), distinct=True),
          # No Coalesce to 0: an assignment with nothing finalized must read
          # null, not a real-looking 0-point average. The serializer treats a
          # null annotation as "recompute", which also returns null here.
          stats_max_anno=Max('submissions__grade', filter=Q(submissions__isFinalized=True)),
          stats_min_anno=Min('submissions__grade', filter=Q(submissions__isFinalized=True)),
          stats_mean_anno=Avg('submissions__grade', filter=Q(submissions__isFinalized=True))
      )
    return queryset

  def perform_update(self, serializer):
    # Audit lifecycle transitions (mirrors QuizViewSet.perform_update's publish events).
    old_state = serializer.instance.state
    old_feedback = serializer.instance.feedbackStatus
    assignment = serializer.save()
    if assignment.state != old_state:
      record_audit_event(
          course=assignment.course,
          event_type='assignment_state_changed',
          user=self.request.user,
          assignment=assignment,
          meta={'from': old_state, 'to': assignment.state},
      )
    if assignment.feedbackStatus != old_feedback:
      record_audit_event(
          course=assignment.course,
          event_type='assignment_feedback_changed',
          user=self.request.user,
          assignment=assignment,
          meta={'from': old_feedback, 'to': assignment.feedbackStatus},
      )

  # Extra functions
  #####################################################################################

  @extend_schema(
      responses=CapabilitiesResponseSerializer,
      parameters=[
          OpenApiParameter(
              name='descriptions', type=bool,
              location=OpenApiParameter.QUERY, required=False,
              description='Include human-readable descriptions for each capability.',
          ),
      ],
  )
  @action(detail=True, methods=['GET'])
  def capabilities(self, request, pk=None):
    """Return the requesting user's capabilities for this assignment."""
    user = request.user
    if not isAuthenticated(user):
      return returnNotAuthorized()

    assignment = self.get_object()
    course = assignment.course

    if not isCourseMember(user, course):
      return returnForbidden()

    caps = compute_assignment_capabilities(user, assignment)

    include_descriptions = request.query_params.get('descriptions', '').lower() in ('true', '1')
    if include_descriptions:
      descriptions = {
          cap: CAPABILITY_DESCRIPTIONS.get(Capability(cap), '')
          for cap in caps
      }
      return Response({'capabilities': caps, 'descriptions': descriptions})

    return Response({'capabilitiesMap': caps})

  @extend_schema(responses=CommentSerializer(many=True))
  @action(detail=True)
  def comments(self, request, pk=None):
    """
    Grab all custom comments applied to submissions for this assignment, possibly filtered by
    author

    FIXME: we should make this endpoint more generic by optionally filtering based on .rubricComment,
    instead of filtering out comments s.t. rubricComment = None by default.
    """
    user = self.request.user
    assignment = self.get_object()
    course = assignment.course

    # are we filtering by author?
    author = self.request.query_params.get('author', None)

    if author:
      if (cast(User, self.request.user).email == author) or isCourseAdmin(user, course) or isSuperGrader(user, course):
        comments = Comment.objects.filter(file__submission__assignment=assignment,
                                          author__email=author, rubricComment=None)
      else:
        return returnForbidden()
    else:
      if isCourseAdmin(user, course) or isSuperGrader(user, course):
        comments = Comment.objects.filter(file__submission__assignment=assignment, rubricComment=None)
      else:
        return returnForbidden()

    serial = CommentSerializer(comments, many=True, context={'request': request})

    return Response(serial.data)

  @extend_schema(responses=AssignmentQueueLengthResponseSerializer)
  @action(detail=True)
  def queueLength(self, request, pk=None):
    """
    Show the rubric for this assignment.
    """
    user = self.request.user
    assignment = self.get_object()
    course = assignment.course

    require_capability(user, 'view_queue', assignment)

    section = self.request.query_params.get('section', None)  # noqa: F841
    
    # Base query: submissions for this assignment that are unassigned
    # We also filter for students enrolled in the course
    if isCourseAdmin(user, course):
      submissions = assignment.submissions.filter(
          grader=None, students__in=course.students.all())
    else:
      submissions = assignment.submissions.filter(
          ~Q(students__in=[user]), grader=None, students__in=course.students.all())

    # Apply section filter if provided
    # Note: query param 'section' can be a list if multiple sections are selected?
    # The frontend code sends params.append('section', id) multiple times for multiple sections
    # Django's request.query_params.getlist('section') should be used if we support multiple
    # but the frontend code: 
    # params.append('section', section.id.toString());
    # implies we might get multiple sections.
    # However, let's look at how drawUnassigned handles it. 
    # drawUnassigned: section = self.request.query_params.get('section', None)
    # it only handles ONE section.
    # Let's check the frontend again...
    
    # Frontend:
    # if (sections && sections.length > 0) {
    #   sections.forEach((section) => {
    #     params.append('section', section.id.toString());
    #   });
    # }
    
    # So potentially multiple 'section' keys.
    # But drawUnassigned only gets one: section = self.request.query_params.get('section', None)
    # The frontend claimSubmission logic calls fetchSubmission (which calls drawUnassigned) in a loop for each section.
    # BUT fetchQueueLength sends ALL sections at once.
    
    # So for queueLength, we should support multiple sections.
    
    sections = self.request.query_params.getlist('section')
    
    if sections:
        # Filter submissions that belong to any of the provided sections
        # Logic: submission -> students -> section
        # We want submissions where at least one student is in one of the provided sections
        
        # This can be complex if we need to verify section existence / course membership
        # effectively:
        # valid_sections = Section.objects.filter(id__in=sections, course=course)
        # submissions = submissions.filter(students__section__in=valid_sections).distinct()
        
        valid_sections = Section.objects.filter(id__in=sections, course=course)
        if valid_sections.exists():
            submissions = submissions.filter(students__student_sections__in=valid_sections).distinct()
    
    toRet = {
        'id': assignment.id,
        'unclaimed': submissions.count(),
        'finalized': assignment.submissions.filter(grader=user, isFinalized=True).count(),
        'unfinalized': assignment.submissions.filter(grader=user, isFinalized=False).count(),
    }

    return Response(toRet)

  # Returns the serialized rubric for this assignment
  @extend_schema(responses=AssignmentRubricResponseSerializer)
  @action(detail=True, permission_classes=((IsAuthenticated, RubricCommentPermissions)), methods=['GET'])
  def rubric(self, request, pk=None):
    """
    Show the rubric for this assignment.
    """
    user = self.request.user
    assignment = self.get_object()
    course = assignment.course

    categories = RubricCategory.objects.filter(assignment=assignment)

    if isCourseStaff(user, course):
      categorySerializer = RubricCategorySerializer(categories, many=True, context={'request': request})
    else:
      categorySerializer = RubricCategoryStudentSerializer(categories, many=True, context={'request': request})

    comments = RubricComment.objects.filter(category__assignment=assignment)
    commentSerializer = RubricCommentSerializer(comments, many=True, context={'request': request})

    toRet = {
        'id': assignment.id,
        'rubricCategories': categorySerializer.data,
        'rubricComments': commentSerializer.data,
    }

    return Response(toRet)

  @extend_schema(responses=QuizSerializer(many=True))
  @action(detail=True, methods=['GET'], permission_classes=[IsAuthenticated])
  def quizzes(self, request, pk=None):
    """List the quizzes attached to this assignment (staff only)."""
    assignment = self.get_object()
    if not isCourseStaff(request.user, assignment.course):
      return returnForbidden()
    quizzes = assignment.quizzes.all()
    return Response(QuizSerializer(quizzes, many=True, context={'request': request}).data)

  @extend_schema(responses=SuggestedQuizQuestionSerializer(many=True))
  @action(detail=True, methods=['GET'], permission_classes=[IsAuthenticated])
  def suggestedQuizQuestions(self, request, pk=None):
    """List pending AI quiz-question suggestions for this assignment (staff only)."""
    assignment = self.get_object()
    if not isCourseStaff(request.user, assignment.course):
      return returnForbidden()
    suggestions = assignment.suggested_quiz_questions.filter(status='pending')
    return Response(SuggestedQuizQuestionSerializer(suggestions, many=True, context={'request': request}).data)

  @extend_schema(
      request=inline_serializer('GenerateQuizQuestionsRequest', fields={
          'num_questions': serializers.IntegerField(required=False),
          'question_types': serializers.ListField(child=serializers.CharField(), required=False),
          'instructions': serializers.CharField(required=False),
      }),
      responses=QuizSuggestionJobSerializer,
      description="Enqueue AI generation of suggested quiz questions for this assignment. "
                  "Instructors review the suggestions and accept the good ones. Returns the "
                  "generation job to poll via quizSuggestionJobs/{id}/ for status and errors. "
                  "Returns 403 when the course has the quiz_generation AI feature turned off.",
  )
  @action(detail=True, methods=['POST'], permission_classes=[IsAuthenticated])
  def generateQuizQuestions(self, request, pk=None):
    """Generate AI quiz-question suggestions from this assignment and course material."""
    assignment = self.get_object()
    if not isCourseStaff(request.user, assignment.course):
      return returnForbidden()
    require_capability(request.user, Capability.GENERATE_AI_QUIZ_QUESTIONS, assignment.course)
    from core.models import QuizSuggestionJob
    from core.tasks import generate_quiz_question_suggestions
    job = QuizSuggestionJob.objects.create(
        course=assignment.course, assignment=assignment, requestedBy=request.user)
    task = generate_quiz_question_suggestions.delay(
        requested_by_id=request.user.id,
        assignment_id=assignment.id,
        num_questions=request.data.get('num_questions', 5),
        question_types=request.data.get('question_types'),
        instructions=request.data.get('instructions', '') or '',
        job_id=job.id,
    )
    # Queryset update (not job.save): under eager Celery the task may have already
    # advanced the DB row, and BaseModel.save would clobber it from a stale instance.
    QuizSuggestionJob.objects.filter(pk=job.pk).update(taskId=task.id)
    job.refresh_from_db()
    return Response(QuizSuggestionJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)

  @extend_schema(responses=SubmissionSerializer(many=True))
  @action(detail=True, methods=['GET'])
  def drawUnassigned(self, request, pk=None):
    """
    Get the next unassigned submission for this submission.
    """
    user = request.user
    assignment = self.get_object()
    course = assignment.course
    amount = self.request.query_params.get('amount', 1)

    if not isGrader(user, course):
      return returnForbidden()

    section = self.request.query_params.get('section', None)

    # Use system ordering to pull random unassigned submission
    # The students__in filter allows submissions to be claimed that *include* inactive students.
    # Examples: if submission.students = [active@uni.edu, inactive@uni.edu], the submission can be claimed
    #           if submission.students = [inactive@uni.edu], the submission can't be claimed
    #
    # To exclude submissions belonging to inactive students, regardless of the other students associated
    # with the submission, use .exclude(students__in=course.inactivestudents.all())
    if isCourseAdmin(user, course):
      submissions = assignment.submissions.filter(
          grader=None, students__in=course.students.all()).order_by('queueOrderKey')
    else:
      submissions = assignment.submissions.filter(
          ~Q(students__in=[user]), grader=None, students__in=course.students.all()).order_by('queueOrderKey')

    if section is not None:
      try:
        section = Section.objects.get(name=section, course=course)
        submissions = submissions.filter(students__in=section.students.all())
      except ObjectDoesNotExist:
        return returnNotFound(message="No such section")

    _submission = None

    if len(submissions) <= 0:
      return Response(status=status.HTTP_204_NO_CONTENT)
     
    claimed_submissions = submissions[:int(amount)]
    
    for s in claimed_submissions:
      # Assign submission to grader
      # Doing this in this call is important, since it prevents two users from drawing the
      # save unassigned submission and subsequently trying to claim it
      s.grader = user
      s.save()
    


    serializerClass = SubmissionSerializer
    if assignment.anonymousGrading and not canViewUnanonymizedSubmissions(user, course):
      serializerClass = AnonymousSubmissionSerializer

    
    data = []
    # serializer = serializerClass(claimed_submissions, context={'request': request})
    
    for s in claimed_submissions:
      serializer = serializerClass(s, context={'request': request})
      data.append(serializer.data)

    return Response(data)

# Optional arguments: username, grader
# If neither specified, returns full list of submissions for this assignment
  @extend_schema(
    parameters=[
      OpenApiParameter(name="grader", required=False, type=str, location=OpenApiParameter.QUERY,
                       description="Filter submissions by grader email."),
      OpenApiParameter(name="student", required=False, type=str, location=OpenApiParameter.QUERY,
                       description="Filter submissions by student email."),
      OpenApiParameter(name="compact", required=False, type=str, location=OpenApiParameter.QUERY,
                       description="If set to '1', return submissions without nested file data."),
    ],
    responses=SubmissionSerializer(many=True),
  )
  @action(detail=True, pagination_class=DefaultPagination)
  def submissions(self, request, pk=None):
    """
    Return a (optionally filtered) list of submissions whose parent is the requested assignment.
    """

    # Gather variables
    user = request.user
    assignment = self.get_object()  # => this endpoint has permissions at least as strict
    course = assignment.course
    student = self.request.query_params.get('student', None)

    grader = self.request.query_params.get('grader', None)
    shouldReturnCompact = self.request.query_params.get('compact', None)
    # select_related the grader (FK) and assignment->course (the Submission.course property walks
    # assignment.course, otherwise 2 FK queries per submission). Every serializer variant returns
    # tests; files (+ per-file comments/edit) are prefetched only on the branches that serialize
    # them. FETCH_PEERS batches any forward relation the serializers touch beyond these.
    submissions = assignment.submissions.all().select_related('grader', 'assignment__course').prefetch_related('students', 'tests').fetch_mode(FETCH_PEERS)
    shouldPaginate = self.request.query_params.get('page', None)

    #############################################################################################
    # Permissions assessment
    #############################################################################################
    isThisGrader = isGrader(user, course) and user.email == grader
    isThisStudent = isStudent(user, course) and user.email == student
    isCourseAdminCached = isCourseAdmin(user, course)

    # If you want to filter by grader, you must be that grader, courseadmin, or superGrader
    if grader is not None:
      if not isCourseAdminCached and not isSuperGrader(user, course) and not isThisGrader:
        return returnForbidden()

    # If you want all of the submissions, you must be a courseAdmin or superGrader
    if student is None and grader is None:
      if not isCourseAdminCached and not isSuperGrader(user, course):
        return returnForbidden()

    # If you want to filter by student, you must be a grader, courseAdmin, or that student
    if student is not None:
      if not isThisStudent and not isGrader(user, course) and not isCourseAdminCached:
        return returnForbidden()

    # If you want to use this endpoint and you are a student only, then you must wait until the assignment
    # is released, student upload is allowed, or live feedback mode is enabled
    isOnlyStudent = isThisStudent and not isThisGrader and not isCourseAdminCached
    # Students may list their own submissions from published onward (or in live-feedback
    # mode) — before that there is nothing of theirs to see.
    if isOnlyStudent and assignment.state not in ('published', 'closed') and assignment.feedbackStatus != 'live':
      return returnForbidden()

    # The student serializer renders files (StudentSubmissionSerializer.get_files) and each file's
    # `edit` reverse-OneToOne (SubmissionFileWithoutCommentsSerializer.get_edit); prefetch files with
    # `edit` select_related so the dashboard's per-assignment call doesn't N+1 over files or edits.
    if isOnlyStudent:
      submissions = submissions.prefetch_related(
          Prefetch('files', queryset=SubmissionFile.objects.select_related('edit'))
      )

    #############################################################################################

    # At this point, we can assume the client has the permissions necessary to perform the requested
    # action.

    # Retrieve student
    studentParam = None
    if student is not None:
      try:
        studentParam = User.objects.filter(Q(username=student) | Q(email=student)).first()
        if studentParam is None:
            raise User.DoesNotExist()
      except User.DoesNotExist:
        if isCourseAdmin(user, course):
          return returnNotFound(message="The user does not exist")
        else:
          return returnForbidden()

    # Retrieve grader
    graderParam = None
    if grader is not None:
      try:
        graderParam = User.objects.filter(Q(username=grader) | Q(email=grader)).first()
        if graderParam is None:
            raise User.DoesNotExist()
      except User.DoesNotExist:
        if isCourseAdmin(user, course):
          return returnNotFound(message="The user does not exist")
        else:
          return returnForbidden()

    # Perform filtering
    filteredSubs = None
    if studentParam is not None and graderParam is not None:
      filteredSubs = submissions.filter(students__in=[studentParam],
                                        grader=graderParam)
    elif studentParam is not None:
      filteredSubs = submissions.filter(students__in=[studentParam])
    elif graderParam is not None:
      filteredSubs = submissions.filter(grader=graderParam)
    else:
      filteredSubs = submissions

    # Client is only a student
    if isOnlyStudent:
      if len(filteredSubs) == 0:
        return Response([])

      subCandidate = filteredSubs[0]  # noqa: F841


      # StudentSubmissionSerializer handles all cases:
      # - Masks grade while the feedback axis is closed (or hideGrades)
      # - Returns files without comments while the feedback axis is closed
      # - Preserves real isFinalized status so frontend can show submission correctly
      serializer = StudentSubmissionSerializer(filteredSubs, many=True, context={'request': request})

    # Client has privilege that exceeds a student's
    else:
      if assignment.anonymousGrading and not canViewUnanonymizedSubmissions(user, course):
        filteredSubs = filteredSubs.prefetch_related('files', 'files__comments', 'files__edit')
        serializer = AnonymousSubmissionSerializer(filteredSubs, many=True, context={'request': request})
      else:
        if shouldReturnCompact is not None and shouldReturnCompact != '0':
          if shouldPaginate:
            page = self.paginate_queryset(filteredSubs)
            if page is not None:
              serializer = SubmissionSerializerWithoutFiles(page, many=True)
              return self.get_paginated_response(serializer.data)
          serializer = SubmissionSerializerWithoutFiles(filteredSubs, many=True, context={'request': request})
        else:
          filteredSubs = filteredSubs.prefetch_related('files', 'files__comments', 'files__edit')
          serializer = SubmissionSerializer(filteredSubs, many=True, context={'request': request})

    return Response(serializer.data)

  @extend_schema(responses=SubmissionHistorySerializer(many=True))
  @action(detail=True, methods=['GET'], pagination_class=DefaultPagination)
  def submissionHistories(self, request: Request, pk=None):
    user = request.user
    assignment = self.get_object()  # => this endpoint has permissions at least as strict
    course = assignment.course
    shouldPaginate = self.request.query_params.get('page', None)


    # If not course admin or supergrader, return forbidden
    if not isCourseAdmin(user, course) and not isSuperGrader(user, course):
      return returnForbidden()
    submissionHistories = SubmissionHistory.objects.filter(submission__assignment=assignment)

    if shouldPaginate:
      page = self.paginate_queryset(submissionHistories)
      if page is not None:
        serializer = SubmissionHistorySerializer(page, many=True)
        return self.get_paginated_response(serializer.data)
    serializer = SubmissionHistorySerializer(submissionHistories, many=True, context={'request:': request})
    return Response(serializer.data)

  @extend_schema(responses=AssignmentStudentTestsResponseSerializer)
  @action(detail=True, methods=["GET"])
  def studentTests(self, request, pk=None):
    #  Only accessed by students
    user = request.user
    assignment = self.get_object()

    if not isAuthenticated(user):
      return returnNotAuthorized()

    if not isCourseMember(user, assignment.course):
      return returnForbidden()

    # If user is the course admin or the assignment is in live feedback mode return all test cases
    # We need to return all tests for users that are course admin for the "See as student" view
    if isCourseAdmin(user, assignment.course) or assignment.feedbackStatus == 'live':
      test_cases = TestCase.objects.filter(testCategory__assignment=assignment)
    # If assignment is has been released and this endpoint has been called, check if the student's submission is finalized
    # If so, return all test cases. Else, return only exposed test cases
    elif assignment.feedbackStatus in ('released', 'per_student'):
      filteredSubs = Submission.objects.filter(assignment=assignment, students__in=[user])
      if len(filteredSubs) > 0 and filteredSubs[0].isFinalized:
        test_cases = TestCase.objects.filter(testCategory__assignment=assignment)
      else:
        test_cases = TestCase.objects.filter(testCategory__assignment=assignment, exposed=True).exclude(type="external")
    # If the assignment is not released or in live feedback mode, return only exposed test cases
    else:
      test_cases = TestCase.objects.filter(testCategory__assignment=assignment, exposed=True).exclude(type="external")

    # Hidden test cases must not be visible to students, but graders and instructors do
    # need to see them (so they can read the description, run them from the code console,
    # and reason about the grade). Filter on isCourseStaff so anyone below grader is excluded.
    if not isCourseStaff(user, assignment.course):
      test_cases = test_cases.exclude(hidden=True)

    test_categories = list(set(map(lambda test_case: test_case.testCategory, test_cases)))  # remove duplicates

    case_serializer = TestCaseStudentSerializer(test_cases, many=True, context={'request': request})
    # Staff get the full category (incl. testScript / testFiles / resources); students must
    # not — that would hand them the entire test harness. isCourseStaff covers graders,
    # admins, and the admin "see as student" / live-feedback paths above.
    CategorySerializer = (
        TestCategorySerializer if isCourseStaff(user, assignment.course) else TestCategoryStudentSerializer
    )
    category_serializer = CategorySerializer(test_categories, many=True, context={'request': request})
    return Response({
        'id': assignment.id,
        'testCases': case_serializer.data,
        'testCategories': category_serializer.data
    })

  @extend_schema(responses=LearningObjectiveSerializer(many=True))
  @action(detail=True, methods=["GET"])
  def learningObjectives(self, request, pk=None):
    """Return all learning objectives for this assignment."""
    user = request.user
    assignment = self.get_object()

    if not isAuthenticated(user):
      return returnNotAuthorized()

    if not isCourseStaff(user, assignment.course):
      return returnForbidden()

    objectives = LearningObjective.objects.filter(assignment=assignment)
    serializer = LearningObjectiveSerializer(objectives, many=True, context={'request': request})
    return Response(serializer.data)

  @extend_schema(responses=BeforeStudentUploadResponseSerializer)
  @action(detail=True, methods=["GET"])
  def beforeStudentUpload(self, request: Request, pk=None):
    """
    Get submission upload information

    return {
      "daysLate": 3,
      "pointsOff": 0,
      "lateDayCreditsAvailable": 2,
      "lateDayCreditsToUse": 2,
      "adjustedDaysLate": 0
    }
    """
    user = self.request.user
    # get_object() runs AssignmentPermissions (lifecycle state + hideFrom); the
    # upload_submission capability below adds the published/allowStudentUpload gate.
    assignment = self.get_object()
    course = assignment.course

    if not isAuthenticated(user):
      return returnNotAuthorized()

    require_capability(user, 'upload_submission', assignment)

    submission = Submission(assignment=assignment, dateUploaded=timezone.now())

    # FIXME: This will not check for partner submissions
    # We can't just have the POST body contain partner ids, for the same information
    # leak risks as allowing students to add partners without approval.
    handler = LateSubmissionHandler(submission, [user])

    if not handler.is_late():
      return Response({
          "daysLate": 0,
          "pointsOff": 0
      }, status=status.HTTP_200_OK)

    if course.lateDayCreditsAllowable is None:
      return Response({
          "daysLate": handler.real_days_late,
          "pointsOff": handler.get_points()
      }, status=status.HTTP_200_OK)
    else:
      return Response({
          "daysLate": handler.real_days_late,
          "pointsOff": handler.get_points(),
          "lateDayCreditsAvailable": handler.late_day_credits_available(),
          "lateDayCreditsToUse": handler.late_day_credits_to_use,
          "adjustedDaysLate": handler.calculated_days_late()
      }, status=status.HTTP_200_OK)

  @extend_schema(
      parameters=[
          OpenApiParameter(
              name="includeDatasets", required=False, type=bool,
              location=OpenApiParameter.QUERY,
              description=("Bundle the assignment's datasets into the zip (default true). "
                           "Pass false for callers that mount datasets separately — e.g. "
                           "JupyterHub — to get the assignment files alone; this also skips "
                           "the per-student variant auto-assignment side effect."),
          ),
      ],
      responses=AssignmentDownloadResponseSerializer,
  )
  @action(detail=True, methods=["GET"])
  def download(self, request: Request, pk=None):
    """
    download all files for an assignment files as a zip
    """
    user = cast(User, request.user)
    assignment = self.get_object()
    _course = assignment.course

    require_capability(user, 'download_assignment_files', assignment)


    files = assignment.files.all()

    # Bundle the assignment's shared (non-hidden) datasets plus — for a per-student
    # variant pool — only the requesting student's own assigned variant (auto-assigned
    # here on first access), never a classmate's. Staff get the shared set only; they can
    # pull any specific variant via the per-dataset download endpoint.
    # Callers that mount datasets themselves (JupyterHub) pass includeDatasets=false: the
    # zip carries only the assignment files, and no variant is auto-assigned as a side effect.
    include_datasets = request.query_params.get(
        'includeDatasets', 'true').lower() in ('true', '1', 'yes')
    datasets = []
    if include_datasets:
      from core.models import AssignmentDataSet
      datasets = list(AssignmentDataSet.objects.filter(
          assignment=assignment, hidden=False, is_student_variant=False))
      if not isCourseStaff(user, assignment.course):
        from core.services.dataset_assignment import get_or_assign
        variant = get_or_assign(assignment, user)
        if variant is not None and not variant.hidden:
          datasets.append(variant)

    if len(files) == 0 and not datasets:
      return Response("No files to download", status=status.HTTP_204_NO_CONTENT)

    files_to_zip = list(files)

    encoded = encoded_zip(files_to_zip, datasets)
    return Response({
        "zip": encoded,
        "filename": f"assignment{assignment.id}_files.zip"
    })
  
  # Upload assignment
  @extend_schema(methods=["GET"], responses=AssignmentStudentUploadGetResponseSerializer)
  @extend_schema(methods=["POST", "PATCH"], responses=StudentSubmissionSerializer)
  @action(detail=True, methods=["POST", "PATCH", "GET"])
  def studentUpload(self, request, pk=None):
    """
    Upload of submission to an assignment


    TODO: add file limits to 10mb
    """
    user = self.request.user
    # get_object() runs AssignmentPermissions (lifecycle state + hideFrom); the
    # upload_submission capability below adds the published/allowStudentUpload gate.
    assignment = self.get_object()
    course = assignment.course

    if not isAuthenticated(user):
      return returnNotAuthorized()

    require_capability(user, 'upload_submission', assignment)

    if request.method == "PATCH" or request.method == "POST":
      if 'files' not in request.data or len(request.data['files']) == 0:
        raise serializers.ValidationError("No files provided")


      # Began late submission check — boundary comes from submission_deadline() so this
      # can never disagree with the derived 'closed' state (Assignment.effective_state).
      if assignment.uploadDueDate and timezone.now() > assignment.uploadDueDate:
        if not assignment.allowLateUploads:
          raise serializers.ValidationError("Late submissions are not allowed for this assignment.")

        if timezone.now() > assignment.submission_deadline():
          raise serializers.ValidationError("The maximum late submission period has passed for this assignment.")

      # Ended late submission check
      
      
      

      # Check to make sure the files are valid before we create the submission
      uploaded_filenames = set()

      for f in request.data['files']:
        serializer = FileValidationSerializerWithoutSubmission(data=f)

        try:
          serializer.is_valid(raise_exception=True)
          
          # Check file size (10MB limit)
          # 'data' field is the string content, but for size we might want bytes.
          # Assuming 'data' is text or base64? The model says "should be utf-8 encoded text".
          # A strict 10MB limit on text length is a fair approximation for now.
          if len(f.get('data', '')) > MAX_FILE_SIZE:
             raise ValidationError(f"File '{f['name']}' exceeds the 10MB size limit.")

          uploaded_filenames.add(f['name'])

        except ValidationError as e:
          if isinstance(e.detail, dict):
            e.detail['file'] = f['name']
          else:
             # If it's a list or string, wrap it
             e = ValidationError({'file': f['name'], 'error': e.detail})
          raise e

      # Check for required files
      required_files = assignment.files.filter(required=True)
      missing_files = []
      for req_file in required_files:
        if req_file.name not in uploaded_filenames:
          missing_files.append(req_file.name)
      
      if missing_files:
        raise serializers.ValidationError(f"Missing required files: {', '.join(missing_files)}")



      otherSubs = Submission.objects.filter(assignment=pk, students__in=[user])
      if len(otherSubs) > 1:
        raise serializers.ValidationError("This student has multiple submissions for this assignment")

      
      if len(otherSubs) == 1:
        submission = otherSubs[0]
      else:
        submission = Submission.objects.create(assignment=assignment)
        submission.students.add(cast(User, user))
        submission.save()
        
      # Don't allow submission if the submission is finalized, unless feedback is live
      if submission.isFinalized and assignment.feedbackStatus != 'live':
        raise serializers.ValidationError("Cannot edit this submission, grading has started.")

      oldFiles = submission.files.all()
      print(oldFiles)
      if (request.method == "POST"):
        # Only if the request is a post do we replace all the submissions
        for f in oldFiles:
          print(f)
          f.delete()
          
          
      for f in request.data['files']:
        # Create new submission file
        SubmissionFile.objects.create(name=f['name'], data=f['data'], submission=submission, extension=f[
                                   'extension'], path=f['path'] if f['path'] else None)

      # Update submission date once files have been uploaded, triggers auto-execution celery task
      submission.dateUploaded = timezone.now()

      if assignment.feedbackStatus == 'live':
        submission.isFinalized = False

      submission.save()

      # Record audit event for direct upload submission
      record_audit_event(
          course=course,
          event_type='submission_attempt',
          user=user,
          assignment=assignment,
          submission=submission,
      )

      ###############################################################
      # [Begin] Late Logic
      ###############################################################

      handler = LateSubmissionHandler(submission)
      try:
        handler.handle()
        # Record late day usage if credits were consumed
        if handler.late_day_credits_to_use > 0:
          record_audit_event(
              course=course,
              event_type='late_day_used',
              user=user,
              assignment=assignment,
              submission=submission,
              meta={'credits_used': handler.late_day_credits_to_use},
          )
      except Exception as e:
        logEvent("Late Submission Error",
                 message=f"Error handling late submission: {e} for submission by user {cast(User, user).email}", level=logging.ERROR)


      ###############################################################
      # [End] Late Logic
      ###############################################################

      # Send upload receipt to each student
      if 'sendConfirmationEmail' in request.data and request.data['sendConfirmationEmail']:
        for student in submission.students.all():
          try:
            StudentUploadReceiptEmail(student).send_email(submission)
            # send_email_student_uploaded_submission(student.email, submission)
          except Exception as e:
            logEvent("API Error",
                     message=f"Error emailing student receipt: {e} for submission by user {cast(User, user).email}", level=logging.ERROR)
      

      serializer = StudentSubmissionSerializer(submission, many=False, context={"request": request})
      return Response(serializer.data)

    # If a GET request, pass back the files for the submission
    else:
      otherSubs = Submission.objects.filter(assignment=pk, students__in=[user])
      if len(otherSubs) == 0:
        submission = None
        return Response({"id": -1, "files": []})
      else:
        submission = otherSubs[0]
        # Remove any autogenerated test files from being exposed
        filesToReturn = submission.files.filter(hiddenBeforePublish=False)
        toRet = {
            'id': submission.id, 'files': SubmissionFileStudentUploadSerializer(filesToReturn, many=True).data
        }
        return Response(toRet)

  @extend_schema(responses=SubmissionWithTestsSerializer(many=True))
  @action(detail=True, methods=["GET"], pagination_class=LargeObjectsPagination)
  def submissionTests(self, request, pk=None):
    """
    Gets a paginated list of submission tests for an assignment.
    We use this for performance for large courses to fetch all submission tests.
    Fetching thousands of requests from the client can cause some to fail.
    Returns a list of {id: int, tests: SubmissionTest[]}
    """
    user = request.user
    assignment = self.get_object()
    course = assignment.course

    # Only allow course admins to access this endpoint
    if not isCourseAdmin(user, course):
      return returnForbidden()

    submissions = assignment.submissions.all().prefetch_related('tests')

    page = self.paginate_queryset(submissions)
    if page is not None:
      serializer = SubmissionWithTestsSerializer(page, many=True)
      return self.get_paginated_response(serializer.data)

    serializer = SubmissionWithTestsSerializer(submissions, many=True)
    return Response(serializer.data)


  @extend_schema(responses=AssignmentDataSetSerializer(many=True))
  @action(detail=True, methods=["GET"])
  def datasets(self, request, pk=None):
    """
    Return the datasets for this assignment. Staff see everything; students see shared
    (non-hidden) datasets plus only the variant assigned to them from a per-student pool
    (auto-assigned here on first access).

    GET /api/assignments/{id}/datasets/
    """
    from django.db.models import Q
    from core.models import AssignmentDataSet
    from core.serializers.assignmentDataSet import AssignmentDataSetSerializer

    assignment = self.get_object()
    user = cast(User, request.user)
    if isCourseStaff(user, assignment.course):
      datasets = AssignmentDataSet.objects.filter(assignment=assignment).order_by('name')
    else:
      from core.services.dataset_assignment import get_or_assign
      get_or_assign(assignment, user)
      datasets = AssignmentDataSet.objects.filter(
          assignment=assignment, hidden=False,
      ).exclude(Q(is_student_variant=True) & ~Q(student_assignments__student=user)).order_by('name')
    serializer = AssignmentDataSetSerializer(datasets, many=True, context={'request': request})
    return Response(serializer.data)

  @extend_schema(request=AssignmentGenerateTestSerializer, responses={200: AssignmentGenerateTestResponseSerializer})
  @action(detail=True, methods=["POST"])
  def generateTest(self, request, pk=None):
    """
    Generate an AI-powered test script for a file in this assignment.
    
    Request body:
    - target_filename: str (required) - Name of the file to test (e.g., 'main.py')
    - context_file_id: int (optional) - ID of an AssignmentFile to use as context (Solution/Starter)
    - context_file_name: str (optional) - Name of an AssignmentFile (if ID not provided)
    - language: str (optional) - Target language (python, java, etc.)
    """
    from asgiref.sync import async_to_sync
    from core.services.ai_service import AIService

    
    user = self.request.user
    assignment = self.get_object()
    course = assignment.course
    
    # Check permissions
    require_capability(user, 'generate_ai_test_cases', assignment)
        
    # Check AI configuration
    service = AIService(course, assignment)
    if not service.is_configured or not service.is_feature_enabled('test_generation'):
        return Response(
            {'error': 'AI is not configured/enabled for this course.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    serializer = AssignmentGenerateTestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    params = serializer.validated_data

    target_filename = params.get('targetFilename') or params.get('target_filename')
    context_file_id = params.get('contextFileId') or params.get('context_file_id')
    context_file_name = params.get('contextFileName') or params.get('context_file_name')
    # Default language logic is handled by serializer default, but check both keys
    language = params.get('language', 'python')
    rubric_text = params.get('rubricText') or params.get('rubric_text', '')
        
    # Fetch context file
    context_content = ""
    context_name = "Assignment Context"
    
    if context_file_id:
        try:
            af = AssignmentFile.objects.get(id=context_file_id, assignment=assignment)
            context_content = af.data
            context_name = af.name
        except AssignmentFile.DoesNotExist:
             return Response({'error': 'Context file not found'}, status=status.HTTP_404_NOT_FOUND)
    elif context_file_name:
         af = AssignmentFile.objects.filter(assignment=assignment, name=context_file_name).first()
         if af:
             context_content = af.data
             context_name = af.name
             
    # Notebook Language Detection
    #If target is a notebook, we need to know the kernel language to provide correct examples
    # Target Code Extraction & Language Detection
    target_code = None
    
    if target_filename:
        try:
            # Try to find the target file (Solution/Starter)
            target_file = AssignmentFile.objects.filter(assignment=assignment, name=target_filename).first()
            
            if target_file and target_file.data:
                if target_filename.endswith('.ipynb'):
                     # Notebook: Extract code from cells
                     try:
                        import json
                        nb_data = json.loads(target_file.data)
                        
                        # Detect language from kernel
                        kernelspec = nb_data.get('metadata', {}).get('kernelspec', {})
                        kernel_lang = kernelspec.get('language', '').lower()
                        if kernel_lang:
                            if 'python' in kernel_lang: language = 'python'
                            elif 'r' == kernel_lang: language = 'r'
                            elif 'javascript' in kernel_lang or 'node' in kernel_lang: language = 'node'
                            elif 'php' in kernel_lang: language = 'php'
                            elif 'ruby' in kernel_lang: language = 'ruby'
                            elif 'c++' in kernel_lang or 'cpp' in kernel_lang: language = 'cpp'
                            else: language = kernel_lang 
                            
                        # Extract code + markdown (markdown included as comments for AI context)
                        def _comment_block(text: str, lang: str) -> str:
                          if text is None:
                            return ""
                          # Normalize comment prefix by language
                          lang_key = (lang or "").lower()
                          if any(k in lang_key for k in ["java", "js", "ts", "c++", "cpp", "c#", "c/"]):
                            prefix = "// "
                          elif any(k in lang_key for k in ["python", "r", "ruby", "bash", "sh", "shell", "php", "node", "javascript", "typescript"]):
                            prefix = "# "
                          else:
                            prefix = "// "
                          lines = text.splitlines() or [""]
                          return "\n".join(f"{prefix}{line}" for line in lines)

                        code_cells = []
                        cells = nb_data.get('cells', [])
                        for cell in cells:
                          cell_type = cell.get('cell_type')
                          source = cell.get('source', '')
                          if isinstance(source, list):
                            source = ''.join(source)

                          if cell_type == 'markdown':
                            if source:
                              code_cells.append(_comment_block(f"[Markdown]\n{source}", language))
                          elif cell_type == 'code':
                            code_cells.append(source)

                        target_code = "\n\n".join([c for c in code_cells if c is not None])
                     except Exception as e:
                        logger.warning(f"Failed to parse notebook: {e}")
                else:
                    # Regular File: Use content directly
                    target_code = target_file.data
                    
        except Exception as e:
            logger.warning(f"Failed to extract target code: {e}")

    try:
        # Generate script
        service.set_request_context(
            user=cast(User, user), request_type='test_generation', instructions=rubric_text,
        )
        result = async_to_sync(service.generate_test_script)(
            context_file_content=context_content,
            context_filename=context_name,
            target_filename=target_filename,
            target_code=target_code or '',
            language=language,
            rubric_text=rubric_text
        )

        # Record AI usage
        service.record_usage(result, cast(User, user), request_type='test_generation')
        
        if result.success:
            return Response({'script': result.text})
        else:
            return Response({'error': result.error}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    except Exception as e:
        logger.error(f"AI Generation failed: {e}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

  @extend_schema(request=AssignmentCloneSerializer, responses=AssignmentSerializer)
  @action(detail=True, methods=["POST"])
  def clone(self, request, pk=None):
    """
    Clone an assignment to a course
    """
    user = self.request.user
    assignment = self.get_object()
    course = assignment.course

    destination_course_id = request.data.get('course', course.id)
    destination_course = Course.objects.get(id=destination_course_id)

    if not isCourseAdmin(user, destination_course):
      return returnForbidden()

    if not isCourseAdmin(user, course):
      return returnForbidden()

    copied_assignment = copy_assignment(assignment, destination_course)

    if copied_assignment is None:
      return returnInvalid()

    # Return the newly created assignment data so frontend can navigate to it
    serializer = AssignmentSerializer(copied_assignment, context={'request': request})
    data = dict(serializer.data)
    # Surface any datasets that failed to copy so the clone isn't silently missing data.
    failed_datasets = getattr(copied_assignment, '_datasets_failed_to_copy', [])
    if failed_datasets:
      data['datasetsFailedToCopy'] = failed_datasets
    return Response(data)

  @extend_schema(
    request=None,
    responses={200: inline_serializer('GenerateDescriptionResponse', fields={
        'aiDescription': serializers.CharField(),
    })},
    description="Generate or regenerate the AI description for this assignment. Course admin only.",
  )
  @action(detail=True, methods=["POST"])
  def generateDescription(self, request, pk=None):
    """Generate an AI description of the assignment for use as AI context."""
    from asgiref.sync import async_to_sync
    from core.services.ai_service import AIService

    user = self.request.user
    assignment = self.get_object()
    course = assignment.course

    require_capability(user, 'edit_assignment', assignment)

    service = AIService(course, assignment)

    if not service.is_configured:
      return Response({'error': 'AI is not configured for this course.'}, status=status.HTTP_400_BAD_REQUEST)

    if not service.is_feature_enabled('assignment_description'):
      return Response({'error': 'Assignment description generation is disabled for this course.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
      service.set_request_context(user=user, request_type='assignment_description')
      result = async_to_sync(service.generate_assignment_description)(assignment)

      if not result.success:
        return Response({'error': result.error}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

      assignment.ai_description = result.text
      assignment.save(update_fields=['ai_description', 'modified'])

      service.record_usage(result, user, request_type='assignment_description')

      return Response({'aiDescription': result.text})

    except Exception as e:
      logger.error(f"AI description generation failed: {e}", exc_info=True)
      return Response({'error': 'An internal error occurred while generating the description.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

  @extend_schema(
    responses=AssignmentAnalyticsResponseSerializer,
    parameters=[
      OpenApiParameter(name='buckets', type=int, location=OpenApiParameter.QUERY, required=False, description='Number of grade distribution buckets (1-100, default 10)'),
    ],
  )
  @action(detail=True, methods=["GET"])
  def analytics(self, request, pk=None):
    """
    Return aggregated analytics for this assignment:
    grade distribution, grader workload, grading timeline, and test results.
    """
    user = self.request.user
    assignment = self.get_object()
    _course = assignment.course

    require_capability(user, 'view_assignment_statistics', assignment)

    try:
      num_buckets = int(request.query_params.get('buckets', 10))
    except (TypeError, ValueError):
      num_buckets = 10

    return Response({
      'gradeDistribution': get_grade_distribution(assignment, num_buckets=num_buckets),
      'graderWorkload': get_grader_workload(assignment),
      'gradingTimeline': get_grading_timeline(assignment),
      'testResults': get_test_results_summary(assignment),
      'rubricUsage': get_rubric_usage(assignment),
      'scoreByCategory': get_score_by_category(assignment),
      'graderConsistency': get_grader_consistency(assignment),
      'submissionAttempts': get_submission_attempts(assignment),
      'timeToGrade': get_time_to_grade(assignment),
      'lateSubmissions': get_late_submission_stats(assignment),
      'feedbackDepth': get_feedback_depth(assignment),
    })
