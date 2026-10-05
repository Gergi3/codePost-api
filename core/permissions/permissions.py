# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Permission classes for codePost API.

This module defines permissions for all major models in the codePost system.
Permissions are organized by domain area for easier navigation and maintenance.

Reference: https://stackoverflow.com/questions/36553197/permission-checks-in-drf-viewsets-are-not-working-right
"""

from core.models import Assignment, AssignmentFile, CourseFile, Quiz, SubmissionFile, SubmissionTest, User
from core.permissions.helpers import (
    hasCourseCreationPrivilege,
    isAuthenticated,
    isCourseAdmin,
    isCourseMember,
    isCourseStaff,
    isGrader,
    isOrganizationMember,
    isQuizGrader,
    isStaffOfSub,
    isStudent,
    isStudentOfSub,
    assignmentFeedbackOpen,
    feedbackOpenForSubmission,
    studentCanSeeAssignment,
)
from core.permissions.template import TemplatePermission
from core.services import seb
from rest_framework import permissions
from rest_framework.authentication import BasicAuthentication, TokenAuthentication
from codepost.settings import logger
from typing import cast

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _is_safe_method(method):
    """Check if HTTP method is safe (GET, HEAD, OPTIONS)."""
    return method in ['GET', 'HEAD', 'OPTIONS']


def _is_write_method(method):
    """Check if HTTP method is a write operation (POST, PUT, PATCH)."""
    return method in ['POST', 'PUT', 'PATCH']


# =============================================================================
# USER & ORGANIZATION PERMISSIONS
# =============================================================================


class OrganizationPermissions(TemplatePermission):
    """
    Permissions for Organization objects.
    
    - Only superusers can create, modify, or view organizations
    - DELETE is disabled to prevent catastrophic cascade effects
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)

        # DELETE is prohibited - use terminal/admin console only
        if request.method == "DELETE":
            return False

        # All other operations require superuser
        if request.method == "GET":
             return user.is_superuser or (user.profile.isOrgStaff and user.profile.organization == obj)

        return user.is_superuser


class UserPermissions(TemplatePermission):
    """
    Permissions for User objects.
    
    - Only superusers can create or modify users
    - Users can view their own profile
    - DELETE is disabled to prevent catastrophic cascade effects
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)

        # DELETE is prohibited - use terminal/admin console only
        if request.method == "DELETE":
            return False

        # GET: superuser or viewing own profile
        if request.method == "GET":
            return user.is_superuser or user == obj

        # POST, PUT, PATCH: superuser only
        return user.is_superuser


class CoursePermissions(TemplatePermission):
    """
    Permissions for Course objects.
    
    - POST: Organization members with course creation privilege
    - GET: Superuser or course members
    - PUT/PATCH: Course admins only
    - DELETE: Prohibited (use terminal/admin console)
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        course = obj

        # DELETE is prohibited - use terminal/admin console only
        if request.method == "DELETE":
            return False

        # GET: superuser or course member
        if request.method == "GET":
            return user.is_superuser or isCourseMember(user, course) or (user.profile.isOrgStaff and user.profile.organization == course.organization)

        # POST: organization member with course creation privilege
        if request.method == "POST":
            return user.is_superuser or (isOrganizationMember(user, course.organization) and hasCourseCreationPrivilege(user))

        # PUT/PATCH: course admin or Org Staff
        if request.method in ["PATCH", "PUT"]:
            return isCourseAdmin(user, course) or (user.profile.isOrgStaff and user.profile.organization == course.organization)

        return False


class BillingPermissions(permissions.BasePermission):
    """
    Permissions for billing operations.
    
    - Authenticated users can access billing actions
    - Course admins can perform billing operations on their courses
    """

    def has_permission(self, request, view):
        if getattr(view, 'action', None) in ['create_checkout_session', 'details', 'request_waiver']:
            return isAuthenticated(request.user)
        return False

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        if getattr(view, 'action', None) in ['create_checkout_session', 'details', 'request_waiver']:
            return isAuthenticated(user) and isCourseAdmin(user, obj)
        return False


# =============================================================================
# COURSE STRUCTURE PERMISSIONS (Sections, Assignments, Rubrics)
# =============================================================================


class SectionPermissions(TemplatePermission):
    """
    Permissions for Section objects.
    
    - POST/PUT/PATCH/DELETE: Course admins only
    - GET: Superuser or course staff
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        course = obj.course

        # GET: superuser or course staff
        if request.method == "GET":
            return user.is_superuser or isCourseStaff(user, course)

        # All write operations: course admin only
        return isCourseAdmin(user, course)


# Student-facing upload actions on AssignmentViewSet. Their POST/PATCH bodies must pass
# the same read gate as GET here — the finer submit gate (published + allowStudentUpload)
# lives in the upload_submission capability, checked in the view.
STUDENT_UPLOAD_ACTIONS = {'studentUpload', 'beforeStudentUpload'}


class AssignmentPermissions(TemplatePermission):
    """
    Permissions for Assignment objects.

    - GET (and the student upload actions): superuser, course staff, or students the
      assignment's lifecycle state allows to see it (see Assignment.state / hideFrom)
    - All other write operations: course admins only
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        course = obj.course

        if request.method == "GET" or getattr(view, 'action', None) in STUDENT_UPLOAD_ACTIONS:
            return (
                user.is_superuser
                or isCourseStaff(user, course)
                or (isStudent(user, course) and studentCanSeeAssignment(user, obj))
            )

        # All other write operations: course admin only
        return isCourseAdmin(user, course)


class RubricCategoryPermissions(TemplatePermission):
    """
    Permissions for RubricCategory objects.
    
    Write permissions depend on collaborativeRubricMode:
    - Collaborative mode: All course staff can modify
    - Standard mode: Only course admins can modify
    
    Read permissions:
    - Superuser or course staff can always view
    - Students can view if assignment is released or in live feedback mode
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        assignment = obj.assignment
        course = assignment.course

        # GET: superuser, staff, or students once feedback is released (or live
        # feedback) — same axis as RubricCommentPermissions
        if request.method == "GET":
            return (
                user.is_superuser
                or isCourseStaff(user, course)
                or (isStudent(user, course) and assignmentFeedbackOpen(assignment, user))
            )

        # Write operations: depends on collaborative mode
        if assignment.collaborativeRubricMode:
            return isCourseStaff(user, course)
        else:
            from core.permissions.helpers import isRubricEditor
            return isCourseAdmin(user, course) or isRubricEditor(user, course)


class RubricCommentPermissions(TemplatePermission):
    """
    Permissions for RubricComment objects.
    
    Similar to RubricCategory, permissions depend on collaborativeRubricMode:
    - Collaborative mode: All course staff can modify
    - Standard mode: Only course admins can modify
    
    Read permissions:
    - Superuser or course staff can always view
    - Students can view if assignment is released or in live feedback mode
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)

        # Handle both Assignment objects and RubricComment objects
        if type(obj) == Assignment:
            assignment = obj
            course = assignment.course
        else:
            assignment = obj.category.assignment
            course = assignment.course

        # GET: superuser, staff, or students (if released/live feedback)
        if request.method == "GET":
            return (
                user.is_superuser
                or isCourseStaff(user, course)
                or (isStudent(user, course) and assignmentFeedbackOpen(assignment, user))
            )

        # Write operations: depends on collaborative mode
        if assignment.collaborativeRubricMode:
            return isCourseStaff(user, course)
        else:
            from core.permissions.helpers import isRubricEditor
            return isCourseAdmin(user, course) or isRubricEditor(user, course)


# =============================================================================
# SUBMISSION & FILE PERMISSIONS
# =============================================================================


class SubmissionPermissions(TemplatePermission):
    """
    Permissions for Submission objects.
    
    - POST/DELETE: Course admins only
    - PUT/PATCH: Course admins or staff assigned to submission
    - GET: Staff of submission, or students (if assignment released/live feedback)
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        course = obj.assignment.course
        _assignment = obj.assignment

        # GET: staff of submission, or students (if released/live feedback). Course staff
        # reviewing AI-generated quiz questions get read-only access — the questions are
        # generated from the submission (SubmissionFile GETs delegate here too).
        if request.method == "GET":
            return (
                isStaffOfSub(user, obj)
                or isStudentOfSub(user, obj)
                or canViewSubmissionForGeneratedReview(user, obj)
            )

        # PUT/PATCH: course admin or staff of submission
        if request.method in ["PUT", "PATCH"]:
            return isCourseAdmin(user, course) or isStaffOfSub(user, obj)

        # POST/DELETE: course admin only
        return isCourseAdmin(user, course)


class FileTemplatePermissions(TemplatePermission):
    """
    Permissions for FileTemplate objects.
    
    - POST/PUT/PATCH/DELETE: Course admins only
    - GET: All course members
    """

    def has_object_permission(self, request, view, obj):
        raise NotImplementedError("FileTemplatePermissions is deprecated. Use specific file permissions instead.")
        


class FilePermissions(TemplatePermission):
    """
    Permissions for File objects.
    
    Inherits permission logic from parent depending on the type of file.
    """

    def has_object_permission(self, request, view, obj):
      logger.debug(f"FilePermissions: Checking permissions for File object ({obj.id}) of type {type(obj)}")
      
      # Handle polymorphic file types - check for child model attributes
      # Django MTI may pass the base File object, so we need to check for the child model relationship
      # Check in order: SubmissionFile, AssignmentFile, CourseFile
      # Note: Must check child model name (lowercase), not foreign key field names
      
      # Check if it's a SubmissionFile
      if isinstance(obj, SubmissionFile) or hasattr(obj, 'submissionfile'):
        try:
          submission_file = obj if isinstance(obj, SubmissionFile) else obj.submissionfile
          logger.debug(f"FilePermissions: Delegating to SubmissionPermissions for submission {submission_file.submission.id}")
          return SubmissionPermissions().has_object_permission(request, view, submission_file.submission)
        except AttributeError as e:
          logger.warning(f"FilePermissions: Failed to access submissionfile for File {obj.id}: {e}")
          return False
      
      # Check if it's an AssignmentFile
      if isinstance(obj, AssignmentFile) or hasattr(obj, 'assignmentfile'):
        try:
          assignment_file = obj if isinstance(obj, AssignmentFile) else obj.assignmentfile
          logger.debug(f"FilePermissions: Delegating to AssignmentPermissions for assignment {assignment_file.assignment.id}")
          return AssignmentPermissions().has_object_permission(request, view, assignment_file.assignment)
        except AttributeError as e:
          logger.warning(f"FilePermissions: Failed to access assignmentfile for File {obj.id}: {e}")
          return False
      
      # Check if it's a CourseFile
      if isinstance(obj, CourseFile) or hasattr(obj, 'coursefile'):
        try:
          course_file = obj if isinstance(obj, CourseFile) else obj.coursefile
          logger.debug(f"FilePermissions: Delegating to CourseFilePermissions for course {course_file.course.id}")
          return CourseFilePermissions().has_object_permission(request, view, course_file)
        except AttributeError as e:
          logger.warning(f"FilePermissions: Failed to access coursefile for File {obj.id}: {e}")
          return False
      
      # If we can't determine the file type, deny access
      logger.error(f"FilePermissions: Could not determine file type for File {obj.id}. No child model found.")
      return False

class CourseFilePermissions(TemplatePermission):
    """
    Permissions for CourseFile objects (receives the CourseFile, not the course).

    - POST/PUT/PATCH/DELETE: Course admins only
    - GET: Superuser or course staff see every file; students only see files an
      instructor has flipped to studentVisible.
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        course_file = obj
        course = course_file.course

        if request.method == "GET":
            if user.is_superuser or isCourseStaff(user, course):
                return True
            return isCourseMember(user, course) and course_file.studentVisible

        # All write operations: course admin only
        return isCourseAdmin(user, course)


class FileExecutionPermissions(TemplatePermission):
    """
    Permissions for FileExecution on Files. 
    
    If the file is a SubmissionFile, the user must be the submitter, or a staff member of the course the submission belongs to.
    If the file is an AssignmentFile, the user must be a staff member of the course the assignment belongs to.
    If the file is a CourseFile, the user must be a staff member of the course the file belongs to.
    
    Note: This is different then the File Permissions because its for executing files, not viewing them. Students can view CourseFiles, but cannot execute them. Students can only exeute their own files, but they can only get a result if the result has been cached by a StaffofSubmission. Execution should check if the current user is a student of the submission as well. If the user is a student of the submission, they should be able to execute the file, but only if the result has been cached by a StaffofSubmission. If the user is a staff of the submission, they should be able to execute the file and get a result.

    """
    
    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        if isinstance(obj, SubmissionFile) or hasattr(obj, 'submissionfile'):
            submission = obj if isinstance(obj, SubmissionFile) else obj.submissionfile.submission
            
            # Staff can always execute
            if isStaffOfSub(user, submission):
                return True
                
            # Students can execute (view cache) if they are on the submission
            if isStudentOfSub(user, submission):
                return True
                
            return False
        elif isinstance(obj, AssignmentFile) or hasattr(obj, 'assignmentfile'):
            assignment = obj if isinstance(obj, AssignmentFile) else obj.assignmentfile.assignment
            return isCourseStaff(user, assignment.course)
        elif isinstance(obj, CourseFile) or hasattr(obj, 'coursefile'):
            course = obj if isinstance(obj, CourseFile) else obj.coursefile.course
            return isCourseStaff(user, course)
        return False
    

class CommentPermissions(TemplatePermission):
    """
    Permissions for Comment objects.
    Note: Staff are defined as graders, super graders, or course admins.
    
    - POST/PUT/PATCH/DELETE: Staff assigned to submission
    - GET: Inherits from submission permissions
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        submission = obj.file.submission

        # GET: inherit submission permissions
        if request.method == "GET":
            # For comments, we don't just inherit submission permissions because submissions are now always visible to students.
            # We must explicitly check if feedback is released.
            
            # Staff of submission can always view
            if isStaffOfSub(user, submission):
                return True

            # Students can view only once the feedback axis is open for this submission
            if isStudentOfSub(user, submission):
                return feedbackOpenForSubmission(submission)
            
            return False

        # All write operations: staff of submission only
        return isStaffOfSub(user, submission)


# =============================================================================
# AUTOGRADER PERMISSIONS (Test Cases, Test Categories, Submission Tests)
# =============================================================================


class TestCasePermissions(TemplatePermission):
    """
    Permissions for TestCase objects.
    
    - POST/PUT/PATCH/DELETE: Course admins only
    - GET: Course staff
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        course = obj.testCategory.assignment.course

        # GET: course staff
        if request.method == "GET":
            return isCourseStaff(user, course)

        # All write operations: course admin only
        return isCourseAdmin(user, course)


class TestCategoryPermissions(TemplatePermission):
    """
    Permissions for TestCategory objects.
    
    - POST/PUT/PATCH/DELETE: Course admins only
    - GET: Course staff
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        course = obj.assignment.course

        # GET: course staff
        if request.method == "GET":
            return isCourseStaff(user, course)

        # All write operations: course admin only
        return isCourseAdmin(user, course)


class LearningObjectivePermissions(TemplatePermission):
    """
    Permissions for LearningObjective objects.
    
    - POST/PUT/PATCH/DELETE: Course admins only
    - GET: Course staff
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        course = obj.assignment.course

        # GET: course staff
        if request.method == "GET":
            return isCourseStaff(user, course)

        # All write operations: course admin only
        return isCourseAdmin(user, course)


class TestCategoryResourcePermissions(TemplatePermission):
    """
    Permissions for TestCategoryResource objects.
    
    - POST/PUT/PATCH/DELETE: Course admins only
    - GET: Course staff
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        course = obj.category.assignment.course

        # GET: course staff
        if request.method == "GET":
            return isCourseStaff(user, course)

        # All write operations: course admin only
        return isCourseAdmin(user, course)


class TestCategoryFilePermissions(TemplatePermission):
    """
    Permissions for TestCategoryFile objects (JUnit test files on a category).

    - POST/PUT/PATCH/DELETE: Course admins only
    - GET: Course staff
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        course = obj.category.assignment.course

        # GET: course staff
        if request.method == "GET":
            return isCourseStaff(user, course)

        # All write operations: course admin only
        return isCourseAdmin(user, course)


class SubmissionTestPermissions(TemplatePermission):
    """
    Permissions for SubmissionTest objects.
    
    - POST/DELETE: Course admins only
    - PUT/PATCH: Course admins or staff of submission
    - GET: Staff of submission, or students of the submission
    """

    def has_object_permission(self, request, view, obj: SubmissionTest):
        user = cast(User, request.user)
        submission = obj.submission
        assignment = submission.assignment
        course = assignment.course

        # GET: staff of submission, or students of the submission
        if request.method == "GET":
            if isStaffOfSub(user, submission):
                return True
            if isStudentOfSub(user, submission):
                # Hidden tests are never directly readable by students; they receive
                # per-category synthetic summaries via /submissions/{id}/testResults/.
                testCase = obj.testCase
                if testCase is not None and testCase.hidden:
                    return False
                return True
            return False

        # PUT/PATCH: course admin or staff of submission
        if request.method in ["PUT", "PATCH"]:
            return isCourseAdmin(user, course) or isStaffOfSub(user, submission)

        # POST/DELETE: course admin only
        return isCourseAdmin(user, course)


# =============================================================================
# AI GRADING ASSISTANCE PERMISSIONS
# =============================================================================


class SuggestedCommentPermissions(TemplatePermission):
    """
    Permissions for SuggestedComment objects.
    Only staff of the submission can view or act on suggested comments.
    Students never see these.
    """

    def has_permission(self, request, view):
        # Skip POST-check for detail actions (accept/reject) — they operate
        # on existing objects and permission is checked in has_object_permission.
        if request.method == 'POST' and view.action in ('accept', 'reject'):
            return True
        return super().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        submission = obj.submission
        return isStaffOfSub(user, submission)


class SubmissionSummaryPermissions(TemplatePermission):
    """
    Permissions for SubmissionSummary objects.
    Only staff of the submission can view the summary.
    Students never see these.
    """

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        submission = obj.submission
        return isStaffOfSub(user, submission)


# =============================================================================
# QUIZZES (Phase 1: authoring, staff-only)
# =============================================================================

def _quiz_authoring_permission(user, course, method):
    """Phase 1 authoring policy: course staff read+write, admins (or superuser) delete.
    No student access. ``course`` may be ``None`` (unresolvable) → deny."""
    if user.is_superuser:
        return True
    if course is None:
        return False
    if method == "DELETE":
        return isCourseAdmin(user, course)
    return isCourseStaff(user, course)


class QuestionBankPermissions(TemplatePermission):
    """Course staff may author question banks; admins may delete."""

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        return _quiz_authoring_permission(user, obj.course, request.method)


class QuestionPermissions(TemplatePermission):
    """Course staff may author questions; admins may delete."""

    def has_permission(self, request, view):
        # These POST actions don't carry a `course` for the POST-check; they validate
        # access themselves (on the existing question / target bank's course).
        if request.method == 'POST' and getattr(view, 'action', None) in (
            'regenerateSuggestion', 'moveToBank', 'copyToBank',
        ):
            return True
        return super().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        return _quiz_authoring_permission(user, obj.course, request.method)


class QuizPermissions(TemplatePermission):
    """Course staff may author quizzes; admins may delete."""

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        return _quiz_authoring_permission(user, obj.course, request.method)


class QuizQuestionPermissions(TemplatePermission):
    """Membership edits follow the quiz's course authoring policy."""

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        # ``QuizQuestion.course`` derives from its quiz; set for both saved rows and
        # the lightweight POST-check instance (which carries the quiz).
        return _quiz_authoring_permission(user, obj.course, request.method)


class QuizQuestionGroupPermissions(TemplatePermission):
    """Random-draw groups follow the quiz's course authoring policy."""

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        return _quiz_authoring_permission(user, obj.course, request.method)


class SuggestedQuizQuestionPermissions(TemplatePermission):
    """AI quiz-question suggestions are staff-only. Course staff may view, edit pending
    suggestions, and accept/reject them. Students never see these."""

    def has_permission(self, request, view):
        # accept/reject act on existing objects — checked in has_object_permission.
        if request.method == 'POST' and view.action in ('accept', 'reject'):
            return True
        return super().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        course = obj.course
        if course is None:
            return False
        return user.is_superuser or isCourseStaff(user, course)


class QuizSuggestionJobPermissions(TemplatePermission):
    """Course staff may poll AI quiz-suggestion generation runs. Jobs are created
    by the generate endpoints (which enforce the generation capability), never
    directly through this viewset."""

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        return user.is_superuser or isCourseStaff(user, obj.course)


class QuizImportPermissions(TemplatePermission):
    """Course staff may start and view Canvas QTI imports. The create endpoint
    validates course staff explicitly (multipart upload)."""

    def has_permission(self, request, view):
        if request.method == 'POST':
            return True  # QuizImportJobViewSet.create() checks course staff
        return super().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        return user.is_superuser or isCourseStaff(user, obj.course)


class QuizImagePermissions(TemplatePermission):
    """Course staff may upload and manage description images. The create endpoint
    validates course staff explicitly (multipart upload). Public read is handled by
    the separate token-based serve view, not this permission."""

    def has_permission(self, request, view):
        if request.method == 'POST':
            return True  # QuizImageViewSet.create() checks course staff
        return super().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        return user.is_superuser or isCourseStaff(user, obj.course)


# =============================================================================
# QUIZZES (Phase 2: student taking)
# =============================================================================

def canGradeQuiz(user, course):
    """Quiz grading: superusers and course admins always; every course grader while the
    course's gradersCanGradeQuizzes flag (default on) is set; otherwise only the explicit
    per-course quizGraders role. Grading access includes reading attempts (answer keys)."""
    return (user.is_superuser or isCourseAdmin(user, course) or isQuizGrader(user, course)
            or (course.gradersCanGradeQuizzes and isGrader(user, course)))


class QuizAttemptPermissions(TemplatePermission):
    """Students take and view their OWN attempts; course staff may read attempts; quiz
    graders (and admins) may grade manual responses. The student-entry actions
    (start/myAttempts/availableQuizzes) validate enrollment + availability in the view
    body, so all this gates is auth + per-attempt ownership. Taking a quiz (start,
    saveAnswer, submit) is browser-only: static credentials (personal API tokens,
    course keys, basic auth) are rejected so attempts can't be scripted via the SDK.
    Quizzes with requireSebBrowser additionally demand a valid Safe Exam Browser
    signature on every taking request (and on reading an in-progress attempt, so a
    second, normal browser can't mine the questions mid-quiz)."""

    def _check_seb(self, request, quiz):
        """Raise a structured 403 when a SEB-required quiz's signature doesn't verify."""
        if not quiz.requireSebBrowser:
            return
        reason = seb.verify_seb_request(request, quiz)
        if reason is not None:
            raise seb.seb_permission_denied(reason)

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if getattr(view, 'action', None) in ('create', 'saveAnswer', 'submit'):
            from core.authentication import CourseAPIKeyAuthentication
            if isinstance(request.successful_authenticator,
                          (CourseAPIKeyAuthentication, TokenAuthentication, BasicAuthentication)):
                return False
        if getattr(view, 'action', None) == 'create':
            # Start is the only taking action without an attempt object; resolve the quiz
            # from the POST body. Missing/invalid/unknown ids pass through — the view
            # 400/404s them itself.
            try:
                quiz = Quiz.objects.get(pk=int(request.data.get('quiz')))
            except (TypeError, ValueError, Quiz.DoesNotExist):
                return True
            if quiz.requireSebBrowser:
                reason = seb.verify_seb_request(request, quiz)
                if reason is not None:
                    # Audit blocked starts only — saveAnswer fires per autosave and would spam.
                    from core.services.audit import record_audit_event
                    record_audit_event(
                        course=quiz.course, event_type='quiz_attempt_seb_blocked',
                        user=request.user, quiz=quiz, assignment=quiz.assignment,
                        meta={'quizTitle': quiz.title, 'reason': reason})
                    raise seb.seb_permission_denied(reason)
        return True

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        action = getattr(view, 'action', None)
        # Manual grading (undoing it, and pinning the official attempt) are the staff
        # writes on someone else's attempt.
        if action in ('gradeResponse', 'reopenResponse', 'setOfficial', 'runCode'):
            return canGradeQuiz(user, obj.quiz.course)
        if user == obj.student:
            if action in ('saveAnswer', 'submit') or (action == 'retrieve' and obj.status == 'in_progress'):
                self._check_seb(request, obj.quiz)
            return True
        # Quiz graders / admins may read (never mutate) a student's attempt. Assignment-only
        # graders are excluded — reading an attempt exposes the answer key, so it matches the
        # quiz-grading gate rather than the broader course-staff one.
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return canGradeQuiz(user, obj.quiz.course)
        return False


# =============================================================================
# QUIZZES (per-student generated questions)
# =============================================================================

def canReviewGeneratedQuestions(user, quiz):
    """Reviewing per-student generated question sets: course admins always; other course
    staff only when the quiz's gradersCanReviewGenerated flag is on (mirrors the
    collaborativeRubricMode flag-branch pattern)."""
    course = quiz.course
    if user.is_superuser or isCourseAdmin(user, course):
        return True
    return quiz.gradersCanReviewGenerated and isCourseStaff(user, course)


def canGenerateQuizQuestions(user, quiz):
    """Triggering the Generate-missing backfill (spends AI credits): course admins always;
    other course staff only when the quiz's gradersCanGenerate flag is on — the instructor's
    opt-in for a grader-run generate → review → release workflow (mirrors
    canReviewGeneratedQuestions)."""
    course = quiz.course
    if user.is_superuser or isCourseAdmin(user, course):
        return True
    return quiz.gradersCanGenerate and isCourseStaff(user, course)


def canViewSubmissionForGeneratedReview(user, submission):
    """Read-only submission access for reviewing AI-generated quiz questions: per-student
    questions are generated FROM the submission, so a reviewer needs to read it for
    context even when they aren't the assigned grader. Course staff only, and only when a
    quiz on this submission's assignment has generated sections open to grader review
    (admins/super graders already pass isStaffOfSub)."""
    assignment = submission.assignment
    if not isCourseStaff(user, assignment.course):
        return False
    return assignment.quizzes.filter(
        gradersCanReviewGenerated=True, generatedSections__isnull=False).exists()


class QuizGeneratedSectionPermissions(TemplatePermission):
    """Generated sections are quiz authoring config — same policy as random-draw groups."""

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        return _quiz_authoring_permission(user, obj.course, request.method)


class GeneratedQuestionSetPermissions(TemplatePermission):
    """Per-student generated sets are reviewed by admins (always) or staff (when the
    quiz allows it). Students never see these — they take approved questions through
    the normal attempt flow."""

    def has_permission(self, request, view):
        # approve/unapprove/regenerate act on existing objects — checked below.
        if request.method == 'POST' and view.action in ('approve', 'unapprove', 'regenerate'):
            return True
        return super().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        # Regeneration spends AI credits, so it is admin-only — the review flag only
        # grants graders review/edit/approve.
        if getattr(view, 'action', None) == 'regenerate':
            return user.is_superuser or isCourseAdmin(user, obj.quiz.course)
        return canReviewGeneratedQuestions(user, obj.quiz)


class GeneratedQuizQuestionPermissions(TemplatePermission):
    """Editing a generated question follows the same review gate as its set."""

    def has_object_permission(self, request, view, obj):
        user = cast(User, request.user)
        return canReviewGeneratedQuestions(user, obj.set.quiz)
