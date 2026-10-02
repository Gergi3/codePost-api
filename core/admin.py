# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import json

from django.contrib import admin, messages
from django.db.models import Count, Q, F
from django.utils.html import format_html
from django.urls import reverse, path
from django.utils.safestring import mark_safe
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.utils import timezone
from typing import Any, Optional

from core.models import (
    AIUsageRecord,
    Assignment,
    AssignmentDataSet,
    AssignmentFile,
    AutograderExecutionEvent,
    CachedExecutionResult,
    Comment,
    CommentTag,
    CommentTemplate,
    Course,
    CourseAPIKey,
    CourseAuditEvent,
    CourseFile,
    CourseFileContent,
    Environment,
    File,
    FileTemplate,
    GeneratedQuestionSet,
    GeneratedQuizQuestion,
    LearningObjective,
    MaintenanceBanner,
    OneTimeToken,
    Organization,
    PendingAgentAction,
    Profile,
    PromptExperiment,
    PromptFeedback,
    PromptLabSettings,
    Question,
    QuestionBank,
    QuestionChoice,
    Quiz,
    QuizAccommodation,
    QuizAttempt,
    QuizGeneratedSection,
    QuizImage,
    QuizImportJob,
    QuizQuestion,
    QuizQuestionGroup,
    QuizResponse,
    QuizSebLaunch,
    QuizSuggestionJob,
    RubricCategory,
    RubricComment,
    Section,
    StudentDataSetAssignment,
    Submission,
    SubmissionFile,
    SubmissionFileEdit,
    SubmissionHistory,
    SubmissionSummary,
    SubmissionTest,
    SubmissionVariantRun,
    SuggestedComment,
    SuggestedQuizQuestion,
    SystemPromptVariant,
    TestCase,
    TestCategory,
    TestCategoryResource,
)

# ============================================================================
# Site Configuration
# ============================================================================

admin.site.site_header = "codePost Administration"
admin.site.site_title = "codePost Admin"
admin.site.index_title = mark_safe(
    '<span style="font-weight:600;">Welcome to codePost Administration</span>'
    '<span style="color:#7a7a7a; margin-left:8px; font-weight:400;">|</span>'
    '<span style="margin-left:8px; color:#6b7280; font-size:12px;">'
    '<a href="/docs/" target="_blank" rel="noopener">Docs</a>'
    '<span style="margin:0 6px; color:#9ca3af;">·</span>'
    '<a href="/api/schema/swagger-ui/" target="_blank" rel="noopener">Swagger</a>'
    '<span style="margin:0 6px; color:#9ca3af;">·</span>'
    '<a href="/api/schema/elements/" target="_blank" rel="noopener">Elements</a>'
    '<span style="margin:0 6px; color:#9ca3af;">·</span>'
    '<a href="/api/schema/" target="_blank" rel="noopener">JSON</a>'
    '<span style="margin:0 6px; color:#9ca3af;">·</span>'
    '<a href="/api/schema/yaml/" target="_blank" rel="noopener">YAML</a>'
    '</span>'
)
admin.site.site_url = "/docs/"


# ============================================================================
# Custom Admin Classes
# ============================================================================


class AutograderEnabledFilter(admin.SimpleListFilter):
    title = "autograder configured"
    parameter_name = "autograder_configured"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Yes"), ("no", "No")]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "yes":
            return queryset.filter(testCategories__isnull=False).distinct()
        if value == "no":
            return queryset.filter(testCategories__isnull=True)
        return queryset


class HasResourcesFilter(admin.SimpleListFilter):
    title = "has resources"
    parameter_name = "has_resources"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Yes"), ("no", "No")]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "yes":
            return queryset.filter(resources__isnull=False).distinct()
        if value == "no":
            return queryset.filter(resources__isnull=True)
        return queryset


class HasTestCasesFilter(admin.SimpleListFilter):
    title = "has test cases"
    parameter_name = "has_test_cases"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Yes"), ("no", "No")]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "yes":
            return queryset.filter(testCases__isnull=False).distinct()
        if value == "no":
            return queryset.filter(testCases__isnull=True)
        return queryset


class NeverRunTestFilter(admin.SimpleListFilter):
    title = "solution run status"
    parameter_name = "solution_run_status"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [("never", "Never run"), ("ran", "Has run")]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "never":
            return queryset.filter(lastSolutionRun=3)
        if value == "ran":
            return queryset.exclude(lastSolutionRun=3)
        return queryset


class AssignmentDueDateFilter(admin.SimpleListFilter):
    title = "upload due date"
    parameter_name = "upload_due_status"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [
            ("none", "No due date"),
            ("set", "Has due date"),
            ("past", "Past due"),
            ("future", "Due in future"),
        ]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        now = timezone.now()
        if value == "none":
            return queryset.filter(uploadDueDate__isnull=True)
        if value == "set":
            return queryset.filter(uploadDueDate__isnull=False)
        if value == "past":
            return queryset.filter(uploadDueDate__isnull=False, uploadDueDate__lt=now)
        if value == "future":
            return queryset.filter(uploadDueDate__isnull=False, uploadDueDate__gte=now)
        return queryset


class NeedsGradingFilter(admin.SimpleListFilter):
    title = "needs grading"
    parameter_name = "needs_grading"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Yes"), ("no", "No")]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "yes":
            return queryset.filter(Q(isFinalized=False) | Q(grade__isnull=True))
        if value == "no":
            return queryset.filter(isFinalized=True, grade__isnull=False)
        return queryset


class HasGraderFilter(admin.SimpleListFilter):
    title = "has grader"
    parameter_name = "has_grader"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Yes"), ("no", "No")]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "yes":
            return queryset.filter(grader__isnull=False)
        if value == "no":
            return queryset.filter(grader__isnull=True)
        return queryset


class CachedExecutionScopeFilter(admin.SimpleListFilter):
    title = "execution scope"
    parameter_name = "execution_scope"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [
            ("submission", "Submission"),
            ("assignment", "Assignment"),
            ("course", "Course"),
            ("unknown", "Unknown"),
        ]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "submission":
            return queryset.filter(file__submissionfile__isnull=False)
        if value == "assignment":
            return queryset.filter(file__assignmentfile__isnull=False)
        if value == "course":
            return queryset.filter(file__coursefile__isnull=False)
        if value == "unknown":
            return queryset.filter(
                file__submissionfile__isnull=True,
                file__assignmentfile__isnull=True,
                file__coursefile__isnull=True,
            )
        return queryset


class CommentRubricLinkFilter(admin.SimpleListFilter):
    title = "rubric link"
    parameter_name = "rubric_link"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [("linked", "Linked"), ("unlinked", "Unlinked")]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "linked":
            return queryset.filter(rubricComment__isnull=False)
        if value == "unlinked":
            return queryset.filter(rubricComment__isnull=True)
        return queryset


class CommentFeedbackFilter(admin.SimpleListFilter):
    title = "feedback score"
    parameter_name = "feedback_score"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [("positive", "Positive"), ("negative", "Negative"), ("neutral", "Neutral")]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "positive":
            return queryset.filter(feedback__gt=0)
        if value == "negative":
            return queryset.filter(feedback__lt=0)
        if value == "neutral":
            return queryset.filter(feedback=0)
        return queryset


class SubmissionFileHasCommentsFilter(admin.SimpleListFilter):
    title = "has comments"
    parameter_name = "has_comments"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Yes"), ("no", "No")]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "yes":
            return queryset.filter(comments__isnull=False).distinct()
        if value == "no":
            return queryset.filter(comments__isnull=True)
        return queryset


class FileHasPathFilter(admin.SimpleListFilter):
    title = "has path"
    parameter_name = "has_path"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Yes"), ("no", "No")]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "yes":
            return queryset.exclude(path__isnull=True).exclude(path="")
        if value == "no":
            return queryset.filter(Q(path__isnull=True) | Q(path=""))
        return queryset


class FileScopeFilter(admin.SimpleListFilter):
    title = "file scope"
    parameter_name = "file_scope"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [
            ("submission", "Submission"),
            ("assignment", "Assignment"),
            ("course", "Course"),
            ("orphan", "Orphan/Unknown"),
        ]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "submission":
            return queryset.filter(submissionfile__isnull=False)
        if value == "assignment":
            return queryset.filter(assignmentfile__isnull=False)
        if value == "course":
            return queryset.filter(coursefile__isnull=False)
        if value == "orphan":
            return queryset.filter(
                submissionfile__isnull=True,
                assignmentfile__isnull=True,
                coursefile__isnull=True,
            )
        return queryset


class AuthOneTimeToken(OneTimeToken):
    """Proxy model so OneTimeToken appears under the 'Auth Token' admin app."""

    class Meta:
        proxy = True
        app_label = "authtoken"
        verbose_name = "One-time token"
        verbose_name_plural = "One-time tokens"


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("shortname", "name", "is_main_org", "sso_enabled", "profile_count", "course_count", "created", "modified")
    search_fields = ("name", "shortname")
    list_filter = ("is_main_org", "sso_enabled", "created", "modified")
    ordering = ("name",)
    readonly_fields = ("created", "modified")
    
    fieldsets = (
        (None, {
            "fields": ("name", "shortname", "is_main_org")
        }),
        ("Email", {
            "fields": ("email_domain", "allowed_email_domains", "send_welcome_email")
        }),
        ("SSO", {
            "fields": ("sso_enabled", "sso_provider", "sso_config")
        }),
        ("Metadata", {
            "fields": ("created", "modified")
        }),
    )
    
    def profile_count(self, obj: Organization) -> int:
        """Number of profiles in this organization"""
        return obj.profiles.count()
    profile_count.short_description = "Profiles"
    
    def course_count(self, obj: Organization) -> int:
        """Number of courses in this organization"""
        return obj.courses.count()
    course_count.short_description = "Courses"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.prefetch_related("profiles", "courses")


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user_email", "organization", "can_create_courses", "can_modify_rosters", "is_org_staff",
                    "pending_validation", "is_password_set", "created")
    search_fields = ("user__email", "user__username", "organization__name")
    list_filter = ("canCreateCourses", "canModifyRosters", "pendingValidation", "isOrgStaff", "isPasswordSet", "organization", "created")
    readonly_fields = ("api_token", "created", "modified")
    autocomplete_fields = ["user", "organization"]
    
    fieldsets = (
        ("User Information", {
            "fields": ("user", "organization", "api_token")
        }),
        ("Permissions", {
            "fields": ("canCreateCourses", "canModifyRosters", "isOrgStaff", "pendingValidation")
        }),
        ("Settings", {
            "fields": ("showProductTips", "isPasswordSet", "stripeCustomerId")
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    def user_email(self, obj: Profile) -> str:
        """Display user email"""
        return obj.user.email
    user_email.short_description = "Email"
    user_email.admin_order_field = "user__email"
    
    def can_create_courses(self, obj: Profile) -> bool:
        return obj.canCreateCourses
    can_create_courses.short_description = "Can Create Courses"
    can_create_courses.boolean = True
    
    def can_modify_rosters(self, obj: Profile) -> bool:
        return obj.canModifyRosters
    can_modify_rosters.short_description = "Can Modify Rosters"
    can_modify_rosters.boolean = True

    def is_org_staff(self, obj: Profile) -> bool:
        return obj.isOrgStaff
    is_org_staff.short_description = "Org Staff"
    is_org_staff.boolean = True
    
    def pending_validation(self, obj: Profile) -> bool:
        return obj.pendingValidation
    pending_validation.short_description = "Pending"
    pending_validation.boolean = True
    
    def is_password_set(self, obj: Profile) -> bool:
        return obj.isPasswordSet
    is_password_set.short_description = "Password Set"
    is_password_set.boolean = True
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("user", "organization")


@admin.register(AuthOneTimeToken)
class OneTimeTokenAdmin(admin.ModelAdmin):
    list_display = ("token_preview", "user_email", "is_valid_status", "used", "expires_at", "created")
    search_fields = ("user__email", "user__username", "token")
    list_filter = ("used", "created", "expires_at")
    readonly_fields = ("token", "user", "expires_at", "created", "modified", "copy_token_button")
    
    fieldsets = (
        ("Token Information", {
            "fields": ("user", "token", "copy_token_button")
        }),
        ("Status", {
            "fields": ("used", "expires_at")
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    actions = ["generate_new_tokens"]
    
    def has_add_permission(self, request: Any) -> bool:
        """Disable add form - tokens should be generated via the action"""
        return False
    
    def changelist_view(self, request: Any, extra_context: Any = None) -> Any:
        """Add custom button to changelist"""
        extra_context = extra_context or {}
        extra_context['generate_token_url'] = reverse('admin:generate_token')
        return super().changelist_view(request, extra_context)
    
    def get_urls(self):
        """Add custom URL for generating token"""
        urls = super().get_urls()
        custom_urls = [
            path('generate-token/', self.admin_site.admin_view(self.generate_token_view), name='generate_token'),
        ]
        return custom_urls + urls
    
    def generate_token_view(self, request: Any) -> HttpResponseRedirect:
        """View to generate a new token for the current user"""
        from django.contrib import messages
        
        token = OneTimeToken.objects.create(user=request.user)
        messages.success(request, format_html(
            'New token generated: <strong style="font-family: monospace; background: #f0f0f0; padding: 2px 6px;">{}</strong>',
            token.token
        ))
        return redirect(f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_changelist")
    
    def token_preview(self, obj: AuthOneTimeToken) -> str:
        """Display first and last 8 characters of token"""
        token_str = str(obj.token)
        if len(token_str) > 20:
            return f"{token_str[:8]}...{token_str[-8:]}"
        return token_str
    token_preview.short_description = "Token"
    
    def user_email(self, obj: AuthOneTimeToken) -> str:
        """Display user email"""
        return obj.user.email
    user_email.short_description = "User"
    user_email.admin_order_field = "user__email"
    
    def is_valid_status(self, obj: AuthOneTimeToken) -> bool:
        """Check if token is still valid"""
        return obj.is_valid()
    is_valid_status.short_description = "Valid"
    is_valid_status.boolean = True
    
    def copy_token_button(self, obj: AuthOneTimeToken) -> str:
        """Display a copyable token field"""
        if obj.token:
            return format_html(
                '<div style="font-family: monospace; background: #f5f5f5; padding: 10px; border: 1px solid #ddd; border-radius: 4px;">'
                '<div style="margin-bottom: 5px;"><strong>Token:</strong></div>'
                '<input type="text" value="{}" readonly style="width: 100%; padding: 5px; font-family: monospace;" '
                'onclick="this.select(); document.execCommand(\'copy\'); alert(\'Token copied to clipboard!\');" />'
                '<div style="margin-top: 5px; font-size: 11px; color: #666;">Click to select and copy</div>'
                '</div>',
                obj.token
            )
        return "-"
    copy_token_button.short_description = "Copy Token"
    
    def generate_new_tokens(self, request: Any, queryset: Any) -> None:
        """Generate a new token for the current user (admin)"""
        # Create a new token for the logged-in admin user
        token = OneTimeToken.objects.create(user=request.user)
        self.message_user(request, f"New token generated for {request.user.email}: {token.token}")
    generate_new_tokens.short_description = "Generate token for me (current admin user)"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("user")


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("name", "period", "organization", "student_count", "grader_count", 
                    "admin_count", "assignment_count", "is_archived", "created")
    search_fields = ("name", "period", "organization__name")
    list_filter = ("archived", "organization", "created", "modified")
    readonly_fields = ("id", "created", "modified")
    autocomplete_fields = ["organization"]
    search_help_text = "Search by course name, period, or organization."
    list_select_related = ("organization",)
    list_per_page = 50
    show_full_result_count = False
    filter_horizontal = ("students", "graders", "courseAdmins", "superGraders",
                        "inactive_students", "inactive_graders", "inactive_courseAdmins")
    
    fieldsets = (
        ("Basic Information", {
            "fields": ("id", "name", "period", "organization", "archived")
        }),
        ("Core Settings", {
            "fields": ("timezone", "activateQueue", "anonymousGradingDefault")
        }),
        ("Advanced Course Settings", {
            "fields": (
                "sendReleasedSubmissionsToBack",
                "showStudentsStatistics",
                "emailNewUsers",
                "allowGradersToEditRubric",
                "minComments",
                "noUnfinalize",
            ),
            "classes": ("collapse",)
        }),
        ("Student Management", {
            "fields": ("rosterMap", "emailWhitelist", "lateDayCreditsAllowable", 
                      "studentCaptions", "useStudentCaptions", "inviteCode", "inviteCodeEnabled"),
            "classes": ("collapse",)
        }),
        ("Active Members", {
            "fields": ("students", "graders", "courseAdmins", "superGraders"),
            "classes": ("collapse",)
        }),
        ("Inactive Members", {
            "fields": ("inactive_students", "inactive_graders", "inactive_courseAdmins"),
            "classes": ("collapse",)
        }),
        ("Notifications", {
            "fields": ("enableStudentFeedbackNotifications",),
            "classes": ("collapse",)
        }),
        ("Payments & Waivers", {
            "fields": ("manual_payments", "waiver_requested"),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    actions = ["archive_courses", "unarchive_courses"]
    
    def student_count(self, obj: Course) -> int:
        return getattr(obj, "student_count_annotated", obj.students.count())
    student_count.short_description = "Students"
    
    def grader_count(self, obj: Course) -> int:
        return getattr(obj, "grader_count_annotated", obj.graders.count())
    grader_count.short_description = "Graders"
    
    def admin_count(self, obj: Course) -> int:
        return getattr(obj, "admin_count_annotated", obj.courseAdmins.count())
    admin_count.short_description = "Admins"
    
    def assignment_count(self, obj: Course) -> int:
        return getattr(obj, "assignment_count_annotated", obj.assignments.count())
    assignment_count.short_description = "Assignments"
    
    def is_archived(self, obj: Course) -> bool:
        return obj.archived
    is_archived.short_description = "Archived"
    is_archived.boolean = True
    
    def archive_courses(self, request: Any, queryset: Any) -> None:
        """Archive selected courses"""
        updated = queryset.update(archived=True)
        self.message_user(request, f"{updated} course(s) archived.")
    archive_courses.short_description = "Archive selected courses"
    
    def unarchive_courses(self, request: Any, queryset: Any) -> None:
        """Unarchive selected courses"""
        updated = queryset.update(archived=False)
        self.message_user(request, f"{updated} course(s) unarchived.")
    unarchive_courses.short_description = "Unarchive selected courses"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("organization").annotate(
            student_count_annotated=Count("students", distinct=True),
            grader_count_annotated=Count("graders", distinct=True),
            admin_count_annotated=Count("courseAdmins", distinct=True),
            assignment_count_annotated=Count("assignments", distinct=True),
        )


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ("name", "course", "points", "state",
                    "submission_count", "test_category_count", "test_case_count", "upload_due_date", "mean_grade", "open_submissions", "open_tests", "created")
    search_fields = ("name", "course__name", "course__period")
    list_filter = ("state", "feedbackStatus", "allowStudentUpload", "anonymousGrading", AutograderEnabledFilter, AssignmentDueDateFilter,
                   "uploadDueDate", "created", "modified")
    readonly_fields = ("course", "publishedAt", "scheduledPublishRanAt", "scheduledFeedbackReleaseRanAt", "mean", "median", "created", "modified")
    autocomplete_fields = ["course"]
    list_select_related = ("course",)
    list_per_page = 50
    show_full_result_count = False
    search_help_text = "Search by assignment name, course name, or course period."
    date_hierarchy = "created"
    ordering = ("-uploadDueDate", "-created")
    
    fieldsets = (
        ("Basic Information", {
            "fields": ("name", "course", "points", "sortKey", "explanation")
        }),
        ("Lifecycle", {
            "fields": ("state", "publishAt", "publishedAt", "scheduledPublishRanAt",
                       "feedbackStatus", "releaseFeedbackAt", "scheduledFeedbackReleaseRanAt",
                       "hideGrades", "anonymousGrading"),
        }),
        ("Student Upload Settings", {
            "fields": ("allowStudentUpload", "allowStudentUploadWithPartners", 
                      "uploadDueDate", "maxLateDays", "allowLateUploads"),
            "classes": ("collapse",)
        }),
        ("Grading Settings", {
            "fields": ("additiveGrading", "commentFeedback"),
            "classes": ("collapse",)
        }),
        ("Statistics", {
            "fields": ("mean", "median"),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    actions = ["release_assignments", "hide_assignments", "enable_student_upload"]
    
    def submission_count(self, obj: Assignment) -> int:
        return getattr(obj, "submission_count_annotated", obj.submissions.count())
    submission_count.short_description = "Submissions"

    def test_category_count(self, obj: Assignment) -> int:
        return getattr(obj, "test_category_count_annotated", obj.testCategories.count())
    test_category_count.short_description = "Test Categories"

    def test_case_count(self, obj: Assignment) -> int:
        return getattr(
            obj,
            "test_case_count_annotated",
            TestCase.objects.filter(testCategory__assignment=obj).count(),
        )
    test_case_count.short_description = "Test Cases"
    
    def mean_grade(self, obj: Assignment) -> Optional[str]:
        if obj.mean:
            return f"{obj.mean:.2f}"
        return "-"
    mean_grade.short_description = "Mean"
    mean_grade.admin_order_field = "mean"

    def upload_due_date(self, obj: Assignment) -> Any:
        return obj.uploadDueDate
    upload_due_date.short_description = "Upload Due"
    upload_due_date.admin_order_field = "uploadDueDate"

    def get_ordering(self, request: Any) -> Any:
        """Default to latest due date first, pushing null due dates to the bottom."""
        return [F("uploadDueDate").desc(nulls_last=True), "-created"]
    
    def open_submissions(self, obj: Assignment) -> str:
        url = f"{reverse('admin:core_submission_changelist')}?assignment__id__exact={obj.id}"
        return format_html('<a href="{}">View Submissions</a>', url)
    open_submissions.short_description = "Submissions"

    def open_tests(self, obj: Assignment) -> str:
        url = f"{reverse('admin:core_testcategory_changelist')}?assignment__id__exact={obj.id}"
        return format_html('<a href="{}">View Test Categories</a>', url)
    open_tests.short_description = "Tests"
    
    def release_assignments(self, request: Any, queryset: Any) -> None:
        """Publish selected assignments. Loops with save() (not queryset.update) so the
        lifecycle sync, publishedAt stamp, and quiz-deadline signals all run."""
        updated = 0
        for assignment in queryset:
            assignment.state = 'published'
            assignment.save()
            updated += 1
        self.message_user(request, f"{updated} assignment(s) published.")
    release_assignments.short_description = "Publish selected assignments"

    def hide_assignments(self, request: Any, queryset: Any) -> None:
        """Move selected assignments back to draft (hidden from students)."""
        updated = 0
        for assignment in queryset:
            assignment.state = 'draft'
            assignment.save()
            updated += 1
        self.message_user(request, f"{updated} assignment(s) moved to draft.")
    hide_assignments.short_description = "Hide selected assignments (draft)"
    
    def enable_student_upload(self, request: Any, queryset: Any) -> None:
        """Enable student upload for selected assignments"""
        updated = queryset.update(allowStudentUpload=True)
        self.message_user(request, f"Student upload enabled for {updated} assignment(s).")
    enable_student_upload.short_description = "Enable student upload"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("course").annotate(
            submission_count_annotated=Count("submissions", distinct=True),
            test_category_count_annotated=Count("testCategories", distinct=True),
            test_case_count_annotated=Count("testCategories__testCases", distinct=True),
        )


@admin.register(Section)
class SectionAdmin(admin.ModelAdmin):
    list_display = ("name", "course", "leader_count", "student_count", "created")
    search_fields = ("name", "course__name")
    list_filter = ("course", "created")
    readonly_fields = ("created", "modified")
    autocomplete_fields = ["course"]
    search_help_text = "Search by section name or course name."
    date_hierarchy = "created"
    filter_horizontal = ("leaders", "students")
    
    fieldsets = (
        ("Basic Information", {
            "fields": ("name", "course")
        }),
        ("Members", {
            "fields": ("leaders", "students")
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    
    def leader_count(self, obj: Section) -> int:
        return obj.leaders.count()
    leader_count.short_description = "Leaders"
    
    def student_count(self, obj: Section) -> int:
        return obj.students.count()
    student_count.short_description = "Students"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("course").prefetch_related("leaders", "students")


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "assignment",
        "students_list",
        "grader",
        "grade_display",
        "is_finalized",
        "has_question",
        "date_uploaded",
        "open_files",
        "open_tests",
    )
    search_fields = ("id", "assignment__name", "students__email", "grader__email")
    list_filter = ("isFinalized", NeedsGradingFilter, HasGraderFilter, "questionIsOpen", "questionIsRegrade", 
                   "assignment__course", "created")
    readonly_fields = ("assignment", "dateEdited", "dateUploaded", "created", "modified")
    autocomplete_fields = ["assignment", "grader", "questionResponder"]
    list_select_related = ("assignment", "grader")
    list_per_page = 75
    show_full_result_count = False
    filter_horizontal = ("students",)
    
    fieldsets = (
        ("Assignment Information", {
            "fields": ("assignment", "students", "grader")
        }),
        ("Grading", {
            "fields": ("isFinalized", "grade")
        }),
        ("Advanced Grading Controls", {
            "fields": ("gradeFrozen", "queueOrderKey"),
            "classes": ("collapse",)
        }),
        ("Late Submission", {
            "fields": ("lateDayCreditsUsed",),
            "classes": ("collapse",)
        }),
        ("Question/Regrade", {
            "fields": ("questionIsOpen", "questionIsRegrade", "questionText", 
                      "questionResponse", "questionResponder", "questionDate", "responseDate"),
            "classes": ("collapse",)
        }),
        ("Testing", {
            "fields": ("testRunsCompleted",),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("dateUploaded", "dateEdited", "created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    actions = ["finalize_submissions", "unfinalize_submissions", "close_open_questions"]
    date_hierarchy = "created"
    
    def students_list(self, obj: Submission) -> str:
        emails = [s.email for s in obj.students.all()[:3]]
        count = obj.students.count()
        if count > 3:
            return ", ".join(emails) + f" (+{count - 3} more)"
        return ", ".join(emails)
    students_list.short_description = "Students"
    
    def is_finalized(self, obj: Submission) -> bool:
        return obj.isFinalized
    is_finalized.short_description = "Finalized"
    is_finalized.boolean = True

    def grade_display(self, obj: Submission) -> str:
        return f"{obj.grade:.2f}" if obj.grade is not None else "-"
    grade_display.short_description = "Grade"
    grade_display.admin_order_field = "grade"
    
    def has_question(self, obj: Submission) -> bool:
        return obj.questionIsOpen
    has_question.short_description = "Question"
    has_question.boolean = True
    
    def date_uploaded(self, obj: Submission) -> Any:
        return obj.dateUploaded
    date_uploaded.short_description = "Uploaded"
    date_uploaded.admin_order_field = "dateUploaded"

    def open_files(self, obj: Submission) -> str:
        url = f"{reverse('admin:core_submissionfile_changelist')}?submission__id__exact={obj.id}"
        return format_html('<a href="{}">Files</a>', url)
    open_files.short_description = "Files"

    def open_tests(self, obj: Submission) -> str:
        url = f"{reverse('admin:core_submissiontest_changelist')}?submission__id__exact={obj.id}"
        return format_html('<a href="{}">Tests</a>', url)
    open_tests.short_description = "Tests"
    
    def finalize_submissions(self, request: Any, queryset: Any) -> None:
        """Finalize selected submissions"""
        updated = queryset.update(isFinalized=True)
        self.message_user(request, f"{updated} submission(s) finalized.")
    finalize_submissions.short_description = "Finalize selected submissions"
    
    def unfinalize_submissions(self, request: Any, queryset: Any) -> None:
        """Unfinalize selected submissions"""
        updated = queryset.update(isFinalized=False)
        self.message_user(request, f"{updated} submission(s) unfinalized.")
    unfinalize_submissions.short_description = "Unfinalize selected submissions"

    def close_open_questions(self, request: Any, queryset: Any) -> None:
        """Mark open student questions as closed"""
        updated = queryset.filter(questionIsOpen=True).update(questionIsOpen=False)
        self.message_user(request, f"Closed {updated} open question(s).")
    close_open_questions.short_description = "Close open student questions"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("assignment", "grader").prefetch_related("students")


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "assignment_name",
        "submission_id",
        "file",
        "author",
        "rubric_status",
        "point_delta",
        "feedback_score",
        "text_preview",
        "open_submission",
        "created",
    )
    search_fields = ("text", "author__email", "file__submission__id", "file__submission__assignment__name", "file__name")
    list_filter = (CommentRubricLinkFilter, CommentFeedbackFilter, "created", "rubricComment", "author", "file__submission__assignment")
    readonly_fields = ("file", "author", "rubricComment", "created", "modified")
    autocomplete_fields = ["file", "author", "rubricComment"]
    search_help_text = "Search by comment text, author email, file name, submission ID, or assignment."
    list_select_related = ("file", "author", "rubricComment")
    list_per_page = 100
    show_full_result_count = False
    date_hierarchy = "created"
    filter_horizontal = ("tags",)
    
    fieldsets = (
        ("Location", {
            "fields": ("file", "startLine", "endLine", "startChar", "endChar")
        }),
        ("Content", {
            "fields": ("text", "pointDelta", "rubricComment", "author")
        }),
        ("Appearance", {
            "fields": ("color", "tags"),
            "classes": ("collapse",)
        }),
        ("Feedback", {
            "fields": ("feedback",),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    def text_preview(self, obj: Comment) -> str:
        return obj.text[:50] + "..." if len(obj.text) > 50 else obj.text
    text_preview.short_description = "Text"
    
    def point_delta(self, obj: Comment) -> Optional[str]:
        if obj.pointDelta is not None:
            return f"{obj.pointDelta:+.2f}"
        return "-"
    point_delta.short_description = "Points"
    point_delta.admin_order_field = "pointDelta"

    def feedback_score(self, obj: Comment) -> int:
        return obj.feedback
    feedback_score.short_description = "Feedback"
    feedback_score.admin_order_field = "feedback"

    def rubric_status(self, obj: Comment) -> str:
        return "Linked" if obj.rubricComment else "Unlinked"
    rubric_status.short_description = "Rubric"

    def assignment_name(self, obj: Comment) -> str:
        return obj.file.submission.assignment.name
    assignment_name.short_description = "Assignment"
    assignment_name.admin_order_field = "file__submission__assignment__name"

    def submission_id(self, obj: Comment) -> int:
        return obj.file.submission.id
    submission_id.short_description = "Submission"
    submission_id.admin_order_field = "file__submission__id"

    def open_submission(self, obj: Comment) -> str:
        url = reverse("admin:core_submission_change", args=[obj.file.submission.id])
        return format_html('<a href="{}">Open Submission</a>', url)
    open_submission.short_description = "Submission"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("file", "file__submission", "file__submission__assignment", "author", "rubricComment")


@admin.register(CommentTag)
class CommentTagAdmin(admin.ModelAdmin):
    list_display = ("label", "comment_count", "created")
    search_fields = ("label",)
    list_filter = ("created",)
    readonly_fields = ("created", "modified")
    
    def comment_count(self, obj: CommentTag) -> int:
        return obj.tag_comments.count()
    comment_count.short_description = "Comments"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.prefetch_related("tag_comments")


@admin.register(CommentTemplate)
class CommentTemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "text_preview", "owner_email", "assignment", "is_global", "point_delta", "created")
    search_fields = ("text", "owner__email", "assignment__name", "rubricComment__name", "filePath")
    list_filter = ("isGlobal", "assignment__course", "created")
    readonly_fields = ("created", "modified")
    autocomplete_fields = ["owner", "assignment", "rubricComment", "sourceComment"]
    actions = ["mark_global", "mark_private"]
    list_select_related = ("owner", "assignment")
    list_per_page = 100
    show_full_result_count = False
    search_help_text = "Search templates by text, owner email, assignment, rubric comment name, or file path."
    date_hierarchy = "created"

    fieldsets = (
        ("Template", {
            "fields": ("assignment", "owner", "isGlobal", "text")
        }),
        ("Scoring & Linkage", {
            "fields": ("pointDelta", "rubricComment", "sourceComment")
        }),
        ("Optional Matching", {
            "fields": ("cellId", "filePath"),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )

    def text_preview(self, obj: CommentTemplate) -> str:
        return obj.text[:80] + "..." if len(obj.text) > 80 else obj.text
    text_preview.short_description = "Text"

    def owner_email(self, obj: CommentTemplate) -> str:
        return obj.owner.email
    owner_email.short_description = "Owner"
    owner_email.admin_order_field = "owner__email"

    def is_global(self, obj: CommentTemplate) -> bool:
        return obj.isGlobal
    is_global.short_description = "Global"
    is_global.boolean = True

    def point_delta(self, obj: CommentTemplate) -> str:
        if obj.pointDelta is None:
            return "-"
        return f"{obj.pointDelta:+.2f}"
    point_delta.short_description = "Points"
    point_delta.admin_order_field = "pointDelta"

    def mark_global(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(isGlobal=True)
        self.message_user(request, f"Marked {updated} template(s) as global.")
    mark_global.short_description = "Mark selected templates as global"

    def mark_private(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(isGlobal=False)
        self.message_user(request, f"Marked {updated} template(s) as private.")
    mark_private.short_description = "Mark selected templates as private"

    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("owner", "assignment", "assignment__course", "rubricComment", "sourceComment")


@admin.register(RubricCategory)
class RubricCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "assignment", "point_limit", "sort_key", "at_most_once", "created")
    search_fields = ("name", "assignment__name")
    list_filter = ("atMostOnce", "assignment__course", "created")
    readonly_fields = ("assignment", "created", "modified")
    autocomplete_fields = ["assignment"]
    search_help_text = "Search rubric categories by name or assignment."
    date_hierarchy = "created"
    
    fieldsets = (
        ("Basic Information", {
            "fields": ("assignment", "name", "helpText")
        }),
        ("Settings", {
            "fields": ("pointLimit", "sortKey", "atMostOnce")
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    def point_limit(self, obj: RubricCategory) -> str:
        return str(obj.pointLimit) if obj.pointLimit is not None else "-"
    point_limit.short_description = "Point Limit"
    point_limit.admin_order_field = "pointLimit"
    
    def sort_key(self, obj: RubricCategory) -> int:
        return obj.sortKey
    sort_key.short_description = "Sort"
    sort_key.admin_order_field = "sortKey"
    
    def at_most_once(self, obj: RubricCategory) -> bool:
        return obj.atMostOnce
    at_most_once.short_description = "At Most Once"
    at_most_once.boolean = True
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("assignment")


@admin.register(RubricComment)
class RubricCommentAdmin(admin.ModelAdmin):
    list_display = ("name_or_text", "assignment_name", "category", "point_delta", "usage_count", "sort_key", "open_usage", "created")
    search_fields = ("text", "name", "category__name")
    list_filter = ("category__assignment", "created")
    readonly_fields = ("created", "modified")
    autocomplete_fields = ["category"]
    list_select_related = ("category",)
    list_per_page = 100
    show_full_result_count = False
    search_help_text = "Search by rubric comment text/name or category."
    date_hierarchy = "created"
    
    fieldsets = (
        ("Content", {
            "fields": ("name", "text", "explanation", "pointDelta")
        }),
        ("Template Settings", {
            "fields": ("instructionText", "templateTextOn"),
            "classes": ("collapse",)
        }),
        ("Organization", {
            "fields": ("category", "sortKey")
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    def name_or_text(self, obj: RubricComment) -> str:
        if obj.name:
            return obj.name
        return obj.text[:50] + "..." if len(obj.text) > 50 else obj.text
    name_or_text.short_description = "Name/Text"
    
    def point_delta(self, obj: RubricComment) -> str:
        return f"{obj.pointDelta:+.2f}"
    point_delta.short_description = "Points"
    point_delta.admin_order_field = "pointDelta"
    
    def sort_key(self, obj: RubricComment) -> int:
        return obj.sortKey
    sort_key.short_description = "Sort"
    sort_key.admin_order_field = "sortKey"

    def assignment_name(self, obj: RubricComment) -> str:
        return obj.category.assignment.name
    assignment_name.short_description = "Assignment"
    assignment_name.admin_order_field = "category__assignment__name"

    def usage_count(self, obj: RubricComment) -> int:
        return getattr(obj, "comment_count_annotated", obj.comments.count())
    usage_count.short_description = "Used"

    def open_usage(self, obj: RubricComment) -> str:
        url = f"{reverse('admin:core_comment_changelist')}?rubricComment__id__exact={obj.id}"
        return format_html('<a href="{}">View Comments</a>', url)
    open_usage.short_description = "Usage"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("category", "category__assignment").annotate(
            comment_count_annotated=Count("comments", distinct=True)
        )


@admin.register(SubmissionHistory)
class SubmissionHistoryAdmin(admin.ModelAdmin):
    list_display = ("submission", "student", "has_viewed", "date_viewed", "created")
    search_fields = ("submission__id", "student__email")
    list_filter = ("hasViewed", "created")
    readonly_fields = ("submission", "student", "dateViewed", "created", "modified")
    autocomplete_fields = ["submission", "student"]
    search_help_text = "Search by submission ID or student email."
    date_hierarchy = "created"
    
    def has_viewed(self, obj: SubmissionHistory) -> bool:
        return obj.hasViewed
    has_viewed.short_description = "Viewed"
    has_viewed.boolean = True
    
    def date_viewed(self, obj: SubmissionHistory) -> Optional[Any]:
        return obj.dateViewed
    date_viewed.short_description = "Viewed At"
    date_viewed.admin_order_field = "dateViewed"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("submission", "student")


@admin.register(SubmissionFile)
class SubmissionFileAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "file_name",
        "assignment_name",
        "submission",
        "students_list",
        "comment_count",
        "hiddenBeforePublish",
        "open_submission",
        "open_comments",
        "created",
    )
    search_fields = ("name", "path", "submission__id", "submission__students__email", "submission__assignment__name")
    list_filter = ("hiddenBeforePublish", SubmissionFileHasCommentsFilter, "submission__assignment__course", "created")
    readonly_fields = ("submission", "created", "modified")
    autocomplete_fields = ["submission"]
    search_help_text = "Search by filename/path, submission ID, student email, or assignment."
    list_select_related = ("submission", "submission__assignment", "submission__assignment__course")
    list_per_page = 100
    show_full_result_count = False
    date_hierarchy = "created"
    actions = ["hide_before_publish", "show_before_publish"]
    
    fieldsets = (
        ("File Information", {
            "fields": ("name", "extension", "path")
        }),
        ("Submission", {
            "fields": ("submission", "hiddenBeforePublish")
        }),
        ("Content", {
            "fields": ("data",),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    def file_name(self, obj: SubmissionFile) -> str:
        return obj.name
    file_name.short_description = "File Name"
    file_name.admin_order_field = "name"
    
    def students_list(self, obj: SubmissionFile) -> str:
        if obj.submission:
            emails = [s.email for s in obj.submission.students.all()[:2]]
            count = obj.submission.students.count()
            if count > 2:
                return ", ".join(emails) + f" (+{count - 2} more)"
            return ", ".join(emails)
        return "-"
    students_list.short_description = "Students"

    def assignment_name(self, obj: SubmissionFile) -> str:
        return obj.submission.assignment.name
    assignment_name.short_description = "Assignment"
    assignment_name.admin_order_field = "submission__assignment__name"

    def comment_count(self, obj: SubmissionFile) -> int:
        return getattr(obj, "comment_count_annotated", obj.comments.count())
    comment_count.short_description = "Comments"

    def open_submission(self, obj: SubmissionFile) -> str:
        url = reverse("admin:core_submission_change", args=[obj.submission.id])
        return format_html('<a href="{}">Open Submission</a>', url)
    open_submission.short_description = "Submission"

    def open_comments(self, obj: SubmissionFile) -> str:
        url = f"{reverse('admin:core_comment_changelist')}?file__id__exact={obj.id}"
        return format_html('<a href="{}">View Comments</a>', url)
    open_comments.short_description = "Comments"

    def hide_before_publish(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(hiddenBeforePublish=True)
        self.message_user(request, f"Set hidden-before-publish on {updated} file(s).")
    hide_before_publish.short_description = "Hide selected files before publish"

    def show_before_publish(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(hiddenBeforePublish=False)
        self.message_user(request, f"Cleared hidden-before-publish on {updated} file(s).")
    show_before_publish.short_description = "Show selected files before publish"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("submission", "submission__assignment", "submission__assignment__course").prefetch_related("submission__students").annotate(
            comment_count_annotated=Count("comments", distinct=True)
        )


@admin.register(AssignmentFile)
class AssignmentFileAdmin(admin.ModelAdmin):
    list_display = ("id", "file_name", "assignment", "required", "hidden", "is_test_resource", "created", "open_assignment")
    search_fields = ("name", "path", "assignment__name", "assignment__course__name")
    list_filter = ("required", "hidden", "is_test_resource", "assignment__course", "created")
    readonly_fields = ("assignment", "created", "modified")
    autocomplete_fields = ["assignment"]
    search_help_text = "Search by filename/path, assignment, or course."
    list_select_related = ("assignment",)
    list_per_page = 100
    show_full_result_count = False
    date_hierarchy = "created"
    actions = ["mark_test_resource", "clear_test_resource", "hide_selected_files", "unhide_selected_files"]
    
    fieldsets = (
        ("File Information", {
            "fields": ("name", "extension", "path")
        }),
        ("Assignment", {
            "fields": ("assignment", "required", "description")
        }),
        ("Content", {
            "fields": ("data",),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    def file_name(self, obj: AssignmentFile) -> str:
        return obj.name
    file_name.short_description = "File Name"
    file_name.admin_order_field = "name"

    def open_assignment(self, obj: AssignmentFile) -> str:
        url = reverse("admin:core_assignment_change", args=[obj.assignment_id])
        return format_html('<a href="{}">Open Assignment</a>', url)
    open_assignment.short_description = "Assignment"

    def mark_test_resource(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(is_test_resource=True, hidden=True)
        self.message_user(request, f"Marked {updated} file(s) as test resources.")
    mark_test_resource.short_description = "Mark selected files as test resources"

    def clear_test_resource(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(is_test_resource=False)
        self.message_user(request, f"Cleared test resource flag for {updated} file(s).")
    clear_test_resource.short_description = "Clear test resource flag"

    def hide_selected_files(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(hidden=True)
        self.message_user(request, f"Hidden {updated} file(s).")
    hide_selected_files.short_description = "Hide selected files"

    def unhide_selected_files(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(hidden=False)
        self.message_user(request, f"Made {updated} file(s) visible.")
    unhide_selected_files.short_description = "Make selected files visible"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("assignment", "assignment__course")

@admin.register(CachedExecutionResult)
class CachedExecutionResultAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "file_name",
        "scope",
        "context_ref",
        "executed_by_email",
        "executed_at",
        "execution_time",
        "hash_short",
        "created",
    )
    search_fields = (
        "file__name",
        "executed_by__email",
        "file_content_hash",
        "file__submissionfile__submission__id",
        "file__submissionfile__submission__assignment__name",
        "file__assignmentfile__assignment__name",
        "file__coursefile__course__name",
    )
    list_filter = (CachedExecutionScopeFilter, "file__extension", "executed_by", "executed_at", "created")
    date_hierarchy = "executed_at"
    ordering = ("-executed_at",)
    list_select_related = ("file", "executed_by")
    list_per_page = 100
    show_full_result_count = False
    readonly_fields = ("file", "executed_by", "executed_at", "file_content_hash", 
                        "execution_time_seconds", "output_preview", "created", "modified")
    autocomplete_fields = ["file", "executed_by"]
    search_help_text = "Search by file, executor email, hash, submission ID, assignment, or course context."
    
    fieldsets = (
        ("File Information", {
            "fields": ("file", "file_content_hash")
        }),
        ("Execution Details", {
            "fields": ("executed_by", "executed_at", "execution_time_seconds")
        }),
        ("Output Data", {
            "fields": ("output_data", "output_preview"),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    def file_name(self, obj: CachedExecutionResult) -> str:
        return obj.file.name if obj.file else "-"
    file_name.short_description = "File Name"
    file_name.admin_order_field = "file__name"
    
    def executed_by_email(self, obj: CachedExecutionResult) -> str:
        return obj.executed_by.email if obj.executed_by else "-"
    executed_by_email.short_description = "Executed By"
    executed_by_email.admin_order_field = "executed_by__email"
    
    def execution_time(self, obj: CachedExecutionResult) -> str:
        if obj.execution_time_seconds is not None:
            return f"{obj.execution_time_seconds:.2f}s"
        return "-"
    execution_time.short_description = "Execution Time"
    execution_time.admin_order_field = "execution_time_seconds"

    def scope(self, obj: CachedExecutionResult) -> str:
        if hasattr(obj.file, "submissionfile"):
            return "Submission"
        if hasattr(obj.file, "assignmentfile"):
            return "Assignment"
        if hasattr(obj.file, "coursefile"):
            return "Course"
        return "Unknown"
    scope.short_description = "Scope"

    def context_ref(self, obj: CachedExecutionResult) -> str:
        try:
            if hasattr(obj.file, "submissionfile"):
                submission = obj.file.submissionfile.submission
                return f"Submission #{submission.id}"
            if hasattr(obj.file, "assignmentfile"):
                assignment = obj.file.assignmentfile.assignment
                return f"Assignment: {assignment.name}"
            if hasattr(obj.file, "coursefile"):
                course = obj.file.coursefile.course
                return f"Course: {course.name}"
        except Exception:
            pass
        return "-"
    context_ref.short_description = "Context"

    def hash_short(self, obj: CachedExecutionResult) -> str:
        hash_value = obj.file_content_hash or ""
        if len(hash_value) <= 12:
            return hash_value
        return f"{hash_value[:6]}...{hash_value[-6:]}"
    hash_short.short_description = "Hash"
    hash_short.admin_order_field = "file_content_hash"

    def output_preview(self, obj: CachedExecutionResult) -> str:
        from json import dumps

        if obj.output_data is None:
            return "-"

        preview = dumps(obj.output_data, indent=2, ensure_ascii=False)
        if len(preview) > 20000:
            preview = preview[:20000] + "\n... (truncated)"

        return format_html(
            '<pre style="max-height: 380px; overflow: auto; white-space: pre-wrap; margin: 0;">{}</pre>',
            preview,
        )
    output_preview.short_description = "Pretty Output Preview"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related(
            "file",
            "executed_by",
            "file__submissionfile__submission",
            "file__submissionfile__submission__assignment",
            "file__assignmentfile__assignment",
            "file__coursefile__course",
        )
    
@admin.register(AssignmentDataSet)
class AssignmentDataSetAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "assignment",
        "mount_path_short",
        "is_active",
        "hidden",
        "is_test_resource",
        "created",
        "open_assignment",
    )
    search_fields = ("name", "description", "mount_path", "assignment__name", "assignment__course__name")
    list_filter = ("is_active", "hidden", "is_test_resource", "assignment__course", "created")
    readonly_fields = ("assignment", "created", "modified")
    autocomplete_fields = ["assignment"]
    search_help_text = "Search by dataset name/description/mount path, assignment, or course."
    list_select_related = ("assignment",)
    list_per_page = 100
    show_full_result_count = False
    date_hierarchy = "created"
    actions = ["activate_selected", "deactivate_selected", "hide_selected", "unhide_selected"]
    
    fieldsets = (
        ("Data Set Information", {
            "fields": ("name", "assignment", "description", "file")
        }),
        ("Content", {
            "fields": ("mount_path", "is_active", "hidden", "is_test_resource"),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )

    def mount_path_short(self, obj: AssignmentDataSet) -> str:
        if not obj.mount_path:
            return "-"
        return obj.mount_path if len(obj.mount_path) <= 36 else obj.mount_path[:33] + "..."
    mount_path_short.short_description = "Mount Path"
    mount_path_short.admin_order_field = "mount_path"

    def open_assignment(self, obj: AssignmentDataSet) -> str:
        url = reverse("admin:core_assignment_change", args=[obj.assignment.id])
        return format_html('<a href="{}">Open Assignment</a>', url)
    open_assignment.short_description = "Assignment"

    def activate_selected(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(is_active=True)
        self.message_user(request, f"Activated {updated} dataset(s).")
    activate_selected.short_description = "Activate selected datasets"

    def deactivate_selected(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Deactivated {updated} dataset(s).")
    deactivate_selected.short_description = "Deactivate selected datasets"

    def hide_selected(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(hidden=True)
        self.message_user(request, f"Hidden {updated} dataset(s).")
    hide_selected.short_description = "Hide selected datasets"

    def unhide_selected(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(hidden=False)
        self.message_user(request, f"Made {updated} dataset(s) visible.")
    unhide_selected.short_description = "Make selected datasets visible"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("assignment", "assignment__course")
    
@admin.register(CourseFile)
class CourseFileAdmin(admin.ModelAdmin):
    list_display = ("id", "file_name", "course", "course_period", "is_public", "open_course", "created")
    search_fields = ("name", "path", "course__name", "course__period")
    list_filter = ("course", "created")
    readonly_fields = ("course", "content", "created", "modified")
    autocomplete_fields = ["course"]
    search_help_text = "Search by filename/path, course name, or period."
    list_select_related = ("course", "content")
    list_per_page = 100
    show_full_result_count = False
    date_hierarchy = "created"

    def file_name(self, obj: CourseFile) -> str:
        return obj.name
    file_name.short_description = "File Name"
    file_name.admin_order_field = "name"

    def course_period(self, obj: CourseFile) -> str:
        return obj.course.period
    course_period.short_description = "Period"
    course_period.admin_order_field = "course__period"

    def is_public(self, obj: CourseFile) -> bool:
        return obj.content.isPublic
    is_public.short_description = "Public"
    is_public.boolean = True
    is_public.admin_order_field = "content__isPublic"

    def open_course(self, obj: CourseFile) -> str:
        url = reverse("admin:core_course_change", args=[obj.course.id])
        return format_html('<a href="{}">Open Course</a>', url)
    open_course.short_description = "Course"

    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("course", "content")


@admin.register(CourseFileContent)
class CourseFileContentAdmin(admin.ModelAdmin):
    """Support view of shared course-file content: which courses share it, and its
    public token. Content is written via the copy-on-write service, not here."""
    list_display = ("id", "isPublic", "shared_count", "token", "created")
    readonly_fields = ("token", "created", "modified")
    exclude = ("data",)
    list_per_page = 100
    show_full_result_count = False

    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.annotate(_shared_count=Count("files"))

    def shared_count(self, obj: CourseFileContent) -> int:
        return obj._shared_count
    shared_count.short_description = "Sharing files"
    shared_count.admin_order_field = "_shared_count"


class TestCaseInline(admin.TabularInline):
    model = TestCase
    extra = 0
    fields = (
        "sortKey",
        "description",
        "type",
        "pointsPass",
        "pointsFail",
        "exposed",
        "lastSolutionRun",
        "rubricItem",
    )
    autocomplete_fields = ["rubricItem"]
    ordering = ("sortKey", "id")
    show_change_link = True


class TestCategoryResourceInline(admin.TabularInline):
    model = TestCategoryResource
    extra = 0
    fields = ("target_path", "file", "dataset")
    autocomplete_fields = ["file", "dataset"]


@admin.register(TestCategory)
class TestCategoryAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "assignment",
        "sort_key",
        "target_file",
        "max_points",
        "test_case_count",
        "resource_count",
        "open_test_cases",
        "open_resources",
        "created",
    )
    search_fields = ("name", "assignment__name")
    list_filter = ("assignment__course", HasTestCasesFilter, HasResourcesFilter, "created")
    readonly_fields = ("assignment", "created", "modified")
    autocomplete_fields = ["assignment"]
    search_help_text = "Search test categories by name or assignment."
    list_select_related = ("assignment",)
    list_per_page = 75
    show_full_result_count = False
    inlines = [TestCaseInline, TestCategoryResourceInline]
    date_hierarchy = "created"
    fieldsets = (
        ("Category", {
            "fields": ("assignment", "name", "sortKey", "targetFileName")
        }),
        ("Scoring", {
            "fields": ("maxPoints",)
        }),
        ("Script", {
            "fields": ("testScript",),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    def test_case_count(self, obj: TestCategory) -> int:
        return getattr(obj, "test_case_count_annotated", obj.testCases.count())
    test_case_count.short_description = "Test Cases"

    def resource_count(self, obj: TestCategory) -> int:
        return getattr(obj, "resource_count_annotated", TestCategoryResource.objects.filter(category=obj).count())
    resource_count.short_description = "Resources"

    def open_test_cases(self, obj: TestCategory) -> str:
        url = f"{reverse('admin:core_testcase_changelist')}?testCategory__id__exact={obj.id}"
        return format_html('<a href="{}">View Cases</a>', url)
    open_test_cases.short_description = "Cases"

    def open_resources(self, obj: TestCategory) -> str:
        url = f"{reverse('admin:core_testcategoryresource_changelist')}?category__id__exact={obj.id}"
        return format_html('<a href="{}">View Resources</a>', url)
    open_resources.short_description = "Resources"

    def sort_key(self, obj: TestCategory) -> int:
        return obj.sortKey
    sort_key.short_description = "Sort"
    sort_key.admin_order_field = "sortKey"

    def target_file(self, obj: TestCategory) -> str:
        return obj.targetFileName or "-"
    target_file.short_description = "Target File"

    def max_points(self, obj: TestCategory) -> str:
        return f"{obj.maxPoints:.2f}"
    max_points.short_description = "Max Points"
    max_points.admin_order_field = "maxPoints"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("assignment", "assignment__course").annotate(
            test_case_count_annotated=Count("testCases", distinct=True),
            resource_count_annotated=Count("resources", distinct=True),
        )


@admin.register(TestCase)
class TestCaseAdmin(admin.ModelAdmin):
    list_display = (
        "description",
        "testCategory",
        "assignment_name",
        "type",
        "exposed",
        "points_pass",
        "points_fail",
        "last_solution_run",
        "sort_key",
        "created",
    )
    search_fields = ("description", "testCategory__name", "testCategory__assignment__name", "functionName")
    list_filter = ("type", "exposed", "lastSolutionRun", NeverRunTestFilter, "testCategory__assignment", "created")
    readonly_fields = ("testCategory", "created", "modified")
    autocomplete_fields = ["testCategory"]
    search_help_text = "Search test cases by description, assignment, category, or function name."
    list_select_related = ("testCategory",)
    list_per_page = 100
    show_full_result_count = False
    actions = ["mark_exposed", "mark_hidden"]
    date_hierarchy = "created"
    
    fieldsets = (
        ("Basic Information", {
            "fields": ("testCategory", "description", "type", "sortKey", "functionName")
        }),
        ("Points", {
            "fields": ("pointsPass", "pointsFail")
        }),
        ("Behavior", {
            "fields": ("exposed", "lastSolutionRun", "rubricItem")
        }),
        ("Execution", {
            "fields": ("timeout", "targetCellId"),
            "classes": ("collapse",)
        }),
        ("Content", {
            "fields": ("text", "explanation", "testCode"),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )

    def assignment_name(self, obj: TestCase) -> str:
        return obj.testCategory.assignment.name
    assignment_name.short_description = "Assignment"
    assignment_name.admin_order_field = "testCategory__assignment__name"
    
    def points_pass(self, obj: TestCase) -> str:
        return f"{obj.pointsPass:+.2f}"
    points_pass.short_description = "Pass Points"
    points_pass.admin_order_field = "pointsPass"
    
    def points_fail(self, obj: TestCase) -> str:
        return f"{obj.pointsFail:+.2f}"
    points_fail.short_description = "Fail Points"
    points_fail.admin_order_field = "pointsFail"
    
    def sort_key(self, obj: TestCase) -> int:
        return obj.sortKey
    sort_key.short_description = "Sort"
    sort_key.admin_order_field = "sortKey"

    def last_solution_run(self, obj: TestCase) -> str:
        status_labels = {
            0: "Passed",
            1: "Failed",
            2: "Error",
            3: "Never run",
        }
        return status_labels.get(obj.lastSolutionRun, str(obj.lastSolutionRun))
    last_solution_run.short_description = "Last Solution"
    last_solution_run.admin_order_field = "lastSolutionRun"

    def mark_exposed(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(exposed=True)
        self.message_user(request, f"{updated} test case(s) marked as exposed.")
    mark_exposed.short_description = "Mark selected tests as exposed"

    def mark_hidden(self, request: Any, queryset: Any) -> None:
        updated = queryset.update(exposed=False)
        self.message_user(request, f"{updated} test case(s) marked as hidden.")
    mark_hidden.short_description = "Mark selected tests as hidden"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("testCategory", "testCategory__assignment", "testCategory__assignment__course", "rubricItem")


@admin.register(TestCategoryResource)
class TestCategoryResourceAdmin(admin.ModelAdmin):
    list_display = ("category", "assignment", "resource_type", "resource_name", "target_path", "created")
    search_fields = (
        "target_path",
        "category__name",
        "category__assignment__name",
        "file__name",
        "dataset__name",
    )
    list_filter = ("category__assignment__course", "created")
    readonly_fields = ("created", "modified")
    autocomplete_fields = ["category", "file", "dataset"]
    date_hierarchy = "created"

    def assignment(self, obj: TestCategoryResource) -> Assignment:
        return obj.category.assignment
    assignment.short_description = "Assignment"
    assignment.admin_order_field = "category__assignment__name"

    def resource_type(self, obj: TestCategoryResource) -> str:
        if obj.file is not None:
            return "File"
        if obj.dataset is not None:
            return "Dataset"
        return "-"
    resource_type.short_description = "Type"

    def resource_name(self, obj: TestCategoryResource) -> str:
        if obj.file:
            return obj.file.name
        if obj.dataset:
            return obj.dataset.name
        return "-"
    resource_name.short_description = "Resource"

    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("category", "category__assignment", "category__assignment__course", "file", "dataset")


@admin.register(SubmissionTest)
class SubmissionTestAdmin(admin.ModelAdmin):
    list_display = ("submission", "assignment", "testCase", "passed", "is_error", "score_display", "created")
    search_fields = ("submission__id", "testCase__description", "submission__assignment__name")
    list_filter = ("passed", "isError", "testCase__type", "submission__assignment", "created")
    readonly_fields = ("submission", "testCase", "created", "modified")
    autocomplete_fields = ["submission", "testCase"]
    search_help_text = "Search by submission ID, assignment name, or test case description."
    list_select_related = ("submission", "testCase")
    list_per_page = 100
    show_full_result_count = False
    date_hierarchy = "created"
    
    fieldsets = (
        ("Test Information", {
            "fields": ("submission", "testCase", "passed", "isError")
        }),
        ("Results", {
            "fields": ("logs",),
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    def is_error(self, obj: SubmissionTest) -> bool:
        return obj.isError
    is_error.short_description = "Error"
    is_error.boolean = True

    def assignment(self, obj: SubmissionTest) -> Assignment:
        return obj.submission.assignment
    assignment.short_description = "Assignment"
    assignment.admin_order_field = "submission__assignment__name"

    def score_display(self, obj: SubmissionTest) -> str:
        return f"{obj.score:.2f}/{obj.maxScore:.2f}"
    score_display.short_description = "Score"
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("submission", "submission__assignment", "testCase", "testCase__testCategory")


@admin.register(Environment)
class EnvironmentAdmin(admin.ModelAdmin):
    list_display = (
        "assignment",
        "course_name",
        "language",
        "auto_detect",
        "build_type",
        "image_name",
        "build_status_label",
        "last_built",
        "open_assignment",
        "created",
    )
    search_fields = ("assignment__name", "assignment__course__name", "image_name")
    list_filter = ("language", "auto_detect", "buildType", "build_status", "created")
    readonly_fields = ("assignment", "created", "modified")
    autocomplete_fields = ["assignment"]
    search_help_text = "Search by assignment, course, or image name."
    list_select_related = ("assignment",)
    list_per_page = 75
    show_full_result_count = False
    date_hierarchy = "created"
    
    fieldsets = (
        ("Assignment", {
            "fields": ("assignment",)
        }),
        ("Core Configuration", {
            "fields": ("language", "auto_detect", "buildType", "image_name")
        }),
        ("Advanced Build Inputs", {
            "fields": ("requirements", "dockerfile", "compileText"),
            "classes": ("collapse",)
        }),
        ("Docker Settings", {
            "fields": ("dockerRunInstructions",),
            "classes": ("collapse",)
        }),
        ("Testing Settings", {
            "fields": ("allowNetworkAccess", "maxStudentTestRuns", "maxExposedFailedTests"),
            "classes": ("collapse",)
        }),
        ("Build", {
            "fields": ("buildID", "build_status", "build_logs", "last_built"),
            "classes": ("collapse",)
        }),
        ("Timestamps", {
            "fields": ("created", "modified"),
            "classes": ("collapse",)
        }),
    )
    
    def build_type(self, obj: Environment) -> str:
        return obj.buildType
    build_type.short_description = "Build Type"
    build_type.admin_order_field = "buildType"

    def build_status_label(self, obj: Environment) -> str:
        status_labels = {
            0: "Not Built",
            1: "Building",
            2: "Success",
            3: "Failed",
        }
        return status_labels.get(obj.build_status, str(obj.build_status))
    build_status_label.short_description = "Build Status"
    build_status_label.admin_order_field = "build_status"

    def course_name(self, obj: Environment) -> str:
        return obj.assignment.course.name
    course_name.short_description = "Course"
    course_name.admin_order_field = "assignment__course__name"

    def open_assignment(self, obj: Environment) -> str:
        url = reverse("admin:core_assignment_change", args=[obj.assignment.id])
        return format_html('<a href="{}">Open Assignment</a>', url)
    open_assignment.short_description = "Assignment"
    
    # Django admin seems to be saving carriage returns for compile text
    # Replace if admin console saves
    def save_model(self, request: Any, obj: Environment, form: Any, change: bool) -> None:
        obj.compileText = obj.compileText.replace("\r", "")
        super().save_model(request, obj, form, change)
    
    def get_queryset(self, request: Any) -> Any:
        qs = super().get_queryset(request)
        return qs.select_related("assignment", "assignment__course")








# Register File model with search_fields for autocomplete
@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "extension", "scope", "context_ref", "has_path", "hash_short", "created")
    search_fields = ("name", "extension", "path", "hash")
    list_filter = ("extension", FileScopeFilter, FileHasPathFilter, "created")
    readonly_fields = ("hash", "created", "modified")
    list_per_page = 100
    show_full_result_count = False
    date_hierarchy = "created"
    search_help_text = "Search by file name, extension, path, or hash."

    def scope(self, obj: File) -> str:
        if hasattr(obj, "submissionfile"):
            return "Submission"
        if hasattr(obj, "assignmentfile"):
            return "Assignment"
        if hasattr(obj, "coursefile"):
            return "Course"
        return "Unknown"
    scope.short_description = "Scope"

    def context_ref(self, obj: File) -> str:
        try:
            if hasattr(obj, "submissionfile"):
                submission = obj.submissionfile.submission
                return f"Submission #{submission.id}"
            if hasattr(obj, "assignmentfile"):
                assignment = obj.assignmentfile.assignment
                return f"Assignment: {assignment.name}"
            if hasattr(obj, "coursefile"):
                course = obj.coursefile.course
                return f"Course: {course.name}"
        except Exception:
            pass
        return "-"
    context_ref.short_description = "Context"

    def has_path(self, obj: File) -> bool:
        return bool(obj.path)
    has_path.short_description = "Path"
    has_path.boolean = True

    def hash_short(self, obj: File) -> str:
        hash_value = obj.hash or ""
        if len(hash_value) <= 12:
            return hash_value
        return f"{hash_value[:6]}...{hash_value[-6:]}"
    hash_short.short_description = "Hash"
    hash_short.admin_order_field = "hash"





# Deprecated model, but keep in admin for data access
admin.site.register(FileTemplate)


@admin.register(MaintenanceBanner)
class MaintenanceBannerAdmin(admin.ModelAdmin):
    """
    Singleton admin for the site-wide maintenance banner.

    Behaviour:
    - The changelist immediately redirects to the edit form (pk=1), since there
      is only ever one row.
    - ``has_add_permission`` blocks a second row once the singleton exists.
    - ``has_delete_permission`` always returns False — the singleton must not be
      deleted through the admin.
    - ``save_model`` forces pk=1 so even a freshly-created object lands on the
      canonical singleton row.
    """

    list_display = ('active_status', 'message_preview', 'color_swatch')
    readonly_fields = ('banner_preview',)

    fieldsets = (
        ('Status', {
            'fields': ('active',),
        }),
        ('Content', {
            'fields': ('severity', 'message', 'color'),
            'description': (
                'Use any CSS colour value — e.g. <code>#0e704c</code>, '
                '<code>red</code>, or <code>rgba(14,112,76,0.9)</code>.'
            ),
        }),
        ('Schedule (optional)', {
            'fields': ('starts_at', 'ends_at'),
            'description': (
                'Leave both blank for immediate activation. '
                'The banner auto-activates/deactivates at the specified UTC times. '
                'The manual <em>Active</em> toggle still gates the schedule — '
                'both must be true for the banner to appear.'
            ),
        }),
        ('Preview', {
            'fields': ('banner_preview',),
            'description': 'Rendered after save; reflects the saved values.',
        }),
    )

    # ── Permission guards ─────────────────────────────────────────────────────

    def has_add_permission(self, request: Any) -> bool:
        """Block a second row once the singleton exists."""
        return not MaintenanceBanner.objects.exists()

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        """Prevent accidental deletion of the singleton row."""
        return False

    # ── Singleton enforcement ─────────────────────────────────────────────────

    def save_model(self, request: Any, obj: MaintenanceBanner, form: Any, change: bool) -> None:
        """Always write to pk=1 to enforce the singleton contract."""
        obj.pk = 1
        super().save_model(request, obj, form, change)

    # ── Changelist → edit redirect ────────────────────────────────────────────

    def changelist_view(self, request: Any, extra_context: Any = None) -> HttpResponseRedirect:
        """Skip the list entirely — jump straight to the singleton edit page."""
        banner = MaintenanceBanner.load()
        return redirect(
            reverse('admin:core_maintenancebanner_change', args=[banner.pk])
        )

    # ── list_display helpers ──────────────────────────────────────────────────

    def active_status(self, obj: MaintenanceBanner) -> str:
        if obj.active:
            return format_html(
                '<span style="color:#2e7d32; font-weight:600;">&#9679; Active</span>'
            )
        return format_html('<span style="color:#9e9e9e;">&#9675; Inactive</span>')
    active_status.short_description = 'Status'

    def message_preview(self, obj: MaintenanceBanner) -> str:
        text = obj.message or ''
        truncated = text[:80] + '\u2026' if len(text) > 80 else text
        return format_html('<span style="font-family:monospace;">{}</span>', truncated)
    message_preview.short_description = 'Message'

    def color_swatch(self, obj: MaintenanceBanner) -> str:
        color = obj.color or '#888888'
        return format_html(
            '<span style="display:inline-flex; align-items:center; gap:6px;">'
            '<span style="width:18px; height:18px; border-radius:3px; '
            'border:1px solid #ccc; background:{color}; display:inline-block;">'
            '</span>'
            '<code>{color}</code>'
            '</span>',
            color=color,
        )
    color_swatch.short_description = 'Colour'

    # ── readonly change-form helpers ─────────────────────────────────────────

    def banner_preview(self, obj: MaintenanceBanner) -> str:
        """Render a styled preview of the banner as it will appear to users."""
        if not obj or not obj.pk:
            return format_html(
                '<em style="color:#999;">Save the banner first to see a preview.</em>'
            )
        color = obj.color or '#0e704c'
        message = obj.message or '(no message set)'
        return format_html(
            '<div style="background:{color}; color:#fff; padding:10px 16px; '
            'border-radius:4px; font-size:14px; max-width:640px; '
            'box-shadow:0 1px 3px rgba(0,0,0,.2);">'
            '{message}'
            '</div>',
            color=color,
            message=message,
        )
    banner_preview.short_description = 'Live Preview'





# ============================================================================
# Quizzes
# ============================================================================


def _preview(text: Optional[str], length: int = 80) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:length] + "…" if len(text) > length else text


def _pretty_json(value: Any) -> str:
    """Render a JSON blob as a scrollable <pre> block for readonly admin fields."""
    if not value:
        return mark_safe('<em style="color:#999;">(empty)</em>')
    return format_html(
        '<pre style="max-height:480px; overflow:auto; white-space:pre-wrap; '
        'font-size:12px; background:#f6f8fa; padding:8px; border-radius:4px;">{}</pre>',
        json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _raw_output_block(raw: Optional[str]) -> str:
    """Render a rejected model output for readonly admin fields (red-tinted <pre>)."""
    if not raw:
        return mark_safe('<em style="color:#999;">(none recorded \u2014 the provider call itself failed, '
                           'or the output parsed cleanly)</em>')
    return format_html(
        '<pre style="max-height:480px; overflow:auto; white-space:pre-wrap; font-size:12px; '
        'background:#fff8f6; padding:8px; border-radius:4px;">{}</pre>', raw)


def _error_cell(text: Optional[str]) -> str:
    if not text:
        return "\u2014"
    return format_html('<span style="color:#c62828;" title="{}">{}</span>', text, _preview(text, 90))


class HasGenerationErrorFilter(admin.SimpleListFilter):
    title = "generation error"
    parameter_name = "has_error"

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Has error"), ("no", "No error")]

    def queryset(self, request: Any, queryset: Any) -> Any:
        value = self.value()
        if value == "yes":
            return queryset.exclude(errorMessage="")
        if value == "no":
            return queryset.filter(errorMessage="")
        return queryset


class QuestionChoiceInline(admin.TabularInline):
    model = QuestionChoice
    extra = 0
    fields = ("sortKey", "text", "isCorrect", "feedback")
    ordering = ("sortKey",)


@admin.register(QuestionBank)
class QuestionBankAdmin(admin.ModelAdmin):
    list_display = ("name", "course", "source", "question_count", "createdBy", "created")
    list_filter = ("source", "created")
    search_fields = ("name", "course__name", "course__period")
    autocomplete_fields = ["course"]
    raw_id_fields = ("createdBy",)
    readonly_fields = ("created", "modified")
    date_hierarchy = "created"

    def question_count(self, obj: QuestionBank) -> int:
        return obj.question_count
    question_count.short_description = "Questions"
    question_count.admin_order_field = "question_count"

    def get_queryset(self, request: Any) -> Any:
        return super().get_queryset(request).select_related("course", "createdBy").annotate(
            question_count=Count("questions", distinct=True))


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("id", "text_preview", "questionType", "course", "bank", "points", "source", "created")
    list_filter = ("questionType", "source", "created")
    search_fields = ("text", "course__name", "course__period", "bank__name")
    autocomplete_fields = ["course", "bank"]
    raw_id_fields = ("createdBy",)
    readonly_fields = ("created", "modified")
    date_hierarchy = "created"
    inlines = [QuestionChoiceInline]

    def text_preview(self, obj: Question) -> str:
        return _preview(obj.text)
    text_preview.short_description = "Text"

    def get_queryset(self, request: Any) -> Any:
        return super().get_queryset(request).select_related("course", "bank")


@admin.register(QuestionChoice)
class QuestionChoiceAdmin(admin.ModelAdmin):
    list_display = ("id", "question", "sortKey", "text_preview", "isCorrect")
    list_filter = ("isCorrect",)
    search_fields = ("text", "question__text")
    raw_id_fields = ("question",)
    readonly_fields = ("created", "modified")

    def text_preview(self, obj: QuestionChoice) -> str:
        return _preview(obj.text)
    text_preview.short_description = "Text"


class QuizQuestionInline(admin.TabularInline):
    model = QuizQuestion
    extra = 0
    fields = ("sortKey", "question", "pointsOverride")
    raw_id_fields = ("question",)
    ordering = ("sortKey",)


class QuizQuestionGroupInline(admin.TabularInline):
    model = QuizQuestionGroup
    extra = 0
    fields = ("sortKey", "name", "bank", "pickCount", "pointsPerQuestion")
    raw_id_fields = ("bank",)
    ordering = ("sortKey",)


class QuizGeneratedSectionInline(admin.TabularInline):
    model = QuizGeneratedSection
    extra = 0
    fields = ("sortKey", "name", "numQuestions", "pointsPerQuestion", "questionTypes")
    ordering = ("sortKey",)


@admin.register(Quiz)
class QuizAdmin(admin.ModelAdmin):
    list_display = (
        "id", "title", "course", "assignment", "assignment_state", "published_status",
        "assignmentTrigger", "timeLimitMinutes", "attemptsAllowed",
        "in_progress_count", "submitted_count", "created",
    )
    list_filter = ("isPublished", "assignmentTrigger", "requireSebBrowser", "closeEvent", "created")
    search_fields = ("title", "course__name", "course__period", "assignment__name")
    autocomplete_fields = ["course", "assignment"]
    raw_id_fields = ("createdBy",)
    readonly_fields = ("created", "modified", "scheduledGenerationRanAt")
    date_hierarchy = "created"
    inlines = [QuizQuestionInline, QuizQuestionGroupInline, QuizGeneratedSectionInline]
    search_help_text = "Search by quiz title, course name/period, or assignment name."

    fieldsets = (
        ("Basic Information", {
            "fields": ("title", "description", "course", "assignment", "source", "createdBy", "isPublished")
        }),
        ("Availability", {
            "fields": ("assignmentTrigger", "availableFrom", "availableUntil", "closeEvent",
                       "closeOffsetMinutes", "endAttemptsAtClose", "accessCode")
        }),
        ("Attempt Rules", {
            "fields": ("timeLimitMinutes", "attemptsAllowed", "shuffleQuestions", "oneQuestionAtATime",
                       "allowBacktracking", "requireSebBrowser", "sebConfigKey")
        }),
        ("Results & Scoring", {
            "fields": ("showCorrectAnswers", "sealResultsUntilClose", "showResponses",
                       "allowSubmissionReview", "passingScore", "passingScoreUnit",
                       "scoringPolicy", "multiAttemptScoreMethod")
        }),
        ("Generated Questions", {
            "fields": ("gradersCanReviewGenerated", "gradersCanGenerate", "autoPublishGenerated",
                       "manualGeneration", "generationDate", "scheduledGenerationRanAt"),
            "classes": ("collapse",)
        }),
        ("Metadata", {
            "fields": ("metadata", "created", "modified"),
            "classes": ("collapse",)
        }),
    )

    def assignment_state(self, obj: Quiz) -> str:
        return obj.assignment.state if obj.assignment_id else "—"
    assignment_state.short_description = "Assignment state"
    assignment_state.admin_order_field = "assignment__state"

    def published_status(self, obj: Quiz) -> str:
        if obj.isPublished:
            return mark_safe('<span style="color:#2e7d32; font-weight:600;">&#9679; Published</span>')
        return mark_safe('<span style="color:#9e9e9e;">&#9675; Unpublished</span>')
    published_status.short_description = "Published"
    published_status.admin_order_field = "isPublished"

    def in_progress_count(self, obj: Quiz) -> int:
        return obj.in_progress_count
    in_progress_count.short_description = "In progress"
    in_progress_count.admin_order_field = "in_progress_count"

    def submitted_count(self, obj: Quiz) -> int:
        return obj.submitted_count
    submitted_count.short_description = "Submitted"
    submitted_count.admin_order_field = "submitted_count"

    def get_queryset(self, request: Any) -> Any:
        return super().get_queryset(request).select_related("course", "assignment").annotate(
            in_progress_count=Count("attempts", filter=Q(attempts__status="in_progress"), distinct=True),
            submitted_count=Count("attempts", filter=Q(attempts__status="submitted"), distinct=True),
        )


@admin.register(QuizQuestion)
class QuizQuestionAdmin(admin.ModelAdmin):
    list_display = ("id", "quiz", "sortKey", "question", "pointsOverride")
    search_fields = ("quiz__title", "question__text")
    autocomplete_fields = ["quiz"]
    raw_id_fields = ("question",)
    readonly_fields = ("created", "modified")


@admin.register(QuizQuestionGroup)
class QuizQuestionGroupAdmin(admin.ModelAdmin):
    list_display = ("id", "quiz", "sortKey", "name", "bank", "pickCount", "pointsPerQuestion")
    search_fields = ("quiz__title", "name", "bank__name")
    autocomplete_fields = ["quiz", "bank"]
    readonly_fields = ("created", "modified")


class QuizResponseInline(admin.TabularInline):
    model = QuizResponse
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ("sortKey", "question_text", "answer_preview", "points", "pointsEarned",
              "isCorrect", "needsManualGrading", "gradedBy", "gradedAt")
    readonly_fields = ("sortKey", "question_text", "answer_preview", "points", "gradedBy", "gradedAt")
    ordering = ("sortKey", "id")

    def has_add_permission(self, request: Any, obj: Any = None) -> bool:
        return False

    def question_text(self, obj: QuizResponse) -> str:
        return _preview((obj.questionSnapshot or {}).get("text"), 60)
    question_text.short_description = "Question"

    def answer_preview(self, obj: QuizResponse) -> str:
        if obj.selectedChoiceKeys:
            return f"choices {obj.selectedChoiceKeys}"
        return _preview(obj.answerText, 60)
    answer_preview.short_description = "Answer"


@admin.register(QuizAttempt)
class QuizAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "id", "quiz", "student_email", "attemptNumber", "status_badge", "startedAt", "deadline",
        "submittedAt", "score_display", "answered_count", "needsManualGrading", "passed",
    )
    list_filter = ("status", "needsManualGrading", "passed", "isOfficialOverride",
                   "closeBypassed", "lockdownVerified", "startedAt")
    search_fields = ("student__email", "student__username", "quiz__title",
                     "quiz__course__name", "quiz__course__period")
    autocomplete_fields = ["quiz"]
    raw_id_fields = ("student",)
    readonly_fields = ("created", "modified")
    date_hierarchy = "startedAt"
    inlines = [QuizResponseInline]
    search_help_text = "Search by student email/username, quiz title, or course name/period."

    def student_email(self, obj: QuizAttempt) -> str:
        return obj.student.email
    student_email.short_description = "Student"
    student_email.admin_order_field = "student__email"

    def status_badge(self, obj: QuizAttempt) -> str:
        if obj.status == "submitted":
            return mark_safe('<span style="color:#2e7d32; font-weight:600;">Submitted</span>')
        return mark_safe('<span style="color:#e65100; font-weight:600;">In progress</span>')
    status_badge.short_description = "Status"
    status_badge.admin_order_field = "status"

    def score_display(self, obj: QuizAttempt) -> str:
        if obj.score is None and obj.maxScore is None:
            return "—"
        return f"{obj.score if obj.score is not None else '?'} / {obj.maxScore if obj.maxScore is not None else '?'}"
    score_display.short_description = "Score"
    score_display.admin_order_field = "score"

    def answered_count(self, obj: QuizAttempt) -> str:
        return f"{obj.answered_count} / {obj.response_count}"
    answered_count.short_description = "Answered"
    answered_count.admin_order_field = "answered_count"

    def get_queryset(self, request: Any) -> Any:
        answered = ~Q(responses__answerText="") | ~Q(responses__selectedChoiceKeys=[])
        return super().get_queryset(request).select_related("quiz", "quiz__course", "student").annotate(
            response_count=Count("responses", distinct=True),
            answered_count=Count("responses", filter=answered, distinct=True),
        )


@admin.register(QuizResponse)
class QuizResponseAdmin(admin.ModelAdmin):
    list_display = ("id", "attempt", "sortKey", "question_text", "answer_preview", "points",
                    "pointsEarned", "isCorrect", "needsManualGrading", "gradedBy", "gradedAt")
    list_filter = ("isCorrect", "needsManualGrading", "modified")
    search_fields = ("attempt__student__email", "attempt__quiz__title", "answerText")
    raw_id_fields = ("attempt", "question", "generatedQuestion", "gradedBy")
    readonly_fields = ("created", "modified")
    date_hierarchy = "modified"

    def question_text(self, obj: QuizResponse) -> str:
        return _preview((obj.questionSnapshot or {}).get("text"), 60)
    question_text.short_description = "Question"

    def answer_preview(self, obj: QuizResponse) -> str:
        if obj.selectedChoiceKeys:
            return f"choices {obj.selectedChoiceKeys}"
        return _preview(obj.answerText, 60)
    answer_preview.short_description = "Answer"

    def get_queryset(self, request: Any) -> Any:
        return super().get_queryset(request).select_related(
            "attempt", "attempt__quiz", "attempt__student", "gradedBy")


@admin.register(QuizAccommodation)
class QuizAccommodationAdmin(admin.ModelAdmin):
    list_display = ("id", "course", "student", "timeMultiplier", "sebExempt", "modified")
    list_filter = ("sebExempt",)
    search_fields = ("student__email", "course__name", "course__period")
    autocomplete_fields = ["course"]
    raw_id_fields = ("student",)
    readonly_fields = ("created", "modified")


@admin.register(QuizSebLaunch)
class QuizSebLaunchAdmin(admin.ModelAdmin):
    list_display = ("id", "quiz", "student", "configKey", "expiresAt", "created")
    list_filter = ("created",)
    search_fields = ("student__email", "quiz__title")
    autocomplete_fields = ["quiz"]
    raw_id_fields = ("student", "token")
    readonly_fields = ("created", "modified")
    date_hierarchy = "created"


@admin.register(QuizGeneratedSection)
class QuizGeneratedSectionAdmin(admin.ModelAdmin):
    list_display = ("id", "quiz", "sortKey", "name", "numQuestions", "pointsPerQuestion")
    search_fields = ("quiz__title", "name")
    autocomplete_fields = ["quiz"]
    readonly_fields = ("created", "modified")


@admin.register(GeneratedQuestionSet)
class GeneratedQuestionSetAdmin(admin.ModelAdmin):
    """Per-student AI-generated question sets. A failed run leaves ``errorMessage`` set and, when
    the model answered but the output could not be parsed, the raw model output in
    ``generationMetadata['raw_output']`` — both are rendered read-only on the change page."""

    list_display = ("id", "quiz", "student", "submission", "status", "error", "question_count",
                    "approvedBy", "approvedAt", "modified")
    list_filter = ("status", HasGenerationErrorFilter, "created")
    search_fields = ("quiz__title", "student__email", "generationBatch", "errorMessage")
    actions = ["regenerate_selected"]
    autocomplete_fields = ["quiz"]
    raw_id_fields = ("student", "submission", "approvedBy", "promptVariant")
    readonly_fields = ("errorMessage", "raw_model_output", "section_prompts", "generation_metadata",
                       "generationBatch", "created", "modified")
    exclude = ("generationMetadata",)
    date_hierarchy = "created"

    def error(self, obj: GeneratedQuestionSet) -> str:
        return _error_cell(obj.errorMessage)
    error.short_description = "Error"
    error.admin_order_field = "errorMessage"

    def question_count(self, obj: GeneratedQuestionSet) -> int:
        return obj.question_count
    question_count.short_description = "Questions"
    question_count.admin_order_field = "question_count"

    def raw_model_output(self, obj: GeneratedQuestionSet) -> str:
        return _raw_output_block((obj.generationMetadata or {}).get("raw_output"))
    raw_model_output.short_description = "Raw model output"

    @admin.action(description="Regenerate questions (force, unless the student has attempted)")
    def regenerate_selected(self, request: Any, queryset: Any) -> None:
        """Re-run generation for the selected sets — the same path as the staff
        'regenerate' button. A set whose student already attempted the quiz is skipped
        (reset their attempts first), so an in-progress attempt is never disturbed."""
        from core.tasks import generate_personalized_quiz_sets
        started = skipped = 0
        for gen_set in queryset.select_related("quiz"):
            if gen_set.questions.exists() and gen_set.quiz.attempts.filter(student_id=gen_set.student_id).exists():
                skipped += 1
                continue
            GeneratedQuestionSet.objects.filter(pk=gen_set.pk).update(
                status="pending", approvedBy=None, approvedAt=None)
            generate_personalized_quiz_sets.delay(
                gen_set.submission_id, quiz_id=gen_set.quiz_id, force=True,
                requested_by_id=request.user.id, student_id=gen_set.student_id)
            started += 1
        msg = f"Queued regeneration for {started} set(s)."
        if skipped:
            msg += f" Skipped {skipped} whose student has already attempted the quiz."
        self.message_user(request, msg, level=messages.WARNING if skipped else messages.SUCCESS)

    def section_prompts(self, obj: GeneratedQuestionSet) -> str:
        return _pretty_json((obj.generationMetadata or {}).get("sections"))
    section_prompts.short_description = "Section prompts sent"

    def generation_metadata(self, obj: GeneratedQuestionSet) -> str:
        meta = dict(obj.generationMetadata or {})
        meta.pop("raw_output", None)
        meta.pop("sections", None)
        return _pretty_json(meta)
    generation_metadata.short_description = "Provider / token usage"

    def get_queryset(self, request: Any) -> Any:
        return super().get_queryset(request).select_related("quiz", "student", "approvedBy").annotate(
            question_count=Count("questions", distinct=True))


@admin.register(GeneratedQuizQuestion)
class GeneratedQuizQuestionAdmin(admin.ModelAdmin):
    list_display = ("id", "set", "section", "sortKey", "questionType", "text_preview", "points")
    list_filter = ("questionType",)
    search_fields = ("text", "set__quiz__title", "set__student__email")
    raw_id_fields = ("set", "section")
    readonly_fields = ("created", "modified")

    def text_preview(self, obj: GeneratedQuizQuestion) -> str:
        return _preview(obj.text)
    text_preview.short_description = "Text"


@admin.register(SuggestedQuizQuestion)
class SuggestedQuizQuestionAdmin(admin.ModelAdmin):
    list_display = ("id", "assignment", "questionType", "status", "text_preview", "points", "acceptedBy", "created")
    list_filter = ("status", "questionType", "created")
    search_fields = ("text", "assignment__name", "generationBatch")
    autocomplete_fields = ["assignment"]
    raw_id_fields = ("sourceQuestion", "acceptedBy", "acceptedQuestion", "promptVariant")
    readonly_fields = ("created", "modified")
    date_hierarchy = "created"

    def text_preview(self, obj: SuggestedQuizQuestion) -> str:
        return _preview(obj.text)
    text_preview.short_description = "Text"


@admin.register(QuizImportJob)
class QuizImportJobAdmin(admin.ModelAdmin):
    list_display = ("id", "course", "status", "createdBy", "targetBank", "createdQuizCount",
                    "createdQuestionCount", "created")
    list_filter = ("status", "created")
    search_fields = ("course__name", "createdBy__email", "taskId")
    autocomplete_fields = ["course"]
    raw_id_fields = ("createdBy", "targetBank")
    readonly_fields = ("taskId", "created", "modified")
    date_hierarchy = "created"


@admin.register(QuizSuggestionJob)
class QuizSuggestionJobAdmin(admin.ModelAdmin):
    list_display = ("id", "course", "assignment", "quiz", "status", "error", "requestedBy",
                    "createdCount", "created")
    list_filter = ("status", HasGenerationErrorFilter, "created")
    search_fields = ("course__name", "assignment__name", "quiz__title", "requestedBy__email",
                     "taskId", "errorMessage")
    autocomplete_fields = ["course", "assignment", "quiz"]
    raw_id_fields = ("sourceQuestion", "requestedBy")
    readonly_fields = ("errorMessage", "raw_model_output", "result_data", "taskId", "generationBatch",
                       "created", "modified")
    exclude = ("resultData",)
    date_hierarchy = "created"
    actions = ["retry_generation"]

    def error(self, obj: QuizSuggestionJob) -> str:
        return _error_cell(obj.errorMessage)
    error.short_description = "Error"
    error.admin_order_field = "errorMessage"

    def raw_model_output(self, obj: QuizSuggestionJob) -> str:
        return _raw_output_block((obj.resultData or {}).get("raw_output"))
    raw_model_output.short_description = "Raw model output"

    def result_data(self, obj: QuizSuggestionJob) -> str:
        data = dict(obj.resultData or {})
        data.pop("raw_output", None)
        return _pretty_json(data)
    result_data.short_description = "Result data"

    @admin.action(description="Retry generation (new job, same request)")
    def retry_generation(self, request: Any, queryset: Any) -> None:
        """Re-run a failed suggestion job as a NEW job with the parameters the task
        recorded on it. Section-preview jobs (quiz set) and jobs from before the
        request was recorded are skipped — they can't be reproduced from the row."""
        from core.tasks import generate_quiz_question_suggestions
        started = skipped = 0
        for job in queryset:
            params = (job.resultData or {}).get("request")
            if job.quiz_id is not None or not params or not (job.assignment_id or job.sourceQuestion_id):
                skipped += 1
                continue
            new_job = QuizSuggestionJob.objects.create(
                course=job.course, assignment=job.assignment, sourceQuestion=job.sourceQuestion,
                requestedBy=request.user)
            task = generate_quiz_question_suggestions.delay(
                requested_by_id=request.user.id,
                assignment_id=job.assignment_id,
                source_question_id=job.sourceQuestion_id,
                num_questions=params.get("num_questions", 5),
                question_types=params.get("question_types"),
                instructions=params.get("instructions") or "",
                job_id=new_job.id,
            )
            QuizSuggestionJob.objects.filter(pk=new_job.pk).update(taskId=task.id)
            started += 1
        msg = f"Started {started} new generation job(s)."
        if skipped:
            msg += f" Skipped {skipped} (preview jobs, or no recorded request to replay)."
        self.message_user(request, msg, level=messages.WARNING if skipped else messages.SUCCESS)


@admin.register(QuizImage)
class QuizImageAdmin(admin.ModelAdmin):
    list_display = ("token", "course", "originalName", "contentType", "uploadedBy", "created")
    list_filter = ("contentType", "created")
    search_fields = ("originalName", "token", "course__name")
    autocomplete_fields = ["course"]
    raw_id_fields = ("uploadedBy",)
    readonly_fields = ("token", "created", "modified")
    date_hierarchy = "created"


# ============================================================================
# Audit, Agent & AI
# ============================================================================


@admin.register(CourseAuditEvent)
class CourseAuditEventAdmin(admin.ModelAdmin):
    """Read-only: audit events are written by the services layer, never edited by hand."""

    list_display = ("created", "event_type", "course", "user_email", "assignment", "quiz",
                    "submission", "meta_preview")
    list_filter = ("event_type", "created")
    search_fields = ("course__name", "course__period", "user__email", "assignment__name", "quiz__title")
    raw_id_fields = ("course", "assignment", "quiz", "submission", "user")
    readonly_fields = ("created", "modified")
    date_hierarchy = "created"
    search_help_text = "Search by course name/period, user email, assignment name, or quiz title."

    def has_add_permission(self, request: Any) -> bool:
        return False

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return False

    def user_email(self, obj: CourseAuditEvent) -> str:
        return obj.user.email if obj.user_id else "—"
    user_email.short_description = "User"
    user_email.admin_order_field = "user__email"

    def meta_preview(self, obj: CourseAuditEvent) -> str:
        return format_html('<span style="font-family:monospace;">{}</span>', _preview(str(obj.meta or ""), 100))
    meta_preview.short_description = "Meta"

    def get_queryset(self, request: Any) -> Any:
        return super().get_queryset(request).select_related("course", "assignment", "quiz", "submission", "user")


@admin.register(CourseAPIKey)
class CourseAPIKeyAdmin(admin.ModelAdmin):
    """Keys are minted through the API (the raw key is shown once); the admin only inspects/revokes."""

    list_display = ("name", "course", "key_prefix", "scope", "is_active", "created_by", "last_used_at", "created")
    list_filter = ("scope", "is_active", "created")
    search_fields = ("name", "key_prefix", "course__name", "course__period", "created_by__email")
    raw_id_fields = ("course", "created_by")
    readonly_fields = ("key_prefix", "hashed_key", "last_used_at", "created", "modified")
    date_hierarchy = "created"

    def has_add_permission(self, request: Any) -> bool:
        return False


@admin.register(PendingAgentAction)
class PendingAgentActionAdmin(admin.ModelAdmin):
    list_display = ("id", "tool", "course", "requested_by", "state", "expires_at", "created")
    list_filter = ("tool", "created")
    search_fields = ("tool", "course__name", "requested_by__email", "code")
    raw_id_fields = ("course", "requested_by", "approved_by")
    readonly_fields = ("args_hash", "plan_hash", "code", "approved_at", "denied_at", "redeemed_at",
                       "created", "modified")
    date_hierarchy = "created"

    def has_add_permission(self, request: Any) -> bool:
        return False

    def state(self, obj: PendingAgentAction) -> str:
        if obj.redeemed_at:
            return "redeemed"
        if obj.denied_at:
            return "denied"
        if obj.approved_at:
            return "approved"
        if obj.expires_at and obj.expires_at < timezone.now():
            return "expired"
        return "pending"
    state.short_description = "State"

    def get_queryset(self, request: Any) -> Any:
        return super().get_queryset(request).select_related("course", "requested_by")


@admin.register(AutograderExecutionEvent)
class AutograderExecutionEventAdmin(admin.ModelAdmin):
    list_display = ("created", "course", "assignment", "success", "cached", "language", "trigger",
                    "error_category", "error_message")
    list_filter = ("success", "cached", "trigger", "error_category", "language", "created")
    search_fields = ("course__name", "assignment__name", "error_message")
    raw_id_fields = ("course", "assignment")
    readonly_fields = ("created", "modified")
    date_hierarchy = "created"

    def has_add_permission(self, request: Any) -> bool:
        return False

    def get_queryset(self, request: Any) -> Any:
        return super().get_queryset(request).select_related("course", "assignment")


@admin.register(AIUsageRecord)
class AIUsageRecordAdmin(admin.ModelAdmin):
    list_display = ("created", "request_type", "provider", "model", "user", "course", "assignment",
                    "total_tokens", "cached_tokens", "estimated_cost", "status", "error")
    list_filter = ("status", "request_type", "provider", "model", "created")
    search_fields = ("user__email", "course__name", "assignment__name", "model", "error_message")
    raw_id_fields = ("organization", "course", "assignment", "user", "prompt_variant", "experiment")
    readonly_fields = ("created", "modified")
    date_hierarchy = "created"

    def error(self, obj: AIUsageRecord) -> str:
        return _error_cell(obj.error_message)
    error.short_description = "Error"

    def get_queryset(self, request: Any) -> Any:
        return super().get_queryset(request).select_related("user", "course", "assignment")


@admin.register(SuggestedComment)
class SuggestedCommentAdmin(admin.ModelAdmin):
    list_display = ("id", "submission", "file", "status", "startLine", "endLine", "pointDelta",
                    "text_preview", "acceptedBy", "created")
    list_filter = ("status", "created")
    search_fields = ("text", "submission__id", "file__name", "generationBatch")
    raw_id_fields = ("submission", "file", "rubricComment", "acceptedBy", "acceptedComment", "promptVariant")
    readonly_fields = ("created", "modified")
    date_hierarchy = "created"

    def text_preview(self, obj: SuggestedComment) -> str:
        return _preview(obj.text)
    text_preview.short_description = "Text"

    def get_queryset(self, request: Any) -> Any:
        return super().get_queryset(request).select_related("submission", "file", "acceptedBy")


@admin.register(SubmissionSummary)
class SubmissionSummaryAdmin(admin.ModelAdmin):
    list_display = ("id", "submission", "regenerationCount", "text_preview", "modified")
    search_fields = ("submission__id", "text")
    raw_id_fields = ("submission",)
    readonly_fields = ("created", "modified")

    def text_preview(self, obj: SubmissionSummary) -> str:
        return _preview(obj.text, 120)
    text_preview.short_description = "Summary"


@admin.register(SystemPromptVariant)
class SystemPromptVariantAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "prompt_type", "status", "version", "parent", "created_by", "created")
    list_filter = ("prompt_type", "status", "created")
    search_fields = ("name", "text")
    raw_id_fields = ("parent", "created_by")
    readonly_fields = ("created", "modified")
    date_hierarchy = "created"


@admin.register(PromptExperiment)
class PromptExperimentAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "prompt_type", "status", "variant_a", "variant_b", "sample_rate",
                    "started_by", "completed_at", "created")
    list_filter = ("prompt_type", "status", "created")
    search_fields = ("name",)
    raw_id_fields = ("variant_a", "variant_b", "started_by")
    readonly_fields = ("created", "modified")


@admin.register(PromptFeedback)
class PromptFeedbackAdmin(admin.ModelAdmin):
    list_display = ("created", "prompt_type", "experiment", "variant_used", "chosen_variant", "user",
                    "rating", "is_custom_context")
    list_filter = ("prompt_type", "rating", "is_custom_context", "created")
    search_fields = ("user__email", "feedback_text", "experiment__name")
    raw_id_fields = ("experiment", "variant_used", "chosen_variant", "user", "usage_record")
    readonly_fields = ("created", "modified")
    date_hierarchy = "created"


@admin.register(PromptLabSettings)
class PromptLabSettingsAdmin(admin.ModelAdmin):
    list_display = ("id", "auto_improve_enabled", "schedule_enabled", "schedule_interval_hours",
                    "threshold_enabled", "feedback_threshold", "min_feedback", "ai_provider", "ai_model")
    # The provider key is encrypted at rest; never render it in the admin form.
    exclude = ("ai_api_key",)


# ============================================================================
# Remaining submission / assignment helpers
# ============================================================================


@admin.register(LearningObjective)
class LearningObjectiveAdmin(admin.ModelAdmin):
    list_display = ("shortId", "name", "assignment", "visibilityMode", "aggregationMode", "created")
    list_filter = ("visibilityMode", "aggregationMode")
    search_fields = ("shortId", "name", "assignment__name")
    autocomplete_fields = ["assignment"]
    readonly_fields = ("created", "modified")


@admin.register(StudentDataSetAssignment)
class StudentDataSetAssignmentAdmin(admin.ModelAdmin):
    list_display = ("id", "assignment", "student", "dataset", "assignedBy", "created")
    search_fields = ("assignment__name", "student__email", "dataset__name")
    autocomplete_fields = ["assignment", "dataset"]
    raw_id_fields = ("student", "assignedBy")
    readonly_fields = ("created", "modified")


@admin.register(SubmissionVariantRun)
class SubmissionVariantRunAdmin(admin.ModelAdmin):
    list_display = ("id", "submission", "dataset", "created")
    search_fields = ("submission__id", "dataset__name")
    raw_id_fields = ("submission", "dataset")
    readonly_fields = ("created", "modified")
    date_hierarchy = "created"


@admin.register(SubmissionFileEdit)
class SubmissionFileEditAdmin(admin.ModelAdmin):
    list_display = ("id", "file", "lastEditedBy", "modified")
    search_fields = ("file__name", "file__submission__id", "lastEditedBy__email")
    raw_id_fields = ("file", "lastEditedBy")
    readonly_fields = ("created", "modified")
