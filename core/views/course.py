# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from core.forms.forms import IDForm
from core.models import Course, QuizAccommodation, QuizAttempt, RubricCategory
from core.models import User
from core.serializers.course import (
    CourseSerializer,
    CourseRosterSerializer,
    CourseSettingsSerializer,
    CourseAISettingsSerializer,
    CourseRosterMapSerializer,
    CourseStudentCaptionsSerializer,
)
from core.serializers.assignment import AssignmentSerializer
from core.serializers.gradebook import GradebookResponseSerializer
from core.serializers.section import SectionSerializer
from core.serializers.questionBank import QuestionBankSerializer
from core.serializers.question import QuestionSerializer
from core.serializers.quiz import QuizSerializer
from core.views.template import SuperUserListProtectedViewSet

from rest_framework.response import Response
from rest_framework.decorators import action
from drf_spectacular.utils import extend_schema, inline_serializer, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers
from rest_framework import status

from core.permissions.permissions import CoursePermissions
from core.permissions.helpers import (
    returnNotAuthorized,
    returnForbidden,
    returnNotFound,
)
from core.permissions.helpers import isAuthenticated
from core.permissions.helpers import isCourseAdmin, isCourseMember, isCourseStaff
from core.permissions.helpers import isSuperGrader
import logging
from core.serializers.ai_usage import AIUsageSummarySerializer, AIProviderModelsListSerializer, AIProviderTestRequestSerializer, AIProviderTestResultSerializer
from core.throttles import AIConnectionTestThrottle
from core.serializers.actionResponses import CapabilitiesResponseSerializer
from core.permissions.capabilities import compute_course_capabilities, CAPABILITY_DESCRIPTIONS, Capability, require_capability, check_capability
from core.permissions.course_scope import get_course_scope_id
from core.serializers.course_audit_event import CourseAuditEventSerializer
from core.models import CourseAuditEvent, CourseAPIKey
from core.serializers.course_api_key import (
    PendingAgentActionSerializer,
    CourseAPIKeyReadSerializer,
    CourseAPIKeyCreateSerializer,
    CourseAPIKeyCreateResponseSerializer,
)

logger = logging.getLogger(__name__)

from core.pagination import LargeObjectsPagination


from core.utils import get_or_create_user

# Can override get_serializer method to use different serializer for
# different user types
from core.emails import UserAddedToCourseEmail

class QuizAccommodationRowSerializer(serializers.Serializer):
    """One per-student quiz accommodation: extra-time multiplier + SEB exemption."""
    student = serializers.EmailField()
    timeMultiplier = serializers.DecimalField(max_digits=4, decimal_places=2)
    # Omitted on write = leave the student's current exemption unchanged.
    sebExempt = serializers.BooleanField(required=False)


def generate_invite_code():
    import secrets
    import string

    # methodology: https://stackoverflow.com/a/23728630
    return "".join(
        secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10)
    )


class CourseViewSet(SuperUserListProtectedViewSet):
    """
    list:
    Return a list of all the courses.

    create:
    Create a new course.

    retrieve:
    Return the given course.

    update:
    Update a course.

    partial_update:
    Update a course.

    delete:
    Delete a course
    """

    queryset = Course.objects.all()
    serializer_class = CourseSerializer
    permission_classes = (IsAuthenticated, CoursePermissions)

    def list(self, request):
        user = request.user

        if not isAuthenticated(user):
            return returnNotAuthorized()

        courses = user.courseAdmin_courses.all()
        if user.profile.isOrgStaff:
            courses = courses | Course.objects.filter(organization=user.profile.organization)
            courses = courses.distinct()
        # compute_course_capabilities dereferences course.organization per course (AI feature
        # toggles); without this the list fans out into one org SELECT per course.
        courses = courses.select_related('organization')

        return Response(
            CourseSerializer(
                courses, many=True, context={"request": request}
            ).data
        )

    @extend_schema(
        responses=CapabilitiesResponseSerializer,
        parameters=[
            OpenApiParameter(
                name='descriptions', type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY, required=False,
                description='Include human-readable descriptions for each capability.',
            ),
        ],
    )
    @action(detail=True, methods=["GET"])
    def capabilities(self, request, pk=None):
        """Return the requesting user's capabilities for this course."""
        user = request.user
        if not isAuthenticated(user):
            return returnNotAuthorized()

        course = self.get_object()

        if not isCourseMember(user, course):
            return returnForbidden()

        is_scoped = get_course_scope_id(request) is not None
        caps = compute_course_capabilities(user, course, is_course_scoped=is_scoped)

        include_descriptions = request.query_params.get('descriptions', '').lower() in ('true', '1')
        if include_descriptions:
            descriptions = {
                cap: CAPABILITY_DESCRIPTIONS.get(Capability(cap), '')
                for cap in caps
            }
            return Response({'capabilities': caps, 'descriptions': descriptions})

        return Response({'capabilitiesMap': caps})

    @extend_schema(responses=CourseSettingsSerializer)
    @action(detail=True, methods=["GET", "PATCH"])
    def courseSettings(self, request, pk=None):
        user = request.user
        if not isAuthenticated(user):
            return returnNotAuthorized()

        course = self.get_object()

        if not isCourseMember(user, course):
            return returnForbidden()

        if request.method == "GET":
            serializer = CourseSettingsSerializer(course, context={"request": request})
            return Response(serializer.data)
        elif request.method == "PATCH":
            require_capability(user, 'edit_course_settings', course)

        serializer = CourseSettingsSerializer(
            course, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    @extend_schema(responses=OpenApiTypes.STR)
    @action(detail=True, methods=["PATCH"])
    def changeInviteCode(self, request, pk=None):
        user = request.user
        if not isAuthenticated(user):
            return returnNotAuthorized()

        course = self.get_object()
        require_capability(user, 'change_invite_code', course)

        course.inviteCode = generate_invite_code()
        course.save()

        return Response(course.inviteCode)

    @extend_schema(request=CourseAISettingsSerializer, responses=CourseAISettingsSerializer)
    @action(detail=True, methods=["GET", "PATCH"])
    def aiSettings(self, request, pk=None):
        """
        get:
        Get AI configuration for the course.

        patch:
        Update AI configuration for the course. Admin-only.
        """
        user = request.user
        if not isAuthenticated(user):
            return returnNotAuthorized()

        course = self.get_object()

        if request.method == "GET":
            if not isCourseStaff(user, course):
                return returnForbidden()
        else:
            require_capability(user, 'configure_ai', course)

        if request.method == "GET":
            serializer = CourseAISettingsSerializer(course, context={"request": request})
            return Response(serializer.data)

        serializer = CourseAISettingsSerializer(
            course, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    @extend_schema(
        responses={200: AIUsageSummarySerializer},
        parameters=[
            OpenApiParameter(name='granularity', required=False, type=str,
                             description="Time bucket granularity: 'hourly', 'daily', or 'monthly'",
                             enum=['hourly', 'daily', 'monthly']),
            OpenApiParameter(name='startDate', required=False, type=str,
                             description="Start date (ISO 8601)"),
            OpenApiParameter(name='endDate', required=False, type=str,
                             description="End date (ISO 8601)"),
        ],
    )
    @action(detail=True, methods=["GET"])
    def aiUsage(self, request, pk=None):
        """
        Returns AI usage analytics for the course.
        Includes time-series data and per-assignment breakdown.
        Only accessible by course admins.
        """
        user = request.user
        if not isAuthenticated(user):
            return returnNotAuthorized()

        course = self.get_object()

        require_capability(user, 'view_ai_usage', course)

        from core.services.ai_usage_analytics import get_usage_summary
        from core.services.ai_service import AIService
        from core.models import AIUsageRecord
        from django.utils.dateparse import parse_datetime

        granularity = request.query_params.get('granularity', 'daily')
        if granularity not in ('hourly', 'daily', 'monthly'):
            granularity = 'daily'

        start_date = None
        end_date = None
        start_str = request.query_params.get('startDate', '').strip()
        end_str = request.query_params.get('endDate', '').strip()
        if start_str:
            start_date = parse_datetime(start_str)
        if end_str:
            end_date = parse_datetime(end_str)

        queryset = AIUsageRecord.objects.filter(course=course)

        summary = get_usage_summary(
            queryset=queryset,
            granularity=granularity,
            start_date=start_date,
            end_date=end_date,
            breakdown_field='assignment',
            breakdown_name_field='assignment__name',
            projection_rates=AIService.merged_token_rates(course=course),
        )

        return Response(summary)

    @extend_schema(responses={200: AIProviderModelsListSerializer})
    @action(detail=True, methods=["GET"])
    def aiModels(self, request, pk=None):
        """
        GET: Return curated AI models for the course's effective provider.
        Also queries the provider's API for live model listings using the
        course's own credentials or inherited org credentials.
        Only accessible by course admins.
        """
        import asyncio
        from core.services.ai_service import AI_MODELS, list_provider_models, AIService

        user = request.user
        if not isAuthenticated(user):
            return returnNotAuthorized()

        course: Course = self.get_object()

        require_capability(user, 'configure_ai', course)

        # Use AIService to resolve effective config (handles org inheritance)
        svc = AIService(course)
        provider = svc.provider
        if not provider:
            return Response({'providers': []})

        # Build curated list
        curated = AI_MODELS.get(provider, [])
        result = {
            'provider': provider,
            'models': [
                {'id': mid, 'name': name, 'isDefault': default}
                for mid, name, default in curated
            ],
        }

        # Query provider for live models using effective credentials
        try:
            live = asyncio.run(list_provider_models(
                provider=provider,
                api_key=svc.api_key or '',
                base_url=svc.base_url or '',
            ))
            result['liveModels'] = live
        except Exception as e:
            logger.warning(f"Failed to list models from {provider}: {e}")
            result['liveError'] = str(e)

        return Response({'providers': [result]})

    @extend_schema(request=AIProviderTestRequestSerializer, responses={200: AIProviderTestResultSerializer})
    @action(detail=True, methods=["POST"], throttle_classes=[AIConnectionTestThrottle])
    def aiTest(self, request, pk=None):
        """
        POST: Fire a small completion through the course's effective AI
        config (own settings or inherited org settings) and report success,
        latency, and any error. Accepts an optional custom prompt and a
        one-off model override. Recorded in AI usage as 'provider_test'.
        Only accessible by course admins.
        """
        import asyncio
        from core.services.ai_service import AIService, GenerationResult

        user = request.user
        if not isAuthenticated(user):
            return returnNotAuthorized()

        course: Course = self.get_object()

        require_capability(user, 'configure_ai', course)

        body = AIProviderTestRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        svc = AIService(course).set_request_context(user=user, request_type='provider_test')
        result = asyncio.run(svc.test_connection(
            prompt=body.validated_data.get('prompt') or None,
            model=body.validated_data.get('model') or None,
        ))

        # Record usage when a request was actually attempted (sync context —
        # record_usage does ORM work that can't run inside asyncio.run).
        if result.get('requestSystemPrompt') is not None:
            svc.record_usage(
                GenerationResult(
                    text=result.get('response') or '',
                    success=result['success'],
                    error=result.get('error'),
                    input_tokens=result.get('_inputTokens', 0),
                    output_tokens=result.get('_outputTokens', 0),
                    total_tokens=result.get('_totalTokens', 0),
                    cached_tokens=result.get('_cachedTokens', 0),
                ),
                user,
                request_type='provider_test',
            )

        return Response(AIProviderTestResultSerializer(result).data)

    @extend_schema(responses=CourseRosterSerializer)
    @action(detail=True, methods=["GET", "PATCH"])
    def roster(self, request, pk=None):
        """
        get:
        Show the roster for a course.

        patch:
        Update the roster for a course.
        """
        user = request.user
        course = self.get_object()

        error = get_roster_permission_errors(user, request, course)
        if error:
            return error

        if request.method == "GET":
            serializer = CourseRosterSerializer(course, context={"request": request})
            return Response(serializer.data)

        else:
            # Pre-filter fields for any users who do not exist yet
            # Create these users so serializer doesn't raise a DoesNotExist
            # error for any of them
            tempCreated: list[User] = []
            for keyEl in ["students", "graders", "courseAdmins"]:
                # Since these users are being created via the SDK, assume they should be active
                parse_new_users(keyEl, request, tempCreated, auto_activate=True)

            # Log for emailing new students
            oldStudents = list(course.students.all())
            oldGraders = list(course.graders.all())
            oldAdmins = list(course.courseAdmins.all())

            serializer = CourseRosterSerializer(
                course, data=request.data, partial=True, context={"request": request}
            )
            if serializer.is_valid():
                self.perform_update(serializer)

                # Add elevated permissions to courseAdmins
                newAdmins = course.courseAdmins.all()
                for admin in newAdmins:
                    if admin not in oldAdmins:
                        # Permissions model: once a user is added as a courseadmin to (any) course, they unlock the ability to create courses
                        # and modify rosters.
                        add_admin_privileges(admin)

                # If course setting is set, email newly created users
                if course.emailNewUsers:
                    # Email new students
                    for keyEl in ["students", "graders", "courseAdmins"]:
                        if keyEl in request.data:
                            if keyEl == "students":
                                oldList = oldStudents
                                newList = course.students.all()
                                roleType = "student"
                            elif keyEl == "graders":
                                oldList = oldGraders
                                newList = course.graders.all()
                                roleType = "grader"
                            elif keyEl == "courseAdmins":
                                oldList = oldAdmins
                                newList = course.courseAdmins.all()
                                roleType = "admin"

                            for userInRoster in newList:
                                if userInRoster not in oldList:
                                    UserAddedToCourseEmail(userInRoster).send_email(
                                        course_name=course.name,
                                        course_period=course.period,
                                        user_type=roleType,
                                        force_send=True
                                    )
                                    # send_new_user_email(userInRoster, roleType, course)

                from webhooks.signals import hook_event

                for keyEl in ["students", "graders", "courseAdmins"]:
                    """
                    Roster webhooks
                    > course.courseAdmins
                    > course.graders
                    > course.students

                    Webhook triggers with new users added to the 'extra' field in the payload
                    """
                    if keyEl in request.data:
                        if keyEl == "students":
                            oldList = oldStudents
                            newList = course.students.all()
                            roleType = "student"
                        elif keyEl == "graders":
                            oldList = oldGraders
                            newList = course.graders.all()
                            roleType = "grader"
                        elif keyEl == "courseAdmins":
                            oldList = oldAdmins
                            newList = course.courseAdmins.all()
                            roleType = "admin"

                        hook_event.send(
                            sender=course.__class__,
                            action="roster",
                            instance=course,
                            updated_fields=[keyEl],
                            payload_addition=list(
                                newList.exclude(
                                    email__in=map(lambda u: u.email, oldList)
                                )
                            ),
                        )

            else:
                # If something went wrong, delete the newly created users
                for user in tempCreated:
                    user.delete()

                # Then trigger the validation error (normally raised via
                # serializer.is_valid(raise_exception=True))
                raise serializers.ValidationError(serializer.errors)

            return Response(serializer.data)

    @extend_schema(responses=CourseRosterSerializer)
    @action(detail=True, methods=["PATCH"])
    def addToRoster(self, request, pk=None):
        """
        get:
        Show the roster for a course.

        patch:
        Update the roster for a course.
        """
        user = request.user
        course = self.get_object()

        error = get_roster_permission_errors(user, request, course)
        if error:
            return error

        # Pre-filter fields for any users who do not exist yet
        newStudents: list[User] = []
        newGraders: list[User] = []
        newAdmins: list[User] = []
        newSuperGraders: list[User] = []
        newRubricEditors: list[User] = []
        newQuizGraders: list[User] = []
        for keyEl, userList in zip(
            ("students", "graders", "courseAdmins", "superGraders", "rubricEditors", "quizGraders"),
            (newStudents, newGraders, newAdmins, newSuperGraders, newRubricEditors, newQuizGraders),
        ):
            parse_new_users(keyEl, request, userList)
        # Add the users to the course
        course.students.add(*newStudents)
        course.inactive_students.remove(*newStudents)
        course.graders.add(*newGraders)
        course.inactive_graders.remove(*newGraders)
        course.courseAdmins.add(*newAdmins)
        course.inactive_courseAdmins.remove(*newAdmins)
        course.superGraders.add(*newSuperGraders)
        course.rubricEditors.add(*newRubricEditors)
        course.quizGraders.add(*newQuizGraders)
        course.save()

        for admin in newAdmins:
            # Permissions model: once a user is added as a courseadmin to (any) course, they unlock the ability to create courses
            # and modify rosters.
            add_admin_privileges(admin)

            if not admin.is_active or course.emailNewUsers: 
                # Email the admin that they have been added to the course, so they can activate their account, bypasses email new users setting for course
                UserAddedToCourseEmail(admin).send_email(
                    course_name=course.name,
                    course_period=course.period,
                    user_type="admin",
                    force_send=course.emailNewUsers
                )

        # If course setting is set, email newly created users
        if course.emailNewUsers:
            # Email new students

            for userList, roleType in zip(
                (newGraders, newStudents), ("grader", "student")
            ):
                for newUser in userList:
                    UserAddedToCourseEmail(newUser).send_email(
                        course_name=course.name,
                        course_period=course.period,
                        user_type=roleType,
                        force_send=True
                    )

        serializer = CourseRosterSerializer(course, context={"request": request})
        return Response(serializer.data)

    @extend_schema(responses=CourseRosterSerializer)
    @action(detail=True, methods=["PATCH"])
    def removeFromRoster(self, request, pk=None):
        """
        get:
        Show the roster for a course.

        patch:
        Update the roster for a course.
        """
        user = request.user
        course = self.get_object()

        error = get_roster_permission_errors(user, request, course)
        if error:
            return error

        # Pre-filter fields for any users who do not exist yet
        inactiveStudents, inactiveGraders, inactiveAdmins, inactiveSuperGraders, inactiveRubricEditors, inactiveQuizGraders = (
            [],
            [],
            [],
            [],
            [],
            [],
        )
        for keyEl, userList in zip(
            ("students", "graders", "courseAdmins", "superGraders", "rubricEditors", "quizGraders"),
            (inactiveStudents, inactiveGraders, inactiveAdmins, inactiveSuperGraders, inactiveRubricEditors, inactiveQuizGraders),
        ):
            parse_new_users(keyEl, request, userList)
        # Add the users to the course
        course.students.remove(*inactiveStudents)
        course.inactive_students.add(*inactiveStudents)
        course.graders.remove(*inactiveGraders)
        course.inactive_graders.add(*inactiveGraders)
        course.courseAdmins.remove(*inactiveAdmins)
        course.inactive_courseAdmins.add(*inactiveAdmins)
        course.superGraders.remove(*inactiveGraders, *inactiveSuperGraders)
        course.rubricEditors.remove(*inactiveGraders, *inactiveRubricEditors)
        course.quizGraders.remove(*inactiveGraders, *inactiveQuizGraders)
        course.save()

        # remove inactive graders and students from their sections
        for section in course.sections.all():
            if len(inactiveGraders) > 0:
                section.leaders.remove(*inactiveGraders)
            if len(inactiveStudents) > 0:
                section.students.remove(*inactiveStudents)
            section.save()

        serializer = CourseRosterSerializer(course, context={"request": request})
        return Response(serializer.data)

    @extend_schema(responses={204: None})
    @action(detail=True, methods=["patch"])
    def deleteRubricCategory(self, request, pk=None):
        user = request.user
        if not isAuthenticated(user):
            return returnNotAuthorized()

        form = IDForm(request.data)
        if form.is_valid():
            course = Course.objects.get(id=pk)

            require_capability(user, 'edit_rubric', course)

            try:
                category = RubricCategory.objects.get(id=form.cleaned_data["id"])
            except:
                return returnNotFound(message="Category doesn't exist")

            category.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        else:
            return Response(form.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(request=CourseRosterMapSerializer, responses=CourseRosterMapSerializer)
    @action(detail=True, methods=["GET", "PATCH"])
    def rosterMap(self, request, pk=None):
        user = request.user
        if not isAuthenticated(user):
            return returnNotAuthorized()

        course = self.get_object()
        require_capability(user, 'manage_roster', course)

        if request.method == "GET":
            serializer = CourseRosterMapSerializer({"rosterMap": course.rosterMap})
            return Response(serializer.data)
        else:
            serializer = CourseRosterMapSerializer(data=request.data)
            if not serializer.is_valid():
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            map = serializer.validated_data.get("rosterMap", {})
            course.rosterMap = map
            course.save()
            return Response(CourseRosterMapSerializer({"rosterMap": course.rosterMap}).data)

    @extend_schema(request=CourseStudentCaptionsSerializer, responses=CourseStudentCaptionsSerializer)
    @action(detail=True, methods=["GET", "PATCH"])
    def studentCaptions(self, request, pk=None):
        user = request.user
        if not isAuthenticated(user):
            return returnNotAuthorized()

        course = self.get_object()
        require_capability(user, 'manage_roster', course)

        if request.method == "GET":
            serializer = CourseStudentCaptionsSerializer({"studentCaptions": course.studentCaptions})
            return Response(serializer.data)
        else:
            serializer = CourseStudentCaptionsSerializer(data=request.data)
            if not serializer.is_valid():
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            new_captions = serializer.validated_data.get("studentCaptions", {})
            course.studentCaptions = new_captions
            course.save()
            return Response(CourseStudentCaptionsSerializer({"studentCaptions": course.studentCaptions}).data)

    @extend_schema(responses=SectionSerializer(many=True))
    @action(detail=True, methods=["GET"], pagination_class=LargeObjectsPagination)
    def sections(self, request, pk=None):
        """
        Gets a paginated list of sections for a course.
        We use this for performance for large courses to fetch sections in bulk.
        They're rarely used in admin console operations, so it's a great candidate to paginate
        Returns a list of Section objects
        """
        user = request.user
        course = self.get_object()

        # Only allow course admins to access this endpoint
        require_capability(user, 'manage_sections', course)

        sections = course.sections.all().prefetch_related("leaders", "students")
        page = self.paginate_queryset(sections)
        if page is not None:
            serializer = SectionSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = SectionSerializer(sections, many=True)
        return Response(serializer.data)

    @extend_schema(
        responses=inline_serializer('QuizGradingProgress', {
            'quizzes': inline_serializer('QuizGradingProgressQuiz', {
                'id': serializers.IntegerField(),
                'title': serializers.CharField(),
                'totalManual': serializers.IntegerField(),
                'graded': serializers.IntegerField(),
                'pending': serializers.IntegerField(),
            }, many=True),
            'graders': inline_serializer('QuizGradingProgressGrader', {
                'grader': serializers.EmailField(),
                'totalGraded': serializers.IntegerField(),
                'lastGradedAt': serializers.DateTimeField(allow_null=True),
                'perQuiz': serializers.DictField(child=serializers.IntegerField()),
            }, many=True),
            'pendingUngraded': serializers.IntegerField(),
        }),
        description="Per-grader manual quiz-grading progress across the course's published "
                    "quizzes (submitted attempts only). perQuiz keys are quiz ids; graders "
                    "who left the course keep their rows (accountability, not a roster).",
    )
    @action(detail=True, methods=["GET"])
    def quizGradingProgress(self, request, pk=None):
        """Per-grader manual quiz-grading progress for the course's published quizzes."""
        course = self.get_object()
        require_capability(request.user, 'view_analytics', course)
        from core.services.quiz_analytics import get_quiz_grading_progress
        return Response(get_quiz_grading_progress(course))

    @extend_schema(
        responses=CourseAuditEventSerializer(many=True),
        parameters=[
            OpenApiParameter(name='student', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, required=False, description='Filter by student email'),
            OpenApiParameter(name='assignment', type=OpenApiTypes.INT, location=OpenApiParameter.QUERY, required=False, description='Filter by assignment ID'),
            OpenApiParameter(name='event_type', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, required=False, description='Filter by event type'),
            OpenApiParameter(name='date_from', type=OpenApiTypes.DATETIME, location=OpenApiParameter.QUERY, required=False, description='Filter events after this datetime'),
            OpenApiParameter(name='date_to', type=OpenApiTypes.DATETIME, location=OpenApiParameter.QUERY, required=False, description='Filter events before this datetime'),
        ],
    )
    @action(detail=True, methods=["GET"], pagination_class=LargeObjectsPagination)
    def auditLog(self, request, pk=None):
        """Return paginated audit events for a course, filterable by student, assignment, event type, and date range."""
        user = request.user
        course = self.get_object()

        require_capability(user, 'view_audit_log', course)

        qs = CourseAuditEvent.objects.filter(course=course).select_related('user', 'assignment', 'submission', 'quiz')

        student = request.query_params.get('student')
        assignment = request.query_params.get('assignment')
        event_type = request.query_params.get('event_type')
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')

        if student:
            qs = qs.filter(user__email=student)
        if assignment:
            qs = qs.filter(assignment_id=assignment)
        if event_type:
            qs = qs.filter(event_type=event_type)
        if date_from:
            qs = qs.filter(created__gte=date_from)
        if date_to:
            qs = qs.filter(created__lte=date_to)

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = CourseAuditEventSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = CourseAuditEventSerializer(qs, many=True)
        return Response(serializer.data)

    @extend_schema(
        responses={(200, 'text/csv'): OpenApiTypes.STR},
        parameters=[
            OpenApiParameter(name='student', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(name='assignment', type=OpenApiTypes.INT, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(name='event_type', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(name='date_from', type=OpenApiTypes.DATETIME, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(name='date_to', type=OpenApiTypes.DATETIME, location=OpenApiParameter.QUERY, required=False),
        ],
    )
    @action(detail=True, methods=["GET"])
    def auditLogExport(self, request, pk=None):
        """Export audit events for a course as CSV."""
        import csv
        from django.http import HttpResponse

        user = request.user
        course = self.get_object()

        require_capability(user, 'view_audit_log', course)

        qs = CourseAuditEvent.objects.filter(course=course).select_related('user', 'assignment', 'submission', 'quiz')

        student = request.query_params.get('student')
        assignment = request.query_params.get('assignment')
        event_type = request.query_params.get('event_type')
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')

        if student:
            qs = qs.filter(user__email=student)
        if assignment:
            qs = qs.filter(assignment_id=assignment)
        if event_type:
            qs = qs.filter(event_type=event_type)
        if date_from:
            qs = qs.filter(created__gte=date_from)
        if date_to:
            qs = qs.filter(created__lte=date_to)

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="audit_log_course_{course.id}.csv"'

        writer = csv.writer(response)
        writer.writerow(['Timestamp', 'Event Type', 'Student Email', 'Assignment', 'Quiz', 'Submission ID', 'Details'])

        for event in qs.iterator():
            quiz_title = event.quiz.title if event.quiz else (event.meta or {}).get('title') if isinstance(event.meta, dict) else ''
            writer.writerow([
                event.created.isoformat(),
                event.event_type,
                event.user.email if event.user else '',
                event.assignment.name if event.assignment else '',
                quiz_title or '',
                event.submission_id or '',
                str(event.meta) if event.meta else '',
            ])

        return response

    # ----- Course API Keys -----

    @extend_schema(
        request=CourseAPIKeyCreateSerializer,
        responses={201: CourseAPIKeyCreateResponseSerializer},
    )
    @action(
        detail=True,
        methods=["GET", "POST"],
        url_path="apiKeys",
        url_name="api-keys",
        permission_classes=[IsAuthenticated],
    )
    def apiKeys(self, request, pk=None):
        """List or create course-scoped API keys."""
        course = self.get_object()

        is_scoped = get_course_scope_id(request) is not None
        require_capability(request.user, 'manage_course_api_keys', course, is_course_scoped=is_scoped)

        if request.method == "GET":
            keys = CourseAPIKey.objects.filter(course=course).order_by("-created")
            serializer = CourseAPIKeyReadSerializer(keys, many=True)
            return Response(serializer.data)

        # POST — create a new key
        ser = CourseAPIKeyCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        name = ser.validated_data["name"]
        scope = ser.validated_data.get("scope", "read")

        if CourseAPIKey.objects.filter(course=course, name=name).exists():
            return Response(
                {"error": f"A key named '{name}' already exists for this course."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_key = CourseAPIKey.generate_key(course.id)
        prefix = raw_key[: raw_key.index("_", 4) + 1]  # e.g. "cpk_123_"

        api_key = CourseAPIKey.objects.create(
            course=course,
            name=name,
            key_prefix=prefix,
            hashed_key=CourseAPIKey.hash_key(raw_key),
            created_by=request.user,
            scope=scope,
        )

        return Response(
            {
                "id": api_key.id,
                "name": api_key.name,
                "key": raw_key,
                "keyPrefix": api_key.key_prefix,
                "scope": api_key.scope,
                "createdBy": request.user.username,
                "created": api_key.created,
            },
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        responses={200: CourseAPIKeyReadSerializer},
    )
    @action(
        detail=True,
        methods=["PATCH", "DELETE"],
        url_path=r"apiKeys/(?P<key_id>\d+)",
        url_name="api-key-detail",
        permission_classes=[IsAuthenticated],
    )
    def apiKeyDetail(self, request, pk=None, key_id=None):
        """Update or revoke a single course API key."""
        course = self.get_object()

        is_scoped = get_course_scope_id(request) is not None
        require_capability(request.user, 'manage_course_api_keys', course, is_course_scoped=is_scoped)

        try:
            api_key = CourseAPIKey.objects.get(pk=key_id, course=course)
        except CourseAPIKey.DoesNotExist:
            return returnNotFound()

        if request.method == "DELETE":
            api_key.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        # PATCH
        if "name" in request.data:
            api_key.name = request.data["name"]
        if "isActive" in request.data:
            api_key.is_active = request.data["isActive"]

        api_key.save()
        serializer = CourseAPIKeyReadSerializer(api_key)
        return Response(serializer.data)

    @extend_schema(responses=PendingAgentActionSerializer(many=True))
    @action(detail=True, methods=["GET"], url_path="pendingAgentActions")
    def pendingAgentActions(self, request, pk=None):
        """Tier-3 agent actions awaiting (or holding) this course's approval.

        The whole point of this panel is that the AGENT cannot touch it, so a
        course-scoped credential (the agent's own key) is refused outright —
        only a human course admin, signed in normally, may see or decide these.
        """
        from django.utils import timezone

        from core.models import PendingAgentAction

        course = self.get_object()
        if get_course_scope_id(request) is not None:
            return Response(
                {"detail": "Pending agent actions are only visible to a signed-in "
                           "course admin, never to a course-scoped credential."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not (request.user.is_superuser or isCourseAdmin(request.user, course)):
            return returnForbidden()

        # Approved-but-unconsumed rows stay visible so the admin can watch the
        # request flip to executed (the agent's retry consumes it) or expire.
        rows = PendingAgentAction.objects.filter(
            course=course, denied_at=None, redeemed_at=None,
            expires_at__gt=timezone.now())
        return Response(PendingAgentActionSerializer(rows, many=True).data)

    @extend_schema(request=None, responses={204: None})
    # POST, not DELETE: CoursePermissions forbids the DELETE verb on Course
    # objects wholesale, and sub-actions inherit that object check.
    @action(detail=True, methods=["POST"],
            url_path=r"pendingAgentActions/(?P<action_id>\d+)/deny")
    def denyPendingAgentAction(self, request, pk=None, action_id=None):
        """Deny a pending agent action.

        The denial sticks until the request expires: the agent's retries get a
        clear "denied — stop asking" error instead of minting a fresh request.
        """
        from django.utils import timezone

        from core.models import PendingAgentAction

        course = self.get_object()
        if get_course_scope_id(request) is not None:
            return Response(status=status.HTTP_403_FORBIDDEN)
        if not (request.user.is_superuser or isCourseAdmin(request.user, course)):
            return returnForbidden()

        action_row = PendingAgentAction.objects.filter(
            pk=action_id, course=course).first()
        if action_row is None:
            return returnNotFound(message="No such pending action")
        PendingAgentAction.objects.filter(
            pk=action_row.pk, denied_at=None).update(denied_at=timezone.now())
        self._audit_agent_action(request, course, action_row, approved=False)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=None, responses={204: None})
    @action(detail=True, methods=["POST"],
            url_path=r"pendingAgentActions/(?P<action_id>\d+)/approve")
    def approvePendingAgentAction(self, request, pk=None, action_id=None):
        """Approve a pending agent action — the agent's next retry executes it.

        The conditional update makes approve-vs-deny and double-click races
        resolve deterministically: whoever flips the row first wins, everyone
        else gets 409.
        """
        from django.utils import timezone

        from core.models import PendingAgentAction

        course = self.get_object()
        if get_course_scope_id(request) is not None:
            return Response(
                {"detail": "Only a signed-in course admin may approve agent "
                           "actions, never a course-scoped credential."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not (request.user.is_superuser or isCourseAdmin(request.user, course)):
            return returnForbidden()

        action_row = PendingAgentAction.objects.filter(
            pk=action_id, course=course).first()
        if action_row is None:
            return returnNotFound(message="No such pending action")
        flipped = PendingAgentAction.objects.filter(
            pk=action_row.pk, denied_at=None, approved_at=None,
            redeemed_at=None, expires_at__gt=timezone.now(),
        ).update(approved_at=timezone.now(), approved_by=request.user)
        if not flipped:
            return Response(
                {"detail": "This action can no longer be approved (already "
                           "decided, consumed, or expired)."},
                status=status.HTTP_409_CONFLICT)
        self._audit_agent_action(request, course, action_row, approved=True)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _audit_agent_action(self, request, course, action_row, *, approved):
        from core.services.audit import record_audit_event

        record_audit_event(
            course=course,
            event_type="agent_action_approved" if approved
                       else "agent_action_denied",
            user=request.user,
            meta={"tool": action_row.tool, "planHash": action_row.plan_hash,
                  "actionId": action_row.pk, "origin": "dashboard"})

    @extend_schema(
        parameters=[OpenApiParameter(
            name="q", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY,
            required=True,
            description="An agent alias (student-3f9a1c2d40), a student's email, "
                        "or a bare NetID/username.")],
        responses=inline_serializer(name="AgentAliasLookupResponse", fields={
            "matches": serializers.ListField(child=inline_serializer(
                name="AgentAliasMatch", fields={
                    "alias": serializers.CharField(),
                    "email": serializers.EmailField(),
                    "username": serializers.CharField(),
                    "active": serializers.BooleanField(),
                }))}))
    @action(detail=True, methods=["GET"], url_path="agentAliases")
    def agentAliases(self, request, pk=None):
        """Resolve the pseudonymous alias an MCP agent uses for a student back
        to the student (or the reverse).

        Course-scoped credentials are refused outright: the agent's own key
        must never be able to de-anonymize its own output. Only a human
        course admin, signed in normally, may look aliases up.
        """
        from core.agent.privacy import StudentAliasMap

        course = self.get_object()
        if get_course_scope_id(request) is not None:
            return Response(
                {"detail": "Alias lookup is only available to a signed-in course "
                           "admin, never to a course-scoped credential."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not (request.user.is_superuser or isCourseAdmin(request.user, course)):
            return returnForbidden()

        q = (request.query_params.get("q") or "").strip().lower()
        if not q:
            return Response({"detail": "Pass ?q=<alias, email or NetID>."},
                            status=status.HTTP_400_BAD_REQUEST)

        aliases = StudentAliasMap.for_course(course)
        if q in aliases.by_alias:
            emails = aliases.by_alias[q]
        elif q in aliases.by_email:
            emails = aliases.by_alias[aliases.by_email[q]]
        elif q in aliases.by_token:
            emails = [e for e in aliases.by_alias[aliases.by_token[q]]
                      if e.lower().split("@", 1)[0] == q
                      or aliases.username_of[e.lower()].lower() == q]
        else:
            emails = []
        active = {e.lower() for e in course.students.values_list("email", flat=True)}
        return Response({"matches": [
            {"alias": aliases.by_email[e.lower()], "email": e,
             "username": aliases.username_of[e.lower()],
             "active": e.lower() in active}
            for e in emails]})

    @extend_schema(responses=QuestionBankSerializer(many=True))
    @action(detail=True, methods=["GET"], url_path="questionBanks")
    def questionBanks(self, request, pk=None):
        """List the course's quiz question banks (staff only)."""
        course = self.get_object()
        if not (request.user.is_superuser or isCourseStaff(request.user, course)):
            return returnForbidden()
        banks = course.questionBanks.all()
        return Response(QuestionBankSerializer(banks, many=True, context={"request": request}).data)

    @extend_schema(responses=QuizSerializer(many=True))
    @action(detail=True, methods=["GET"])
    def quizzes(self, request, pk=None):
        """List the course's quizzes (staff only)."""
        course = self.get_object()
        if not (request.user.is_superuser or isCourseStaff(request.user, course)):
            return returnForbidden()
        quizzes = course.quizzes.all()
        return Response(QuizSerializer(quizzes, many=True, context={"request": request}).data)

    @extend_schema(responses=QuizAccommodationRowSerializer(many=True))
    @action(detail=True, methods=["GET"])
    def quizAccommodations(self, request, pk=None):
        """List per-student quiz extra-time accommodations (course admins only)."""
        course = self.get_object()
        if not (request.user.is_superuser or isCourseAdmin(request.user, course)):
            return returnForbidden()
        rows = course.quizAccommodations.select_related('student').order_by('student__email')
        return Response(QuizAccommodationRowSerializer(
            [{'student': a.student.email, 'timeMultiplier': a.timeMultiplier,
              'sebExempt': a.sebExempt} for a in rows],
            many=True).data)

    @extend_schema(
        request=QuizAccommodationRowSerializer,
        responses=QuizAccommodationRowSerializer,
    )
    @action(detail=True, methods=["PATCH"])
    def setQuizAccommodation(self, request, pk=None):
        """Set a student's quiz time multiplier (course admins only). A multiplier of 1
        removes the accommodation."""
        course = self.get_object()
        if not (request.user.is_superuser or isCourseAdmin(request.user, course)):
            return returnForbidden()
        if course.archived:
            return Response({'detail': 'This course is archived.'}, status=status.HTTP_403_FORBIDDEN)
        ser = QuizAccommodationRowSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        multiplier = ser.validated_data['timeMultiplier']
        if multiplier < 1:
            return Response({'detail': 'timeMultiplier must be at least 1.'},
                            status=status.HTTP_400_BAD_REQUEST)
        student = course.students.filter(email=ser.validated_data['student']).first()
        if student is None:
            return Response({'detail': 'No such student in this course.'},
                            status=status.HTTP_400_BAD_REQUEST)
        # An omitted sebExempt preserves the student's current exemption, so the roster's
        # time-multiplier control can keep sending {student, timeMultiplier} alone.
        existing = QuizAccommodation.objects.filter(course=course, student=student).first()
        seb_exempt = ser.validated_data.get(
            'sebExempt', existing.sebExempt if existing else False)
        if multiplier == 1 and not seb_exempt:
            QuizAccommodation.objects.filter(course=course, student=student).delete()
        else:
            QuizAccommodation.objects.update_or_create(
                course=course, student=student,
                defaults={'timeMultiplier': multiplier, 'sebExempt': seb_exempt})
        # Reflect the new multiplier onto the student's in-progress attempts — deadlines are
        # stored at start, so without this an accommodation granted mid-quiz would do nothing.
        from core.services import quiz_grading
        for attempt in QuizAttempt.objects.filter(
                quiz__course=course, student=student, status='in_progress').select_related('quiz'):
            deadline = quiz_grading.compute_attempt_deadline(
                attempt.quiz, student, attempt.startedAt, bypass_close=attempt.closeBypassed)
            if deadline != attempt.deadline:
                attempt.deadline = deadline
                attempt.save()
        return Response(QuizAccommodationRowSerializer(
            {'student': student.email, 'timeMultiplier': multiplier,
             'sebExempt': seb_exempt}).data)

    @extend_schema(responses=GradebookResponseSerializer)
    @action(detail=True, methods=["GET"])
    def gradebook(self, request, pk=None):
        """The course gradebook: every active student × every assignment and quiz, with
        totals over graded work (course admins only)."""
        from core.services.gradebook import build_gradebook
        course = self.get_object()
        if not (request.user.is_superuser or isCourseAdmin(request.user, course)):
            return returnForbidden()
        return Response(GradebookResponseSerializer(build_gradebook(course)).data)

    @extend_schema(responses=AssignmentSerializer(many=True))
    @action(detail=True, methods=["GET"])
    def assignments(self, request, pk=None):
        """Every assignment in the course the caller may see, each serialized exactly as
        GET /assignments/{id}/ would serialize it for this caller. Replaces the client-side
        per-id fan-out (the course detail returns ids only, and list endpoints are blocked).

        The visible set mirrors AssignmentPermissions.has_object_permission, NOT the course
        serializer's id list: staff and superusers see everything; students see
        STUDENT_VISIBLE_STATES minus section-hidden assignments; anyone else who can read
        the course (org staff of the org who is not a member) gets an empty list, because
        every per-id retrieve 403s for them today."""
        from django.db.models import Avg, Count, Max, Min, Q
        from core.models import STUDENT_VISIBLE_STATES
        from core.permissions.helpers import assignmentFeedbackOpen, isGrader, isStudent
        from core.serializers.assignment import (
            AssignmentSerializerWithStatisticsAndSummary,
            AssignmentStudentSerializer,
            AssignmentStudentSerializerNoStats,
            AssignmentStudentSerializerWithStats,
        )

        course = self.get_object()
        user = request.user

        if user.is_superuser or isCourseStaff(user, course):
            qs = course.assignments.all()
        elif isStudent(user, course):
            qs = (course.assignments
                  .filter(state__in=STUDENT_VISIBLE_STATES)
                  .exclude(hideFrom__students=user))
        else:
            return Response([])

        qs = qs.prefetch_related('files', 'rubricCategories', 'testCategories', 'dataSets')
        context = {'request': request}

        # Serializer choice replicates AssignmentViewSet.get_serializer_class for retrieve —
        # including the quirk that a superuser who is not a course member falls through to
        # the student serializers, exactly as the per-id endpoint does.
        if isCourseAdmin(user, course):
            # Same annotations retrieve uses; without them the stats/summary fields
            # recompute per object (~8 queries × N assignments).
            qs = qs.annotate(
                submissions_count_anno=Count('submissions', distinct=True),
                submissions_finalized_count_anno=Count('submissions', filter=Q(submissions__isFinalized=True), distinct=True),
                submissions_inprogress_count_anno=Count('submissions', filter=Q(submissions__isFinalized=False) & ~Q(submissions__grader=None), distinct=True),
                submissions_unclaimed_count_anno=Count('submissions', filter=Q(submissions__grader=None), distinct=True),
                stats_max_anno=Max('submissions__grade', filter=Q(submissions__isFinalized=True)),
                stats_min_anno=Min('submissions__grade', filter=Q(submissions__isFinalized=True)),
                stats_mean_anno=Avg('submissions__grade', filter=Q(submissions__isFinalized=True)),
            )
            return Response(AssignmentSerializerWithStatisticsAndSummary(qs, many=True, context=context).data)
        if isGrader(user, course):
            return Response(AssignmentSerializer(qs, many=True, context=context).data)

        data = []
        for assignment in qs:
            if not assignmentFeedbackOpen(assignment, user):
                serializer_class = AssignmentStudentSerializer
            elif not course.showStudentsStatistics:
                serializer_class = AssignmentStudentSerializerNoStats
            else:
                serializer_class = AssignmentStudentSerializerWithStats
            data.append(serializer_class(assignment, context=context).data)
        return Response(data)

    @extend_schema(
        responses={(200, 'text/csv'): OpenApiTypes.STR},
        parameters=[
            OpenApiParameter(name='assignments', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY,
                             required=False,
                             description="Comma-separated assignment ids to include; omit for all."),
            OpenApiParameter(name='quizzes', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY,
                             required=False,
                             description="Comma-separated quiz ids to include; omit for all."),
            OpenApiParameter(name='section', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY,
                             required=False,
                             description="Restrict rows to students in this section."),
        ],
    )
    @action(detail=True, methods=["GET"])
    def gradebookExport(self, request, pk=None):
        """Export the course gradebook as CSV (course admins only). Same data as the
        gradebook endpoint: one row per active student, blank cells for pending/missing.
        Totals are computed over the included columns only."""
        import csv
        from django.http import HttpResponse
        from core.services.gradebook import build_gradebook

        course = self.get_object()
        if not (request.user.is_superuser or isCourseAdmin(request.user, course)):
            return returnForbidden()

        def id_set(name):
            # Absent → None (all); present → the listed ids (possibly none).
            if name not in request.query_params:
                return None
            raw = request.query_params.get(name) or ''
            return {int(x) for x in raw.split(',') if x.strip().isdigit()}

        data = build_gradebook(course,
                               assignment_ids=id_set('assignments'),
                               quiz_ids=id_set('quizzes'),
                               section=request.query_params.get('section') or None)

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="gradebook_course_{course.id}.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ['Student', 'Section']
            + [f"{a['name']} ({a['points']})" for a in data['assignments']]
            + [f"Quiz: {q['title']}" for q in data['quizzes']]
            + ['Total Earned', 'Total Possible', 'Percent'])
        for row in data['rows']:
            writer.writerow(
                [row['student'], row['section'] or '']
                + [('' if c['grade'] is None else str(c['grade'])) for c in row['assignmentCells']]
                + [('' if c['score'] is None else str(c['score'])) for c in row['quizCells']]
                + [str(row['totalEarned']), str(row['totalPossible']),
                   '' if row['percent'] is None else str(row['percent'])])
        return response

    @extend_schema(responses=QuestionSerializer(many=True))
    @action(detail=True, methods=["GET"])
    def questions(self, request, pk=None):
        """List the course's quiz questions (staff only)."""
        course = self.get_object()
        if not (request.user.is_superuser or isCourseStaff(request.user, course)):
            return returnForbidden()
        questions = course.questions.prefetch_related("choices").all()
        return Response(QuestionSerializer(questions, many=True, context={"request": request}).data)


def add_admin_privileges(user):
    user.profile.canCreateCourses = True
    user.profile.canModifyRosters = True
    user.save()


def get_roster_permission_errors(user, request, course):
    if not isAuthenticated(user):
        return returnNotAuthorized()
    if request.method == "GET":
        # Full roster access requires admin or supergrader — broader than view_roster
        # capability (which includes regular graders for lighter UI checks).
        if not (isCourseAdmin(user, course) or isSuperGrader(user, course) or user.is_superuser):
            return returnForbidden()
    elif request.method == "PATCH":
        if not (user.is_superuser or (check_capability(user, 'manage_roster', course) and user.profile.canModifyRosters)):
            return returnForbidden()
    return False


def parse_new_users(keyEl, request, userList, auto_activate=False):
    if keyEl in request.data:
        # There are two ways to specify a list of values in a request
        # Type 1: request['graders'] = firstEmail, request['graders'] = secondEmail
        # Type 2: request['graders'] = [firstEmail, secondEmail]
        # If Type 1, then .get returns a string (the last value corresponding to the key), and we need to use request.data.getlist
        # If Type 2, then .get returns an array
        # If there are other valid types that we don't currently handle, this view will return a 500 error (thisData won't be defined),
        # which should allow us to figure them out.
        if type(request.data[keyEl]) is str:
            thisData = request.data.getlist(keyEl)
        elif type(request.data[keyEl]) is list:
            thisData = request.data.get(keyEl)

        for email in thisData:
            # Add any newly created users to the creator's
            # organization
            newUser = get_or_create_user(email, request.user.profile.organization, auto_activate=auto_activate)
            userList.append(newUser)
