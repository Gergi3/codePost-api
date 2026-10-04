# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from __future__ import annotations

from asyncio.log import logger
from datetime import datetime, timedelta
from decimal import Decimal
import re
import hashlib
import base64
import uuid
import shutil
from encrypted_model_fields.fields import EncryptedCharField

from django.conf import settings as django_settings
from django.contrib.auth.models import User  # type: ignore[assignment]
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Avg
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils.timezone import now
from jsonfield import JSONField
from rest_framework.authtoken.models import Token
from django.utils import timezone
from django.utils.text import slugify

from core.validators import validate_hex_color
from core.prompts.registry import prompt_registry
import core.prompts  # noqa: F401 — triggers @register_prompt side-effects
from typing import Optional, Any, TYPE_CHECKING
from codepost.settings import DEBUG, MEDIA_ROOT
import os

def get_default_token_expiry():
  """Returns datetime 5 minutes from now for OneTimeToken expiration."""
  if DEBUG:
    return timezone.now() + timedelta(days=1)

  return timezone.now() + timedelta(minutes=5)

if TYPE_CHECKING:
    from django.db.models import Manager as RelatedManager
    
    # Extend User model with custom reverse relationships for type checking
    # This tells Pylance about the relationships defined in our models
    class User(User):  # type: ignore[no-redef]
        profile: Profile
        student_submissions: RelatedManager[Submission]
        grader_submissions: RelatedManager[Submission]
        grader_regraded_submissions: RelatedManager[Submission]
        student_submissionHistories: RelatedManager[SubmissionHistory]
        student_courses: RelatedManager[Course]
        grader_courses: RelatedManager[Course]
        courseAdmin_courses: RelatedManager[Course]
        student_inactive_courses: RelatedManager[Course]
        grader_inactive_courses: RelatedManager[Course]
        courseAdmin_inactive_courses: RelatedManager[Course]
        leader_sections: RelatedManager[Section]
        student_sections: RelatedManager[Section]
    

# Notes
# Consider using indexes (db_index) to speed up common queries
# (https://stackoverflow.com/questions/14786413/add-indexes-db-index-true)

class BaseModel(models.Model):
  created: models.DateTimeField[datetime, datetime] = models.DateTimeField(editable=False, default=now)
  modified: models.DateTimeField[datetime, datetime] = models.DateTimeField(default=now)

  class Meta:
    abstract = True

  def save(self, *args: Any, **kwargs: Any) -> None:
    if self.pk:
      ''' Update '''
      if kwargs.get('update_fields') is not None:
        # The caller declared what changed — honor it (no diff, no extra SELECT)
        # and persist the `modified` stamp alongside.
        self.modified = now()
        update_fields = list(kwargs['update_fields'])
        if 'modified' not in update_fields:
          update_fields.append('modified')
        kwargs['update_fields'] = update_fields
        return super(BaseModel, self).save(*args, **kwargs)

      ######################################################################
      # Check which fields have been updated
      ######################################################################
      # Concrete fields compared by attname so FKs compare raw ids instead of
      # lazy-loading related objects; deferred fields are skipped (touching
      # them would fire a query each). An empty diff keeps update_fields=[],
      # which makes Django skip the UPDATE and signals entirely.
      cls = self.__class__
      old = cls.objects.get(pk=self.pk)
      deferred = self.get_deferred_fields()
      changed_fields: list[str] = []
      for field in cls._meta.concrete_fields:
        attname = field.attname
        if attname in deferred:
          continue
        try:
          if getattr(old, attname) != getattr(self, attname):
            changed_fields.append(field.name)
        except Exception:
          pass
      if changed_fields:
        self.modified = now()
        changed_fields.append('modified')
      kwargs['update_fields'] = changed_fields
      ######################################################################
      ######################################################################
    else:
      ''' Create '''
      self.created = now()
      self.modified = now()

    return super(BaseModel, self).save(*args, **kwargs)

############# User Section ####################################################

# Internal Model - not published in public API


class Organization(BaseModel):
  if TYPE_CHECKING:
    id: int
    profiles: RelatedManager[Profile]
    courses: RelatedManager[Course]
    
  name = models.CharField(max_length=64, unique=True,
                          help_text=("The name of the organization."))
  shortname = models.CharField(max_length=12, unique=True, help_text=(
      "A shortname for the organization (e.g. Princeton University -> PU)"))
  email_domain = models.CharField(max_length=64, blank=True, null=True, help_text=(
      "The email domain associated with the organization."))
  allowed_email_domains = models.JSONField(default=list, blank=True, help_text=(
      "Additional email domains that should map to this organization for SSO lookup (e.g. subdomains like scarletmail.rutgers.edu)."))

  sso_enabled = models.BooleanField(default=False, help_text=("If True, new users in this organization are automatically activated and assume external authentication."))
  sso_provider = models.CharField(max_length=32, blank=True, null=True, help_text=("The SSO provider (e.g. CAS, AZURE, OIDC, GOOGLE)."))
  sso_config = JSONField(default=dict, blank=True, help_text=("JSON configuration for the SSO provider."))
  send_welcome_email = models.BooleanField(default=True, help_text=("If False, suppresses welcome/added-to-course emails for users in this organization."))
  is_main_org = models.BooleanField(default=False, help_text=("If True, this organization is the main/default organization. Only one organization can be the main org at a time. The main org's SSO is used as the default login method."))

  # AI Configuration at organization level
  AI_PROVIDER_CHOICES = [
      ('gemini', 'Google Gemini'),
      ('openai', 'OpenAI'),
      ('ollama', 'Ollama (Self-hosted)'),
      ('portkey', 'Portkey (Self-hosted)'),
      ('custom', 'Custom Provider'),
  ]
  AI_COURSE_POLICY_CHOICES = [
      ('all', 'All courses'),
      ('selected', 'Selected courses only'),
      ('none', 'Disabled'),
  ]
  ai_provider = models.CharField(
      max_length=32, blank=True, null=True, choices=AI_PROVIDER_CHOICES,
      help_text="AI provider for the organization"
  )
  ai_api_key = EncryptedCharField(
      max_length=512, blank=True, null=True,
      help_text="API key for AI provider (stored encrypted)"
  )
  ai_base_url = models.URLField(
      blank=True, null=True,
      help_text="Base URL for Ollama or custom provider"
  )
  ai_model = models.CharField(
      max_length=64, blank=True, null=True,
      help_text="Default model name (e.g., gemini-1.5-flash, gpt-4)"
  )
  ai_disabled = models.BooleanField(
      default=False,
      help_text="If True, all AI features are disabled for this organization"
  )
  ai_comments_disabled = models.BooleanField(
      default=False,
      help_text="If True, AI comment generation is disabled at the organization level"
  )
  ai_course_policy = models.CharField(
      max_length=16, default='none', choices=AI_COURSE_POLICY_CHOICES,
      help_text="Controls which courses can use the organization's AI configuration: 'all', 'selected', or 'none'"
  )
  ai_enabled_courses = models.ManyToManyField(
      'Course', blank=True, related_name='ai_enabled_by_organizations',
      help_text="Courses explicitly enabled for the organization's AI configuration (used when ai_course_policy is 'selected')"
  )
  ai_token_rates = JSONField(
      default=dict, blank=True,
      help_text='Custom per-model token rates. JSON object mapping model names to {"input": <$/1M tokens>, "output": <$/1M tokens>}'
  )
  ai_feature_config = JSONField(
      default=dict, blank=True,
      help_text='Per-feature AI toggles. JSON: {"comment_generation": true, "suggested_comments": false, ...}. Missing keys use defaults (enabled).'
  )
  ai_feature_models = JSONField(
      default=dict, blank=True,
      help_text='Per-feature AI model overrides. JSON: {"quiz_generation": "gemini-2.5-pro", ...}. Missing keys use ai_model.'
  )

  class Meta:
    ordering = ('name',)

  def save(self, *args: Any, **kwargs: Any) -> None:
    if self.is_main_org:
      Organization.objects.exclude(pk=self.pk).filter(is_main_org=True).update(is_main_org=False)
    super().save(*args, **kwargs)

  def __str__(self):
    return self.shortname


def get_main_org() -> Optional['Organization']:
  """Returns the main organization, checking MAIN_ORG_ID env var first, then the DB flag."""
  from django.conf import settings as django_settings
  main_org_id = getattr(django_settings, 'MAIN_ORG_ID', None)
  if main_org_id:
    try:
      return Organization.objects.get(pk=int(main_org_id))
    except (Organization.DoesNotExist, ValueError):
      pass
  return Organization.objects.filter(is_main_org=True).first()


# Internal Model - not published in public API
# https://wsvincent.com/django-custom-user-model-tutorial/


class Profile(BaseModel):
  if TYPE_CHECKING:
    id: int
    
  user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile", help_text=(
      "The username of the related user."))
  api_token = models.ForeignKey(
      Token, on_delete=models.SET_NULL, blank=True, null=True)
  organization = models.ForeignKey(Organization, on_delete=models.CASCADE, blank=True,
                                   null=True, related_name="profiles", help_text=("The related organization_id"))
  canCreateCourses = models.BooleanField(default=False)
  canModifyRosters = models.BooleanField(default=False)
  isOrgStaff = models.BooleanField(default=False, help_text=("If True, user can manage Organization settings (SSO, Defaults)."))
  pendingValidation = models.BooleanField(default=False)
  showProductTips = models.BooleanField(default=True)
  isPasswordSet = models.BooleanField(default=False, help_text=(
      "A boolean field. If True, the user has set a password for their account. If False, the user has not set a password and should be prompted to do so."))

  stripeCustomerId = models.CharField(max_length=96, unique=True, null=True, blank=True, help_text=(
      "The customer_id from the Stripe customer object."))

  isServiceAccount = models.BooleanField(default=False, help_text=(
      "If True, this user is an auto-created service account for a course API key."))

  def __init__(self, *args, **kwargs):
    # Remove deprecated stripeCustomerId if provided
    if 'stripeCustomerId' in kwargs:
      kwargs.pop('stripeCustomerId')
    super().__init__(*args, **kwargs)

  def __str__(self):
    return self.user.email

class OneTimeToken(BaseModel):
  if TYPE_CHECKING:
    id: int
    user: models.ForeignKey[User, User]

  user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="one_time_tokens", help_text=(
      "The username of the related user."))
  token = models.CharField(max_length=255, unique=True, help_text=(
      "The one-time token string."), default=uuid.uuid4)
  expires_at = models.DateTimeField(default=get_default_token_expiry)

  used = models.BooleanField(default=False, help_text=("Whether the token has been used."))
  course = models.ForeignKey('Course', on_delete=models.CASCADE, null=True, blank=True,
      related_name="one_time_tokens", help_text=("If set, the JWT issued from this OTT is scoped to this course."))

  def is_valid(self):
    return not self.used and timezone.now() < self.expires_at

  def __str__(self):
    return f"OneTimeToken for {self.user.email} expiring at {self.expires_at}"

class Course(BaseModel):
  if TYPE_CHECKING:
    id: int
    sections: RelatedManager[Section]
    assignments: RelatedManager[Assignment]
    files: RelatedManager[CourseFile]

  name = models.CharField(max_length=36, help_text=("The name of the course."))
  organization = models.ForeignKey(Organization, on_delete=models.CASCADE,
                                   related_name="courses", help_text=("The related organization_id"))
  period = models.CharField(max_length=32, help_text=(
      "A string describing the period (e.g. F2019, T32019, etc."))
  archived = models.BooleanField(default=False, help_text=("If True, the course will not be editable."))

  # ManyToMany relationships - accessed via Manager (e.g., course.students.all())
  students = models.ManyToManyField(User, related_name="student_courses", help_text=(
      "A list of usernames of students enrolled in the course."))
  inactive_students = models.ManyToManyField(
      User,
      related_name="student_inactive_courses",
      help_text=("A list of usernames of students unenrolled in the course."),
      blank=True,
  )
  inactive_graders = models.ManyToManyField(
    User,
    related_name="grader_inactive_courses",
    help_text=(
      "A list of usernames of graders inactive in the course."),
    blank=True,
    )
  inactive_courseAdmins = models.ManyToManyField(
    User, 
    related_name="courseAdmin_inactive_courses", 
    help_text=(
      "A list of usernames of admins inactive in the course."),
    blank=True,
  )
  graders = models.ManyToManyField(User, related_name="grader_courses", help_text=(
      "A list of usernames of graders for the course."))
  superGraders = models.ManyToManyField(User, related_name="superGrader_courses", help_text=(
      "A list of usernames of graders for the course who have expanded permissions."))
  rubricEditors = models.ManyToManyField(User, related_name="rubricEditor_courses", help_text=(
      "A list of usernames of graders for the course who are allowed to edit the rubric."))
  quizGraders = models.ManyToManyField(User, related_name="quizGrader_courses", blank=True, help_text=(
      "Graders explicitly allowed to grade quizzes when gradersCanGradeQuizzes is off. "
      "Course admins can always grade quizzes."))
  courseAdmins = models.ManyToManyField(User, related_name="courseAdmin_courses", help_text=(
      "A list of usernames for admins for the course."))

  # Settings
  sendReleasedSubmissionsToBack = models.BooleanField(default=False, help_text=(
      "A boolean field. If True, submissions that are claimed and subsequently released will be added to the back of the grading queue."))
  showStudentsStatistics = models.BooleanField(default=False, help_text=(
      "A boolean field. If True, students will be able to view basic grade statistics for released assignments."))
  timezone = models.CharField(default="US/Eastern", max_length=32, help_text=("Timezone in which course operates."))
  emailNewUsers = models.BooleanField(default=False, help_text=(
      "A boolean field. If True, when emails are added to a course roster that do not correspond to existing codePost users, those emails will be sent an email notifying that they have been added to a course and providing a link to register their (new) accounts."))
  anonymousGradingDefault = models.BooleanField(default=False, help_text=(
      "A boolean field. If True, new assignments will have anonymous grading mode enabled by default."))

  allowGradersToEditRubric = models.BooleanField(default=False, help_text=(
      "A boolean field. If True, graders will be allowed to add and update unlinked rubric comments."))
  gradersCanGradeQuizzes = models.BooleanField(default=True, help_text=(
      "A boolean field. If True (default), all course graders may view and grade quiz attempts. "
      "If False, quiz grading is restricted to course admins and the designated quizGraders."))
  minComments = models.IntegerField(default=0, help_text=(
      "An integer representing the minimum number of comments that graders are asked to make prior to finalizing. 0 indicates no minimum."))
  noUnfinalize = models.BooleanField(default=False, help_text=(
      "If True, only admins can unfinalize submissions."))
  lateDayCreditsAllowable = models.IntegerField(blank=True, null=True, help_text=(
      "The number of Late Day Credits that each student gets at the beginning of the term. Null if the course does not have students submit directly to codePost."))
  rosterMap = JSONField(default={}, help_text="An map of LMS id to codePost student emails for this course.", blank=True)
  studentCaptions = JSONField(default={}, help_text="Map student emails to captions.", blank=True)
  useStudentCaptions = models.BooleanField(default=False, help_text=(
      "If True, graders will see student captions (where defined) instead of emails."))
  activateQueue = models.BooleanField(default=True, help_text=(
      "If True, will be able to claim submissions from the ungraded queue."))
  inviteCode = models.CharField(max_length=10, help_text=("A token which allows students to join course."), null=True, blank=True, unique=True)

  course = property(lambda self: self)

  emailWhitelist = models.TextField(blank=True, help_text=("Permissible student email domains."))
  inviteCodeEnabled = models.BooleanField(default=False, help_text=("If True, the course's invite code can be used."))
  enableStudentFeedbackNotifications = models.BooleanField(default=False, help_text=("If True, the graders may send students feedback notifications."))

  def validate_manual_payments(self):
    if not isinstance(self, list):
        raise ValidationError('Must be an array')

    for item in self:
        if not set(['id', 'timestamp', 'amount', 'description', 'email']).issubset(item.keys()):
            raise ValidationError('Each manual payment must have an id, timestamp, amount, description, and email field')
        
  manual_payments = JSONField(default=list, help_text="An array of manual payments", validators=[validate_manual_payments], blank=True)
  waiver_requested = models.BooleanField(default=False, help_text=("If True, the course has requested a waiver."))
  studentsCanSeeGraders = models.BooleanField(default=False, help_text=(
      "If True, students can see the grader who graded their submission."))
  
  expiration_date = models.DateTimeField(null=True, blank=True, help_text=("The date when the course will be automatically deleted."))

  # AI Configuration for comment generation
  AI_PROVIDER_CHOICES = [
      ('gemini', 'Google Gemini'),
      ('openai', 'OpenAI'),
      ('ollama', 'Ollama (Self-hosted)'),
      ('portkey', 'Portkey (Self-hosted)'),
      ('custom', 'Custom Provider'),
  ]
  ai_provider = models.CharField(
      max_length=32,
      blank=True,
      null=True,
      choices=AI_PROVIDER_CHOICES,
      help_text="AI provider for comment generation"
  )


  ai_api_key = EncryptedCharField(
      max_length=512,
      blank=True,
      null=True,
      help_text="API key for AI provider (stored encrypted)"
  )
  ai_base_url = models.URLField(
      blank=True,
      null=True,
      help_text="Base URL for Ollama or custom provider"
  )
  ai_model = models.CharField(
      max_length=64,
      blank=True,
      null=True,
      help_text="Model name (e.g., gemini-1.5-flash, gpt-4)"
  )
  ai_disabled = models.BooleanField(
      default=False,
      help_text="If True, all AI features are disabled even if configured"
  )
  ai_comments_disabled = models.BooleanField(
      default=False,
      help_text="If True, AI comment generation is disabled even if AI is globally enabled"
  )
  ai_use_own_settings = models.BooleanField(
      default=False,
      help_text="If True, course uses its own AI settings instead of the organization's configuration"
  )
  ai_token_rates = JSONField(
      default=dict, blank=True,
      help_text='Custom per-model token rates. JSON object mapping model names to {"input": <$/1M tokens>, "output": <$/1M tokens>}'
  )
  ai_feature_config = JSONField(
      default=dict, blank=True,
      help_text='Per-feature AI toggles. JSON: {"comment_generation": true, "suggested_comments": false, ...}. Missing keys use defaults (enabled).'
  )
  ai_feature_models = JSONField(
      default=dict, blank=True,
      help_text='Per-feature AI model overrides. JSON: {"quiz_generation": "gemini-2.5-pro", ...}. Missing keys use the effective default model.'
  )

  class Meta:
    unique_together = ('name', 'period', 'organization')
    ordering = ('name', 'period')

  def __str__(self):

    return str(self.name) + " | " + self.period


##########################################################################

############# Course Infrastructure Section ##############################


class Section(BaseModel):
  if TYPE_CHECKING:
    id: int
    course: models.ForeignKey[Course, Course]
    
  name = models.CharField(
      max_length=16, help_text=("The name of the section."))
  course = models.ForeignKey(Course, on_delete=models.CASCADE,
                             related_name='sections', help_text=("The related course_id."))
  leaders = models.ManyToManyField(User, blank=True, related_name='leader_sections', help_text=(
      "A list of usernames of graders leading the section."))
  students = models.ManyToManyField(User, related_name='student_sections', help_text=(
      "A list of usernames of students in the section."))

  class Meta:
    unique_together = ('name', 'course')
    ordering = ('name',)

  def __str__(self):
    
    return self.name + " | " + str(self.course)


# Assignment lifecycle. Students see visible/preview/published/closed; they can download
# files from preview onward and submit only while published (and before the deadline —
# see Assignment.effective_state). draft and archived are instructor-only.
ASSIGNMENT_STATE_CHOICES = [
    ('draft', 'Draft'),
    ('visible', 'Visible'),
    ('preview', 'Preview'),
    ('published', 'Published'),
    ('closed', 'Closed'),
    ('archived', 'Archived'),
]
STUDENT_VISIBLE_STATES = ('visible', 'preview', 'published', 'closed')
STUDENT_DOWNLOAD_STATES = ('preview', 'published', 'closed')

# Feedback axis: how grading becomes visible to students. Orthogonal to the work-axis
# `state` above, and to `hideGrades` (which masks numeric grades in ANY revealing state).
FEEDBACK_STATUS_CHOICES = [
    ('hidden', 'Hidden'),
    ('live', 'Live'),
    ('per_student', 'Per student'),
    ('released', 'Released'),
]


class Assignment(BaseModel):
  if TYPE_CHECKING:
    id: int
    environment: Environment
    submissions: RelatedManager[Submission]
    files: RelatedManager[AssignmentFile]
    rubricCategories: RelatedManager[RubricCategory]
    testCategories: RelatedManager[TestCategory]
    dataSets: RelatedManager[AssignmentDataSet]
    course: models.ForeignKey[Course, Course]
    uploadDueDate: models.DateTimeField[datetime, datetime]
    maxLateDays: models.IntegerField[int, int]

  state = models.CharField(max_length=16, choices=ASSIGNMENT_STATE_CHOICES, default='draft',
      help_text=("Lifecycle state: draft -> visible -> preview -> published -> closed -> archived. "
                 "Students see visible through closed, download files from preview onward, and "
                 "submit only while published."))
  publishedAt = models.DateTimeField(null=True, blank=True, help_text=(
      "When the assignment last entered the published state. Cleared if it moves back to a "
      "pre-published state (not on close/archive)."))
  publishAt = models.DateTimeField(null=True, blank=True, help_text=(
      "Visible/Preview assignments auto-publish at this time (checked every few minutes)."))
  scheduledPublishRanAt = models.DateTimeField(null=True, blank=True, help_text=(
      "When the scheduled publishAt run last fired (one-shot stamp). Moving publishAt past "
      "this re-arms the run."))

  explanation = models.TextField(blank=True, help_text=("The explanation of an assignment, visible to students."))
  hideFrom = models.ManyToManyField(Section, related_name="hidden_sections", help_text=("Sections from which to hide this assignment."), blank=True)

  course = models.ForeignKey(Course, on_delete=models.CASCADE,
                             related_name='assignments', help_text=("The related course_id."))
  name = models.CharField(
      max_length=32, help_text=("The name of the assignment."))
  points = models.DecimalField(validators=[MinValueValidator(0)], max_digits=5,
                               decimal_places=2, help_text=("Total points for the assignment."))
  mean = models.DecimalField(validators=[MinValueValidator(0)], max_digits=5, decimal_places=2, blank=True, null=True, help_text=(
      "The average grade of the assignment. Null if no submissions yet"))
  median = models.DecimalField(validators=[MinValueValidator(0)], max_digits=5, decimal_places=2, blank=True, null=True, help_text=(
      "The median grade of the assignment. Null if no submissions yet"))
  sortKey = models.IntegerField(default=0, help_text=(
      "Optional integer to specify the order of a Course's Assignments."))

  # Settings
  hideGrades = models.BooleanField(default=False, help_text=(
      "A boolean field. If True, numeric grades are masked from students in any revealing "
      "feedbackStatus — they still see comments and the rubric."))

  feedbackStatus = models.CharField(max_length=16, choices=FEEDBACK_STATUS_CHOICES, default='hidden',
      help_text=("How grading becomes visible to students: hidden (nothing), live (as it's "
                 "written), per_student (each student once their submission is finalized), "
                 "released (finalized submissions, globally)."))
  releaseFeedbackAt = models.DateTimeField(null=True, blank=True, help_text=(
      "Hidden/per-student assignments auto-release feedback at this time (checked every few "
      "minutes)."))
  scheduledFeedbackReleaseRanAt = models.DateTimeField(null=True, blank=True, help_text=(
      "When the scheduled releaseFeedbackAt run last fired (one-shot stamp). Moving "
      "releaseFeedbackAt past this re-arms the run."))
  feedbackReleasedAt = models.DateTimeField(null=True, blank=True, help_text=(
      "When feedbackStatus last entered 'released'. Anchors quiz close times relative to "
      "feedback release."))

  anonymousGrading = models.BooleanField(default=False, help_text=(
      "A boolean field. If 'True', graders will not have access to the students field of submission objects, unless they have elevated privileges."))
  commentFeedback = models.BooleanField(default=True, help_text=(
      "A boolean field. If True, students can provide feedback on rubric comments."))
  allowStudentUpload = models.BooleanField(default=False, help_text=(
      "A boolean field. If true, students will be allowed to upload submissions until the upload due date."))
  allowStudentUploadWithPartners = models.BooleanField(default=False, help_text=("A boolean field. If true, students will be allowed to invite partners to their submission."))
  uploadDueDate = models.DateTimeField(null=True, help_text=(
      "The date after which students are not allowed to upload submissions. Only useful if allowStudentUpload is set to True."))
  maxLateDays = models.IntegerField(default=2, help_text=(
      "An integer representing the maximum number of late days to continue to accept submissions for this assignment."))
  
  additiveGrading = models.BooleanField(default=False, help_text=(
      "A boolean field. If true, grades begin at 0 (instead of assignment.points)"))
  hideGradersFromStudents = models.BooleanField(default=True, help_text=(
      "A boolean field. If True, the graders of a submission will be hidden from students."))
  studentsCanSeeGraders = models.BooleanField(null=True, blank=True, default=None, help_text=(
      "If set, overrides course setting. If True, students can see graders for this assignment."))
  collaborativeRubricMode = models.BooleanField(default=False, help_text=(
      "A boolean field. If true, admins and graders can edit the assignment rubric inline in the code console."))

  allowRegradeRequests = models.BooleanField(default=False, help_text=(
      "A boolean field. If True, students will be allowed to submit questions and regrade requests after their submission has been graded."))
  regradeInstructions = models.TextField(default='', blank=True, help_text=(
      "Instructions (in Markdown) to show students when they submit regrade requests."))
  regradeDeadline = models.DateTimeField(null=True, help_text=(
      "The date after which students are not allowed submit a regrade request."))

  forcedRubricMode = models.BooleanField(default=False, help_text=(
      "A boolean field. If true, graders will be required to link a rubric comment on all comments."))
  templateMode = models.BooleanField(
      default=False, help_text="A boolean field. If true, admins will be able upload template code files. Those template files will be used to de-emphasize provided versus student-written code in submissions.")
  showFrequentlyUsedRubricComments = models.BooleanField(
      default=False, help_text="A boolean field. If true, an assignment's 10 most frequently used rubric comments will be shown within the code console.")

  allowLateUploads = models.BooleanField(default=False, help_text=(
      "A boolean field. If True and an uploadDueDate is set, students will still be able to submit after a deadline has passed."))
  lateDeductions = JSONField(default=[], help_text="An array of point deductions for each day late.")

  gradersCanEditSubmissions = models.BooleanField(default=False, help_text=(
      "A boolean field. If True, graders will be allowed to edit student submissions (e.g. for testing fixes)."))
  
  runFilesOnSubmit = models.BooleanField(default=True, help_text=(
      "If True, submission files will be automatically executed and cached when a student submits."))

  runTestsOnSubmit = models.BooleanField(default=True, help_text=(
      "If True, autograder tests will automatically run when a student submits."))
  
  testsAffectGrade = models.BooleanField(default=True, help_text=(
      "If True, the results of autograder tests will be included in the submission grade calculation."))

  # AI-powered comment generation
  ai_system_prompt = models.TextField(
      blank=True,
      default="" ,
      help_text="System prompt for AI comment generation. "
                "Placeholders: {assignment_name}, {file_content}, {selected_content}, {rubric_context}, {grader_draft}"
  )
  ai_summary_prompt = models.TextField(
      blank=True,
      default="",
      help_text="Per-assignment override of the submission-summary system prompt. "
                "Placeholders: {assignment_name}, {assignment_description}, {test_results}, {rubric}, "
                "{description_comparison}. Blank uses the global default."
  )
  ai_description = models.TextField(
      blank=True,
      default="",
      help_text="AI-generated description of the assignment used as context for AI grading features. "
                "Editable by course admins, visible to graders. Not shown to students."
  )
  ai_description_locked = models.BooleanField(
      default=False,
      help_text="When True, prevents automatic regeneration of ai_description from new submissions."
  )


  def __str__(self):
    return str(self.name) + " | " + str(self.course)

  def submission_deadline(self):
    """Final moment uploads are accepted, or None if no deadline.

    Single source of truth for the upload boundary — the studentUpload view and the
    derived 'closed' state (effective_state) both use it, so they can never disagree.
    """
    if not self.uploadDueDate:
      return None
    if self.allowLateUploads:
      return self.uploadDueDate + timedelta(days=self.maxLateDays)
    return self.uploadDueDate

  def effective_state(self, at=None):
    """Stored state, except a past-deadline published assignment reads as closed.

    'closed' is also storable directly (early close); the stored value wins there.
    """
    if self.state != 'published':
      return self.state
    deadline = self.submission_deadline()
    if deadline and (at or timezone.now()) > deadline:
      return 'closed'
    return 'published'

  def calculate_average_and_median(self):
    finalizedSubmissions = self.submissions.filter(isFinalized=True)
    if (len(finalizedSubmissions) == 0):
      return (None, None)

    scores = finalizedSubmissions.values_list(
        'grade', flat=True).order_by('grade')
    mean = scores.aggregate(Avg('grade'))['grade__avg']
    median = scores[int(round(len(scores) / 2))]
    return (mean, median)

  def save(self, *args, **kwargs):
    ''' Calculate mean, median on save '''
    is_new = self.pk is None
    # Stamp the feedback-release time (before super() so it lands in update_fields —
    # the quiz-deadline resync signal watches for feedbackReleasedAt changes).
    if self.feedbackStatus == 'released' and self.feedbackReleasedAt is None:
      self.feedbackReleasedAt = now()
    elif self.feedbackStatus != 'released' and self.feedbackReleasedAt is not None:
      self.feedbackReleasedAt = None
    # Stamp the publish time (mirrors feedbackReleasedAt). Cleared only when the
    # assignment moves back to a pre-published state — closed/archived keep it.
    if self.state == 'published' and self.publishedAt is None:
      self.publishedAt = now()
    elif self.state in ('draft', 'visible', 'preview') and self.publishedAt is not None:
      self.publishedAt = None
    super(Assignment, self).save(*args, **kwargs)

    if is_new:
        # Now self.pk is available
        self.mean, self.median = self.calculate_average_and_median()
        super().save(update_fields=["mean", "median"])


  class Meta:
    unique_together = ('name', 'course')
    ordering = ('sortKey', 'name')
    indexes = [
        models.Index(fields=['course', 'state']),
    ]
    


class RubricCategory(BaseModel):
  if TYPE_CHECKING:
    id: int
    assignment: Assignment
    assignment_id: int | None
    rubricComments: RelatedManager[RubricComment]

  assignment: Assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE,  # type: ignore[assignment]
                                 related_name="rubricCategories", help_text=("The related assignment_id."))
  name = models.CharField(max_length=72, help_text=(
      "The name of the category (e.g. 'General')."))
  pointLimit = models.IntegerField(blank=True, null=True, help_text=(
      "An integer cap for the maximum number of points that can be deducted under this category."))
  sortKey = models.IntegerField(default=0, help_text=(
      "Optional integer to specify the order of an Assignment's Rubric Categories"))
  helpText = models.TextField(
      blank=True, help_text=("Subtext for the category name."))
  atMostOnce = models.BooleanField(default=False, help_text=(
      "A boolean field. If True, at most one rubric comment from this rubric category can be applied to a submission."))

  course = property(lambda self: self.assignment.course)



class RubricComment(BaseModel):
  if TYPE_CHECKING:
    id: int
    category: RubricCategory
    category_id: int | None
    comments: RelatedManager[Comment]

  text = models.TextField(blank=True, help_text=("The text on the rubric comment."))
  explanation = models.TextField(blank=True, help_text=("The explanation of a rubric comment shown to students."))
  instructionText = models.TextField(blank=True, help_text=(
      "Text shown to the grader in the custom text field of an instance comment."))
  templateTextOn = models.BooleanField(default=False, help_text=(
      "If True, instruction text will pre-populate instance comments."))
  pointDelta = models.DecimalField(max_digits=5, decimal_places=2, help_text=(
      "The points deducted. A negative number represents a bonus."))
  category: RubricCategory = models.ForeignKey(RubricCategory, on_delete=models.CASCADE,  # type: ignore[assignment]
                               related_name="rubricComments", help_text=("The related rubricCategory_id"))
  sortKey = models.IntegerField(default=0, help_text=(
      "Optional integer to specify the order of a Rubric Category's comments."))
  name = models.CharField(max_length=255, null=True, blank=True)


  course = property(lambda self: self.category.course)


###############################################################################


############# Submissions Section #############################################

class Submission(BaseModel):
  if TYPE_CHECKING:
    id: int
    assignment: Assignment
    grader: User | None
    questionResponder: User | None
    files: RelatedManager[SubmissionFile]
    histories: RelatedManager[SubmissionHistory]
    tests: RelatedManager[SubmissionTest]

  assignment: Assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE,  # type: ignore[assignment]
                                 related_name="submissions", help_text=("The related assignment_id."))
  students = models.ManyToManyField(User, related_name="student_submissions", help_text=(
      "A list of usernames of students for the submission."))
  grader: User | None = models.ForeignKey(User, blank=True, null=True, on_delete=models.SET_NULL, related_name="grader_submissions", help_text=(  # type: ignore[assignment]
      "The username of the assigned grader for the submission."))
  isFinalized = models.BooleanField(default=False, help_text=(
      "A boolean field. 'True' if the submission is finalized. 'False' otherwise."))
  dateEdited = models.DateTimeField(default=now, help_text=(
      "The date this submission (or any of its associated files or comments) was last edited."))
  grade = models.DecimalField(validators=[MinValueValidator(0)], max_digits=5, decimal_places=2,
                              blank=True, null=True, help_text=("The grade for the submission. Null if not graded yet."))
  queueOrderKey = models.IntegerField(default=0, help_text=(
      "Index used to order the queue from which graders draw submissions. Will sort low to high."))
  gradeFrozen = models.BooleanField(default=False, help_text=(
      "A boolean field. If 'True', the submissions grade will not be re-calculated based on the current comments applied to it. Warning: this can cause grade to become out of sync with the submission's comments."))
  dateUploaded = models.DateTimeField(
      default=None, help_text=("The date this submission was created. None if just created, and files haven't been uploaded yet. Used for Celery tasks."), null=True)

  lateDayCreditsUsed = models.IntegerField(default=0, help_text=(
      "The number of Late Day Credits used by the Submission."))

  # Student question
  questionIsOpen = models.BooleanField(default=False, help_text=(
      "A boolean field. If true the submission has an open question."))
  questionIsRegrade = models.BooleanField(default=False, help_text=(
      "A boolean field. If 'True', the submission's question is a regrade request."))
  questionResponder: User | None = models.ForeignKey(User, blank=True, null=True, on_delete=models.SET_NULL,  # type: ignore[assignment]
                                        related_name="grader_regraded_submissions", help_text=("The username of the responder for the question."))
  questionText = models.CharField(
      blank=True, max_length=500, help_text=("The text of the question."))
  questionResponse = models.TextField(blank=True, help_text=("The text of the question response."))
  questionDate = models.DateTimeField(null=True, blank=True, help_text=(
      "The date the request / question was submitted."))
  responseDate = models.DateTimeField(
      null=True, blank=True, help_text=("The date the response was submitted."))
  testRunsCompleted = models.PositiveIntegerField(default=0, help_text=(
      "Number of times exposed tests have been run for a submission. It only increments if the maxStudentTestRuns Environment setting is on."))

  course = property(lambda self: self.assignment.course)

  def save(self, *args, **kwargs):
    # Always recalculate grade when not frozen (allows tests to affect grade before finalization)
    if not self.gradeFrozen:
      self.grade = calculate_grade(self)
    self.dateEdited = now()


    super(Submission, self).save(*args, **kwargs)
  class Meta:
    ordering = ('queueOrderKey', 'dateUploaded')


class FileTemplate(BaseModel):
  """Deprecated model - kept for backwards compatibility only."""
  pass


def normalize_file_data(data: str) -> str:
  """Normalize newlines for plain-text content (data URIs are binary — skipped) and
  ensure the result is valid utf-8. Shared by File.save and CourseFileContent.save."""
  if not data.startswith('data:'):
    if '\\r\\n' in data:
      data = data.replace("\\r\\n", "\\n")
  return data.encode('utf-8').decode('utf-8')


class File(BaseModel):
  if TYPE_CHECKING:
    id: int
    data: models.TextField[str, str]
    name: models.CharField[str, str]
    extension: models.CharField[str, str]
  
  name = models.CharField(max_length=250, help_text=("The name of the file."))
  data = models.TextField(help_text=("The data in a file. should be utf-8 encoded text."), default="", null=False)
  extension = models.CharField(max_length=36, help_text=(
      "The extension for the file (e.g. '.java' or '.py'"))
  path = models.CharField(null=True, blank=True, max_length=500, help_text=(
      "Optional file path, delimited by slashes, to indicate a directory structure."))

  course = property(lambda self: self.get_course())
  hash = models.CharField(max_length=256, help_text=("The SHA-256 hash of the file."), default="")
  
  def __repr__(self):
    return super().__repr__() + f" (name={self.name}, extension={self.extension})"
  
  
  def get_course(self):
    try:
      return self.submissionfile.submission.assignment.course  # type: ignore[attr-defined]
    except (AttributeError, ObjectDoesNotExist):
      pass
    try:
      return self.assignmentfile.assignment.course  # type: ignore[attr-defined]
    except (AttributeError, ObjectDoesNotExist):
      pass
    try:
      return self.coursefile.course  # type: ignore[attr-defined]
    except (AttributeError, ObjectDoesNotExist):
      pass
    return None
  def get_file_info(self) -> tuple[Optional[Submission], Optional[Assignment], Optional[Course]]:
    """ Returns the associated Submission, Assignment, and Course for this File, if any."""
    submission = None
    assignment = None
    course = None
    
    try:
      if hasattr(self, 'submissionfile'):
        submission = self.submissionfile.submission  # type: ignore[attr-defined]
        assignment = submission.assignment
        course = assignment.course
      elif hasattr(self, 'assignmentfile'):
        assignment = self.assignmentfile.assignment  # type: ignore[attr-defined]
        course = assignment.course
      elif hasattr(self, 'coursefile'):
        course = self.coursefile.course  # type: ignore[attr-defined]
    except (AttributeError, ObjectDoesNotExist):
      # Related objects may have been deleted during cascade deletion
      pass

    return submission, assignment, course

  @staticmethod
  def get_file_obj(file_id: int) -> tuple[File|SubmissionFile|AssignmentFile|CourseFile, Optional[Submission], Optional[Assignment], Optional[Course]]:
    file = File.objects.get(id=file_id)
    if not file:
      raise ValueError(f"File with id {file_id} not found")
    submission = None
    assignment = None
    course = None
    try:
      if hasattr(file, 'submissionfile'):
        submission = file.submissionfile.submission # type: ignore[attr-defined]
        assignment = submission.assignment
        course = assignment.course
      elif hasattr(file, 'assignmentfile'):
        assignment = file.assignmentfile.assignment  # type: ignore[attr-defined]
        course = assignment.course
      elif hasattr(file, 'coursefile'):
        course = file.coursefile.course  # type: ignore[attr-defined]
    except (AttributeError, ObjectDoesNotExist):
      # Related objects may have been deleted during cascade deletion
      pass

    return file, submission, assignment, course
  
  @property
  def handler(self):
      """
      Returns the appropriate FileHandler for this file.
      Cached on the instance to avoid re-creation.
      """
      if not hasattr(self, '_handler'):
          from core.services.file_handlers.factory import FileHandlerFactory
          self._handler = FileHandlerFactory.get_handler(self)
      return self._handler

  def __getattr__(self, name):
      # Delegate unknown attributes to the handler
      # Avoid recursion if handler is missing
      if name in ['_handler', 'handler', 'code']:
          raise AttributeError(name)
          
      try:
          return getattr(self.handler, name)
      except AttributeError:
          raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

  # ── Magic byte signatures for data URI MIME validation ────────────────────
  # Maps MIME type prefixes to a list of valid magic byte sequences.
  # Each entry is (offset, bytes) — the signature must appear at the given offset.
  # Only MIME types that appear in data URIs from the frontend need entries here.
  MIME_SIGNATURES: dict[str, list[tuple[int, bytes]]] = {
      # ── Images ──
      'image/png':       [(0, b'\x89PNG\r\n\x1a\n')],
      'image/jpeg':      [(0, b'\xff\xd8\xff')],
      'image/gif':       [(0, b'GIF87a'), (0, b'GIF89a')],
      'image/webp':      [(8, b'WEBP')],                    # RIFF????WEBP
      'image/bmp':       [(0, b'BM')],
      'image/tiff':      [(0, b'II\x2a\x00'), (0, b'MM\x00\x2a')],  # little-endian / big-endian
      'image/x-icon':    [(0, b'\x00\x00\x01\x00'), (0, b'\x00\x00\x02\x00')],  # ICO / CUR
      'image/avif':      [(4, b'ftyp')],                    # ISOBMFF container like MP4
      'image/heic':      [(4, b'ftyp')],                    # ISOBMFF container
      'image/heif':      [(4, b'ftyp')],                    # ISOBMFF container
      'image/jxl':       [(0, b'\xff\x0a'), (0, b'\x00\x00\x00\x0cJXL \r\n\x87\n')],  # raw / ISOBMFF

      # ── Documents ──
      'application/pdf': [(0, b'%PDF')],

      # ── Archives & compressed ──
      'application/zip':              [(0, b'PK\x03\x04'), (0, b'PK\x05\x06'), (0, b'PK\x07\x08')],
      'application/java-archive':     [(0, b'PK\x03\x04')],                     # JAR is a ZIP
      'application/x-zip-compressed': [(0, b'PK\x03\x04')],                     # alternate ZIP MIME
      'application/gzip':             [(0, b'\x1f\x8b')],
      'application/x-gzip':           [(0, b'\x1f\x8b')],
      'application/x-bzip2':          [(0, b'BZh')],
      'application/x-xz':             [(0, b'\xfd7zXZ\x00')],
      'application/x-7z-compressed':  [(0, b'7z\xbc\xaf\x27\x1c')],
      'application/x-rar-compressed': [(0, b'Rar!\x1a\x07\x00'), (0, b'Rar!\x1a\x07\x01\x00')],  # RAR4 / RAR5
      'application/x-tar':            [(257, b'ustar')],                         # POSIX tar
      'application/zstd':             [(0, b'\x28\xb5\x2f\xfd')],

      # ── Executables & bytecode ──
      'application/x-sqlite3':        [(0, b'SQLite format 3\x00')],
      'application/java-vm':          [(0, b'\xca\xfe\xba\xbe')],               # Java .class
      'application/wasm':             [(0, b'\x00asm')],                         # WebAssembly
      'application/x-elf':            [(0, b'\x7fELF')],                        # Linux ELF
      'application/x-executable':     [(0, b'\x7fELF')],
      'application/x-mach-binary':    [(0, b'\xfe\xed\xfa\xce'), (0, b'\xfe\xed\xfa\xcf'),   # Mach-O 32/64
                                       (0, b'\xce\xfa\xed\xfe'), (0, b'\xcf\xfa\xed\xfe')],  # Mach-O reversed
      'application/x-msdownload':     [(0, b'MZ')],                             # PE / DOS EXE
      'application/x-dosexec':        [(0, b'MZ')],

      # ── Microsoft Office (OOXML = ZIP-based) ──
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document':   [(0, b'PK\x03\x04')],
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':         [(0, b'PK\x03\x04')],
      'application/vnd.openxmlformats-officedocument.presentationml.presentation': [(0, b'PK\x03\x04')],
      # Legacy Office (OLE2 Compound Document)
      'application/msword':            [(0, b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1')],
      'application/vnd.ms-excel':      [(0, b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1')],
      'application/vnd.ms-powerpoint': [(0, b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1')],

      # ── Audio ──
      'audio/mpeg':      [(0, b'\xff\xfb'), (0, b'\xff\xf3'), (0, b'\xff\xf2'), (0, b'ID3')],
      'audio/wav':       [(0, b'RIFF')],                    # RIFF????WAVE
      'audio/ogg':       [(0, b'OggS')],
      'audio/flac':      [(0, b'fLaC')],
      'audio/aac':       [(0, b'\xff\xf1'), (0, b'\xff\xf9')],  # ADTS frames
      'audio/midi':      [(0, b'MThd')],
      'audio/x-midi':    [(0, b'MThd')],
      'audio/webm':      [(0, b'\x1a\x45\xdf\xa3')],       # EBML header

      # ── Video ──
      'video/mp4':       [(4, b'ftyp')],
      'video/webm':      [(0, b'\x1a\x45\xdf\xa3')],       # EBML header (Matroska/WebM)
      'video/x-matroska':[(0, b'\x1a\x45\xdf\xa3')],       # MKV uses same EBML header
      'video/quicktime': [(4, b'ftyp')],                    # MOV
      'video/x-msvideo': [(0, b'RIFF')],                    # AVI = RIFF????AVI
      'video/x-flv':     [(0, b'FLV\x01')],
      'video/mpeg':      [(0, b'\x00\x00\x01\xba'), (0, b'\x00\x00\x01\xb3')],  # MPEG-PS / MPEG-1

      # ── Fonts ──
      'font/woff':       [(0, b'wOFF')],
      'font/woff2':      [(0, b'wOF2')],
      'font/otf':        [(0, b'OTTO')],
      'font/ttf':        [(0, b'\x00\x01\x00\x00')],
      'application/font-woff':  [(0, b'wOFF')],             # older MIME
      'application/font-woff2': [(0, b'wOF2')],

      # ── Misc ──
      'application/x-shockwave-flash': [(0, b'CWS'), (0, b'FWS'), (0, b'ZWS')],  # SWF
      'application/x-apple-diskimage': [(0, b'x\x01\x73\x0d\x62\x62\x60')],      # DMG (zlib start)
  }

  def _validate_data_uri_mime(self) -> None:
      """Validate that the binary content matches the MIME type in the data URI.

      Raises ValidationError if the MIME type has a known signature that
      does not match the decoded bytes. Unknown MIME types are allowed through
      (we only reject known mismatches, not unknown types).
      """
      validate_data_uri_mime(self.name, self.data)

  def effective_data(self) -> str:
    """Content for consumers that read File.data polymorphically (execution, /files/).

    CourseFile rows keep their inherited data column empty — the real content lives on
    the shared CourseFileContent; every other File subclass stores data on the row.
    """
    try:
      cf = self if isinstance(self, CourseFile) else self.coursefile  # type: ignore[attr-defined]
      return cf.content.data or ''
    except (AttributeError, ObjectDoesNotExist):
      return self.data or ''

  def save(self, *args, **kwargs):
    # Check if trying to use deprecated 'code' field
    if hasattr(self, 'code') and self.code:  # type: ignore[attr-defined]
      raise Exception("File.code is deprecated. Use File.data instead.")

    if self.data.startswith('data:'):
        # Validate that the MIME type claimed in the data URI matches the actual
        # binary content (magic bytes). Prevents uploading malicious content
        # disguised as a benign file type.
        self._validate_data_uri_mime()
    self.data = normalize_file_data(self.data)
    self.hash = hashlib.sha256(self.data.encode('utf-8')).hexdigest()
    
    # Infer extension from name if not provided
    if not self.extension:
      match = re.search(r'(\.[^.]+)$', self.name)
      if match:
        self.extension = match.group(1)
      else:
        # extension is required for text files
        raise ValidationError("File extension could not be inferred from file name. Please provide an extension.")
      
    
    return super(File, self).save(*args, **kwargs)



def validate_data_uri_mime(name: str, data: str) -> None:
  """Validate that data-URI binary content matches its claimed MIME type (magic bytes).

  Raises ValidationError on a known mismatch; unknown MIME types are allowed through.
  Module-level so CourseFileContent writes get the same check as File.save.
  """
  try:
      header, encoded = data.split(',', 1)
  except ValueError:
      return  # Malformed data URI — let other validation handle it

  # Parse MIME from "data:<mime>;base64" or "data:<mime>;charset=...;base64"
  mime = header.replace('data:', '').split(';')[0].strip().lower()
  if not mime:
      return

  signatures = File.MIME_SIGNATURES.get(mime)
  if signatures is None:
      return  # No known signature for this MIME — allow through

  try:
      raw = base64.b64decode(encoded)
  except Exception:
      raise ValidationError(
          f"File '{name}': data URI claims {mime} but contains invalid base64."
      )

  for offset, magic in signatures:
      if len(raw) >= offset + len(magic) and raw[offset:offset + len(magic)] == magic:
          return  # Match found

  raise ValidationError(
      f"File '{name}': content does not match the claimed MIME type '{mime}'. "
      "The file may be corrupted or mislabeled."
  )


class SubmissionFile(File):
  if TYPE_CHECKING:
    id: int
    submission: Submission
    comments: RelatedManager[Comment]

  submission: Submission = models.ForeignKey(Submission, on_delete=models.CASCADE,  # type: ignore[assignment]
                                  related_name="files", help_text=("The related submission_id."))
  hiddenBeforePublish = models.BooleanField(default=False, help_text=(
      "Whether this file should hidden to students before their feedback has been published. This is for autogenerated test files that shouldn't be exposed to students on upload."))


class SubmissionFileEdit(BaseModel):
  if TYPE_CHECKING:
    id: int

  file: models.OneToOneField[SubmissionFile, SubmissionFile] = models.OneToOneField(
    SubmissionFile,
    on_delete=models.CASCADE,
    related_name='edit',
    help_text='The submission file this persisted edit belongs to.',
  )  # type: ignore[assignment]
  data: models.TextField[str, str] = models.TextField(
    default='',
    help_text='The persisted edited contents for this submission file (set by an instructor or, if allowed, a grader).',
  )
  lastEditedBy: models.ForeignKey[User | None, User | None] = models.ForeignKey(
    django_settings.AUTH_USER_MODEL,
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name='submission_file_edits',
    help_text='The most recent user to save this persisted edit.',
  )  # type: ignore[assignment]

def dataset_upload_path(instance: AssignmentDataSet|Assignment, filename: str) -> str:
  """
  Generate hierarchical upload path for dataset files.
  Path format: assignment_datasets/<org_shortname>/<course_name>/<period>/<assignment_name>/<filename>
  """
  if isinstance(instance, AssignmentDataSet):
    assignment = instance.assignment
  else:
    assignment = instance
  course = assignment.course
  org = course.organization
  # Sanitize path components for filesystem safety
  org_safe = slugify(org.shortname)
  course_safe = slugify(course.name)
  period_safe = slugify(course.period)
  assignment_safe = slugify(assignment.name)
  filename_safe = os.path.basename(filename)
  
  return f'{org_safe}/{course_safe}/{period_safe}/{assignment_safe}/{filename_safe}'


class AssignmentDataSet(BaseModel):
  if TYPE_CHECKING:
    id: int
    assignment: models.ForeignKey[Assignment, Assignment]

  assignment = models.ForeignKey("Assignment", on_delete=models.CASCADE, related_name="dataSets", help_text=("The related assignment_id."))  
  name = models.CharField(max_length=64, help_text=("The name of the data set."))
  description = models.TextField(blank=True, help_text=("Optional description of the data set."))
  file = models.FileField(upload_to=dataset_upload_path, help_text=("The data set file"))
  
  # Path where the dataset should be mounted in the execution environment
  # Defaults to ~/shared/<dataset_name> to match existing student environment
  mount_path = models.CharField(
    max_length=256, 
    blank=True,
    help_text=("Path where dataset will be mounted in execution environment. Can be an absolute path (e.g. '/etc/config.json') or a relative path (e.g. 'mnist'). Relative paths are mounted under '/shared'."))
  
  # Whether this dataset is available to students during upload/execution
  is_active = models.BooleanField(
    default=True,
    help_text=("If True, this dataset will be mounted during code execution."))
  
  hidden = models.BooleanField(
    default=False,
    help_text=("If True, this dataset will be hidden from students."))

  # Whether this dataset is a test resource (linked to a TestCategory)
  is_test_resource = models.BooleanField(
      default=False,
      help_text="If True, this dataset is used as a resource for a TestCategory.")

  # Per-student variant pool: when True, this dataset is one of several interchangeable
  # variants for the assignment (same mount_path across the pool) and students are each
  # deterministically assigned exactly one, rather than all sharing this file.
  is_student_variant = models.BooleanField(
      default=False,
      help_text=("If True, this dataset is one variant in a per-student pool — each "
                 "student is assigned exactly one variant from the pool (see "
                 "StudentDataSetAssignment) instead of everyone sharing this file."))

  # Autograder scope for a variant pool: rerun a submission against every other variant
  # (not just the student's own) to catch code that's hardcoded to one dataset's numbers.
  autogradeAllVariants = models.BooleanField(
      default=False,
      help_text=("Only meaningful when is_student_variant is True. If True, the autograder "
                 "reruns a finalized submission against every OTHER variant in the pool "
                 "(in addition to the student's own), recording one SubmissionVariantRun per "
                 "variant — an anti-hardcoding check. Must be set consistently across a pool."))

  course = property(lambda self: self.assignment.course)

  def save(self, *args, **kwargs):
    # Test resources should never be student-visible.
    if self.is_test_resource:
      self.hidden = True

    # ~/shared/x, /srv/shared/x, /shared/x ... all mean the shared folder: store one form.
    from core.services.mount_paths import normalize_mount_path
    self.mount_path = normalize_mount_path(self.mount_path)

    # Set default mount_path if not provided
    if not self.mount_path and self.name:
      # Sanitize name for filesystem use - keep dots for file extensions
      safe_name = self.name.lower().replace(' ', '_')
      safe_name = ''.join(c for c in safe_name if c.isalnum() or c in '_-.')
      self.mount_path = f'shared/{safe_name}'

    # A per-student variant pool must all mount at the same path — that's what lets
    # student code reference the dataset by one fixed path regardless of which variant
    # they got. Rather than making the instructor get every variant's path to match by
    # hand, align this one to whatever the pool already uses.
    if self.is_student_variant and self.assignment_id:
      sibling = AssignmentDataSet.objects.filter(
          assignment_id=self.assignment_id, is_student_variant=True,
      ).exclude(pk=self.pk).order_by('id').first()
      if sibling is not None:
        self.mount_path = sibling.mount_path

    super(AssignmentDataSet, self).save(*args, **kwargs)
  
  def delete(self, *args, **kwargs):
    # Delete the file from disk when the dataset is deleted
    if self.file:
      try:
        self.file.delete(save=False)
      except Exception:
        pass  # File might not exist or be inaccessible
    super(AssignmentDataSet, self).delete(*args, **kwargs)
  
  class Meta:
    unique_together = ('assignment', 'name')
    ordering = ('name',)


class StudentDataSetAssignment(BaseModel):
  """Which variant of a per-student dataset pool (AssignmentDataSet.is_student_variant=True)
  a given student is assigned. Per-student data — never cloned with the assignment.
  Auto-assigned (assignedBy=None) by core.services.dataset_assignment.get_or_assign(),
  or overridden by staff."""
  if TYPE_CHECKING:
    id: int
    assignment: models.ForeignKey[Assignment, Assignment]
    student: models.ForeignKey[User, User]
    dataset: models.ForeignKey[AssignmentDataSet, AssignmentDataSet]

  assignment = models.ForeignKey("Assignment", on_delete=models.CASCADE,
      related_name="studentDataSetAssignments", help_text=("The related assignment_id."))
  student = models.ForeignKey(User, on_delete=models.CASCADE,
      related_name="dataset_assignments", help_text=("The assigned student."))
  dataset = models.ForeignKey(AssignmentDataSet, on_delete=models.CASCADE,
      related_name="student_assignments", help_text=("The variant this student was assigned."))
  assignedBy = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
      related_name="+", help_text=("The staff member who manually assigned this override. "
                 "Null means it was assigned automatically."))

  course = property(lambda self: self.assignment.course)

  class Meta:
    unique_together = ('assignment', 'student')

  def __str__(self):
    return f"StudentDataSetAssignment assignment={self.assignment_id} student={self.student_id} dataset={self.dataset_id}"


class SubmissionVariantRun(BaseModel):
  """One autograder rerun of a finalized submission against a dataset variant OTHER than the
  student's own assigned one (AssignmentDataSet.autogradeAllVariants) — an anti-hardcoding
  check: does the submission's code still work when the numbers are different?"""
  if TYPE_CHECKING:
    id: int
    submission: 'Submission'
    dataset: models.ForeignKey[AssignmentDataSet, AssignmentDataSet]

  submission = models.ForeignKey('Submission', on_delete=models.CASCADE,
      related_name="variant_runs", help_text=("The related submission_id."))
  dataset = models.ForeignKey(AssignmentDataSet, on_delete=models.CASCADE,
      related_name="variant_runs", help_text=("The variant this rerun used."))
  result = models.JSONField(null=True, blank=True,
      help_text=("{status: running|success|error, stdout, stderr, images, error, "
                 "executionTime}. Staff-internal — never shown to students."))

  course = property(lambda self: self.submission.course)

  class Meta:
    unique_together = ('submission', 'dataset')
    ordering = ('dataset__name',)

  def __str__(self):
    return f"SubmissionVariantRun submission={self.submission_id} dataset={self.dataset_id}"


class CachedExecutionResult(BaseModel):
  """
  Stores cached execution results for files (notebooks/Python scripts).
  Allows students to see grader's execution output without re-running.
  """
  if TYPE_CHECKING:
    id: int
    file: models.ForeignKey[File, File]
    executed_by: models.ForeignKey[User, User]
  
  # Generic foreign key to support any File subclass
  file = models.ForeignKey(
    "File",
    on_delete=models.CASCADE,
    related_name="cached_executions",
    help_text="The file this execution result is for"
  )
  
  # Execution metadata
  executed_by = models.ForeignKey(
    User,
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    help_text="User who executed the code"
  )
  executed_at = models.DateTimeField(
    auto_now_add=True,
    help_text="When the code was executed"
  )
  
  # File content tracking to detect changes
  file_content_hash = models.CharField(
    max_length=64,
    help_text="SHA256 hash of file content at execution time"
  )
  
  # Execution results (stored as JSON)
  output_data = models.JSONField(
    help_text="Execution output data with cells"
  )
  
  # Execution context
  execution_time_seconds = models.FloatField(
    null=True,
    blank=True,
    help_text="Time taken to execute in seconds"
  )
  
  class Meta:
    ordering = ('-executed_at',)
    indexes = [
      models.Index(fields=['file', '-executed_at']),
      models.Index(fields=['file_content_hash']),
    ]
  
  def __str__(self):
    return f"Execution of {self.file.name} at {self.executed_at}"
  
  @classmethod
  def get_cached_result(cls, file):
    """
    Get cached execution result for a file if valid.
    
    Args:
      file: File instance to get cached result for
    
    Returns:
      CachedExecutionResult if valid cache exists, None otherwise
    """
    import hashlib
    import logging
    
    logger = logging.getLogger(__name__)
    logger.info(f"[CachedExecutionResult.get] Checking cache for file {file.id}")
    
    # Get file content - CourseFiles store it on shared content, others on the row
    file_content = file.effective_data()
    if not file_content:
      logger.warning(f"[CachedExecutionResult.get] File {file.id} has no content")
      return None
    
    # Calculate current file content hash
    current_hash = hashlib.sha256(file_content.encode('utf-8')).hexdigest()
    logger.info(f"[CachedExecutionResult.get] File {file.id} current hash: {current_hash[:16]}...")
    
    # Get most recent cached result for this file
    try:
      cached = cls.objects.filter(
        file=file,
        file_content_hash=current_hash
      ).latest('executed_at')
      logger.info(f"[CachedExecutionResult.get] Cache HIT for file {file.id}, cache ID: {cached.id}")
      return cached
    except cls.DoesNotExist:
      logger.info(f"[CachedExecutionResult.get] Cache MISS for file {file.id}")
      return None
  
  def get_cached_formated_response(self,file:File):
    # Return in the same format as fresh execution
    output_data = self.output_data
    success = True
    stdout = None
    stderr = None
    error = None
    system_logs = None
    tests = None
    execution_time = self.execution_time_seconds

    # If cached output_data looks like ExecutionResult.to_dict(), unwrap and promote fields
    if isinstance(output_data, dict):
      if any(key in output_data for key in ("stdout", "stderr", "error", "success", "output_data")):
        success = output_data.get("success", True)
        stdout = output_data.get("stdout")
        stderr = output_data.get("stderr")
        error = output_data.get("error")
        system_logs = output_data.get("system_logs")
        tests = output_data.get("tests")
        if output_data.get("execution_time") is not None:
          execution_time = output_data.get("execution_time")

        # Prefer the inner output_data if present (matches fresh execution response shape)
        if output_data.get("output_data") is not None:
          output_data = output_data.get("output_data")

    response_data = {
        "success": success,
        "output_data": output_data,
        "file_id": file.id,
        "file_name": file.name,
        "error": error,
        "execution_time": execution_time,
        "cached": True,
        "executed_at": self.executed_at.isoformat(),
        "executed_by": self.executed_by.username if self.executed_by else None,
    }

    if stdout is not None:
      response_data["stdout"] = stdout
    if stderr is not None:
      response_data["stderr"] = stderr
    if system_logs is not None:
      response_data["system_logs"] = system_logs
    if tests is not None:
      response_data["tests"] = tests
    submission, assignment, course = file.get_file_info()
    if submission:
      response_data["submission_id"] = submission.id
    if assignment:
      response_data["assignment_id"] = assignment.id
    if course:
      response_data["course_id"] = course.id


    return response_data

  @classmethod
  def save_execution_result(cls, file, output_data, executed_by=None, execution_time=None):
    """
    Save execution result to cache.
    
    Args:
      file: File instance
      output_data: Execution output data (dict with cells)
      executed_by: User who executed the code
      execution_time: Time taken to execute in seconds
    
    Returns:
      Created CachedExecutionResult instance
    """
    import hashlib
    import logging
    
    logger = logging.getLogger(__name__)
    logger.info(f"[CachedExecutionResult.save] Attempting to save cache for file {file.id}")
    
    # Get file content - CourseFiles store it on shared content, others on the row
    file_content = file.effective_data()
    if not file_content:
      logger.warning(f"[CachedExecutionResult.save] File {file.id} has no content, skipping cache")
      return None
    
    # Calculate file content hash
    content_hash = hashlib.sha256(file_content.encode('utf-8')).hexdigest()
    logger.info(f"[CachedExecutionResult.save] File {file.id} content hash: {content_hash[:16]}...")
    
    # Delete old cache entries for this file with the same content hash
    # This ensures we always have the latest execution result
    try:
      old_count = cls.objects.filter(file=file, file_content_hash=content_hash).count()
      if old_count > 0:
        logger.info(f"[CachedExecutionResult.save] Deleting {old_count} old cache entries for file {file.id}")
        cls.objects.filter(file=file, file_content_hash=content_hash).delete()
    except Exception as e:
      logger.warning(f"[CachedExecutionResult.save] Failed to delete old cache entries: {e}")
    
    # Create cached result
    try:
      cached = cls.objects.create(
        file=file,
        executed_by=executed_by,
        file_content_hash=content_hash,
        output_data=output_data,
        execution_time_seconds=execution_time
      )
      logger.info(f"[CachedExecutionResult.save] Successfully saved cache for file {file.id}, cache ID: {cached.id}")
      return cached
    except Exception as e:
      logger.error(f"[CachedExecutionResult.save] Failed to create cache record: {e}", exc_info=True)
      return None


class AssignmentFile(File):
  if TYPE_CHECKING:
    id: int
    assignment: Assignment
    
  assignment: Assignment = models.ForeignKey("Assignment", on_delete=models.CASCADE,  # type: ignore[assignment]
                                 related_name="files", help_text=("The related assignment_id."))
  required = models.BooleanField(
      default=False, help_text="If student upload is enabled, a file with this name and extension will be required.")
  description = models.TextField(blank=True, help_text=("Optional description shown to students."))
  
  hidden = models.BooleanField(
    default=False,
    help_text=("If True, this file will be hidden from students (but available for tests/helpers)."))
  
  # Whether this file is a test resource (linked to a TestCategory)
  is_test_resource = models.BooleanField(
      default=False,
      help_text="If True, this file is used as a resource for a TestCategory.")
  isVisible = property(lambda self: self.assignment.state in STUDENT_DOWNLOAD_STATES)

  course = property(lambda self: self.assignment.course)

  def save(self, *args, **kwargs):
    # Test resources should never be student-visible.
    if self.is_test_resource:
      self.hidden = True
    return super(AssignmentFile, self).save(*args, **kwargs)


class CourseFileContent(BaseModel):
  """Shareable state of a CourseFile: the bytes, the public flag, and the URL token.

  Course cloning links the cloned course's CourseFile row to the SAME content, so the
  public URL (courseFiles/raw/<token>/) keeps working in cloned markdown. A physical
  copy only happens when a sharing course diverges (edits data or toggles isPublic) —
  see core.services.course_file.update_course_file_content.
  """
  if TYPE_CHECKING:
    id: int
    files: RelatedManager[CourseFile]

  data = models.TextField(default="", null=False, help_text=(
      "The data in a file. should be utf-8 encoded text."))
  isPublic = models.BooleanField(default=False, help_text=(
      "If True, the file is downloadable without authentication via its public "
      "token URL (courseFiles/raw/<token>/)."))
  token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True,
      help_text=("Unguessable token used in the public download URL."))

  def save(self, *args, **kwargs):
    if self.pk:
      was_public = (CourseFileContent.objects.filter(pk=self.pk)
                    .values_list('isPublic', flat=True).first())
      if was_public and not self.isPublic:
        # Unpublish revokes access: rotate the token so previously shared URLs 404.
        self.token = uuid.uuid4()
    self.data = normalize_file_data(self.data)
    super().save(*args, **kwargs)


class CourseFile(File):
  if TYPE_CHECKING:
    id: int
    course: Course
    content: CourseFileContent
    content_id: int

  course: Course = models.ForeignKey(Course, on_delete=models.CASCADE,  # type: ignore[assignment]
                             related_name="files", help_text=("The related course_id."))
  content: CourseFileContent = models.ForeignKey(CourseFileContent, on_delete=models.PROTECT,  # type: ignore[assignment]
                              related_name="files", help_text=("Shared file content/visibility."))
  description = models.TextField(blank=True, help_text=("Optional description shown to students."))
  studentVisible = models.BooleanField(default=False, help_text=(
      "If True, students in the course can see and download this file in their course "
      "file directory. Staff always see all files."))

  # Read-only conveniences: the shareable state lives on content. Writes must go
  # through core.services.course_file.update_course_file_content (copy-on-write).
  isPublic = property(lambda self: self.content.isPublic)
  token = property(lambda self: self.content.token)

  def save(self, *args, **kwargs):
    if self.content_id is None:
      # Create-time bootstrap: move inherited File.data into a fresh (private) content
      # row so existing CourseFile.objects.create(..., data=...) call sites keep working.
      raw = self.data or ''
      if raw.startswith('data:'):
        validate_data_uri_mime(self.name, raw)
      self.content = CourseFileContent.objects.create(data=raw)
      self.data = ''
    elif self.data:
      raise ValueError(
          "CourseFile data lives on CourseFileContent; use "
          "core.services.course_file.update_course_file_content().")
    super().save(*args, **kwargs)

  def get_course(self):
    return self.course

class CommentTag(BaseModel):
  if TYPE_CHECKING:
    id: int
    tag_comments: RelatedManager[Comment]
    
  label = models.CharField(max_length=64, unique=True, help_text=("The tag label."))

  # FIXME: Only for internal checking, should also create serializer field
  def save(self, *args, **kwargs):
    self.label = self.label.lower().strip()

    super(CommentTag, self).save(*args, **kwargs)



class Comment(BaseModel):
  if TYPE_CHECKING:
    id: int
    rubricComment: RubricComment | None
    author: User
    file: SubmissionFile
    
  text = models.TextField(blank=True, help_text=("The text on the comment"))
  pointDelta = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True, help_text=(
      "The points deducted. A negative number represents a bonus."))
  rubricComment: RubricComment | None = models.ForeignKey(RubricComment, null=True, blank=True, on_delete=models.SET_NULL,  # type: ignore[assignment]
                                    related_name="comments", help_text=("The related rubricComment_id. Null if no rubric comment linked."))
  author: User = models.ForeignKey(User, on_delete=models.CASCADE, help_text=(  # type: ignore[assignment]
      "The username of the author of the comment."))
  file: SubmissionFile = models.ForeignKey(SubmissionFile, on_delete=models.CASCADE,  # type: ignore[assignment]
                           related_name="comments", help_text=("The related file_id."))
  startChar = models.IntegerField(help_text=(
      "The starting character offset of the comment. "
      "For code/markdown files: 0-indexed character position within the start line. "
      "For PDF text-selection comments: character offset in the page's text layer. "
      "For PDF region comments: values >= 1,000,000 encode a bounding box as MARKER + leftPct*101 + topPct (percentages 0-100 of page dimensions). "
      "For Jupyter notebooks (.ipynb): always 0 (comments target entire cells)."))
  endChar = models.IntegerField(help_text=(
      "The ending character offset of the comment. "
      "For code/markdown files: 0-indexed character position within the end line. "
      "For PDF text-selection comments: character offset in the page's text layer. "
      "For PDF region comments: values >= 1,000,000 encode a bounding box as MARKER + rightPct*101 + bottomPct (percentages 0-100 of page dimensions). "
      "For Jupyter notebooks (.ipynb): always 0 (comments target entire cells)."))
  startLine = models.IntegerField(help_text=(
      "The line or position where the comment begins. "
      "For code/markdown files: 0-indexed line number. "
      "For PDF files: 1-based page number. "
      "For Jupyter notebooks (.ipynb): 0-based cell index."))
  endLine = models.IntegerField(help_text=(
      "The line or position where the comment ends. "
      "For code/markdown files: 0-indexed line number. "
      "For PDF files: 1-based page number (usually same as startLine). "
      "For Jupyter notebooks (.ipynb): 0-based cell index (usually same as startLine)."))
  feedback = models.IntegerField(default=0, help_text=(
      "Student feedback on this comment. Valid values: -1 (negative), 0 (none), 1 (positive). "
      "Only applicable when rubricComment is set."))
  color = models.CharField(max_length=7, blank=True, null=True, help_text=(
      "The color in which the comment will render in codePost."), validators=[validate_hex_color])
  tags = models.ManyToManyField(CommentTag, related_name="tag_comments",
                                help_text=("A list of tags (strings) for the comment."))

  course = property(lambda self: self.file.course)

  def save(self, *args, **kwargs):
    if self.rubricComment:
      self.pointDelta = None

    super(Comment, self).save(*args, **kwargs)


############################## Autograder #####################################################

class TestCategory(BaseModel):
  __test__ = False

  if TYPE_CHECKING:
    id: int
    assignment: Assignment
    testCases: RelatedManager[TestCase]

  assignment: Assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE,  # type: ignore[assignment]
                                 related_name="testCategories", help_text=("The related assignment__id."))
  name = models.CharField(max_length=48, help_text=("The name of the test."))
  
  testScript = models.TextField(blank=True, default="", help_text=("Python script containing @test decorated functions."))
  maxPoints = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text=("Total points available for this category."))
  sortKey = models.IntegerField(default=0, help_text=("Integer to specify the order of display."))
  targetFileName = models.CharField(max_length=255, null=True, blank=True, help_text="The name of the file this test targets.")
  
  course = property(lambda self: self.assignment.course)

  class Meta:
    unique_together = ('name', 'assignment')

testTypes = (
    ('io', 'io'),
    ('io_cli', 'io_cli'),
    ('unit', 'unit'),
    ('shell', 'shell'),
    ('file', 'file'),
    ('external', 'external'),
    ('script', 'script'),)

testCase_status_types = (
    (0, 'Passed'),
    (1, 'Failed'),
    (2, 'Error'),
    (3, 'Never run'),
)


class TestCase(BaseModel):
  __test__ = False

  if TYPE_CHECKING:
    id: int
    testCategory: TestCategory
    instances: RelatedManager[SubmissionTest]

  testCategory: TestCategory = models.ForeignKey(TestCategory, on_delete=models.CASCADE,  # type: ignore[assignment]
                                   related_name="testCases", help_text=("The related testCategory__id."))
  sortKey = models.IntegerField(default=0, help_text=(
      "Integer to specify the order of a Assignment's Tests."))
  description = models.CharField(max_length=255, help_text=("Test description."))
  type = models.CharField(max_length=25, choices=testTypes)
  pointsFail = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text=(
      "The points assigned to a failed test."))
  pointsPass = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text=(
      "The points assigned to a passed test."))
  text = models.TextField(blank=True, help_text=("The text of the test"))
  explanation = models.TextField(blank=True, help_text=("A description of what the test achieves"))
  exposed = models.BooleanField(default=False, help_text=(
      "If True and type is not 'external', this test will be run when a student submits, and the results shown to the student"))
  lastSolutionRun = models.IntegerField(default=3, choices=testCase_status_types)
  
  rubricItem = models.ForeignKey(RubricComment, null=True, blank=True, on_delete=models.SET_NULL, help_text=("The related rubric comment. If set, failure applies this rubric item."))

  functionName = models.CharField(max_length=128, blank=True, null=True, help_text=("The name of the function in the test script."))

  ################# Script / Notebook / Robust Framework Fields ########################
  # Field for custom test scripts (type='script')
  testCode = models.TextField(blank=True, help_text=("The custom test script code."))
  targetCellId = models.CharField(max_length=64, blank=True, null=True, help_text=("The ID of the notebook cell to target for execution."))
  timeout = models.IntegerField(default=30, help_text=("Execution timeout in seconds for this test."))

  hidden = models.BooleanField(default=False, help_text=(
      "If True, students see only pass/fail status and point impact — not the test name, logs, or explanation."))

  learningObjectives = models.ManyToManyField('LearningObjective', blank=True, related_name='testCases',
      help_text=("Learning objectives this test is associated with."))

  course = property(lambda self: self.testCategory.course)


LEARNING_OBJECTIVE_VISIBILITY_MODES = (
    ('always', 'Always show'),
    ('on_pass', 'Show when tests pass'),
    ('on_fail', 'Show when tests fail'),
    ('never', 'Admin only'),
)

LEARNING_OBJECTIVE_AGGREGATION_MODES = (
    ('all', 'All linked tests must pass'),
    ('any', 'At least one linked test must pass'),
    ('percentage', 'Percentage of linked tests that pass'),
    ('points_weighted', 'Weighted by test point values'),
)


class LearningObjective(BaseModel):
  if TYPE_CHECKING:
    id: int
    assignment: Assignment
    testCases: RelatedManager[TestCase]

  assignment: Assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE,  # type: ignore[assignment]
                                 related_name="learningObjectives", help_text=("The related assignment__id."))
  shortId = models.CharField(max_length=64, help_text=("Short identifier used in test decorators, e.g. 'recursion'."))
  name = models.CharField(max_length=128, help_text=("Display name for the learning objective."))
  description = models.TextField(blank=True, default="", help_text=("Optional longer description of the objective."))
  visibilityMode = models.CharField(max_length=16, choices=LEARNING_OBJECTIVE_VISIBILITY_MODES, default='always',
      help_text=("Controls when students can see this objective in test results."))
  aggregationMode = models.CharField(max_length=16, choices=LEARNING_OBJECTIVE_AGGREGATION_MODES, default='all',
      help_text=("How to aggregate results from multiple linked tests."))

  course = property(lambda self: self.assignment.course)

  class Meta:
    unique_together = ('shortId', 'assignment')

  def __str__(self):
    return f"{self.shortId} ({self.name})"


class SubmissionTest(BaseModel):
  if TYPE_CHECKING:
    id: int
    submission: Submission
    testCase: TestCase
    
  submission: Submission = models.ForeignKey(Submission, on_delete=models.CASCADE,  # type: ignore[assignment]
                                 related_name="tests", help_text=("The related submission_id."))
  testCase: TestCase = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name="instances",  # type: ignore[assignment]
                               help_text=("The related parent test id"))
  logs = models.TextField(help_text=("The logs of a test."))
  passed = models.BooleanField(help_text=(
      "A boolean field. 'True' if the submission passed this test. 'False' otherwise."))
  isError = models.BooleanField(default=False, help_text=(
      "A boolean field. 'True' if the test resulted in an error. False otherwise."))
  results = models.JSONField(null=True, blank=True, help_text=(
      "Structured test results (list of subtests)."))
  score = models.DecimalField(max_digits=7, decimal_places=2, default=0, help_text=(
      "Aggregate score earned from all subtests."))
  maxScore = models.DecimalField(max_digits=7, decimal_places=2, default=0, help_text=(
      "Maximum possible score from all subtests."))

  course = property(lambda self: self.submission.course)

  class Meta:
    ordering = ('created', )

################################################################################################
##################################### Internal Models ##########################################
################################################################################################


class SubmissionHistory(BaseModel):
  if TYPE_CHECKING:
    id: int
    submission: Submission
    student: User
    
  submission: Submission = models.ForeignKey(Submission, on_delete=models.CASCADE,  # type: ignore[assignment]
                                 related_name="histories", help_text=("The related submission_id."))
  student: User = models.ForeignKey(User, on_delete=models.CASCADE, related_name="student_submissionHistories", help_text=(  # type: ignore[assignment]
      "Username of student for the submissionHistory"))
  hasViewed = models.BooleanField(default=False, help_text=(
      "A boolean field indicating whether the student has seen the submission."))
  dateViewed = models.DateTimeField(null=True, blank=True, help_text=(
      "The date this submission (or any of its associated files or comments) was last edited."))

  course = property(lambda self: self.submission.course)

  def save(self, *args, **kwargs):
    if self.hasViewed:
      self.dateViewed = now()
    super(SubmissionHistory, self).save(*args, **kwargs)

  class Meta:
    unique_together = ('submission', 'student')
    ordering = ['-created']

##################################### Autograder Models ##########################################


class Environment(BaseModel):
  if TYPE_CHECKING:
    id: int
    assignment: Assignment
  assignment: Assignment = models.OneToOneField(Assignment, on_delete=models.CASCADE,  # type: ignore[assignment]
                                    related_name="environment", help_text=("The related assignment__id."))
  dockerRunInstructions = JSONField(default=[], blank=True, help_text="Instructions to be added to the docker file with a RUN command.")
  language = models.CharField(max_length=25, choices=(
      ('python-3.12', 'python-3.12'),
      ('python-3.11', 'python-3.11'),
      ('python-3.10', 'python-3.10'),
      ('python-3.7', 'python-3.7'),
      ('python-2.7', 'python-2.7'),
      ('java', 'java'),
      ('java-17', 'java-17'),
      ('java-11', 'java-11'),
      ('c/c++', 'c/c++'),
      ('node-20', 'node-20'),
      ('node-18', 'node-18'),
      ('r-4', 'r-4'),
      ('ruby', 'ruby'),
      ('php', 'php'),
      ('other', 'other')), default='python-3.7')
  buildType = models.CharField(max_length=25, choices=(
      ('default', 'default'),
      ('alpine', 'alpine'),
      ('ubuntu', 'ubuntu'),
      ('windows', 'windows')), default='default')
  dockerfile = models.TextField(default='', blank=True, help_text=(
      "A custom set of docker commands to append to the base image docker file"))
  compileText = models.TextField(default='', blank=True, help_text=(
      "Command to be run on every submission before tests"))
  allowNetworkAccess = models.BooleanField(default=False, help_text=(
      "A boolean field indicating whether tests should be run in a container that allows network access."))
  maxStudentTestRuns = models.PositiveIntegerField(null=True, blank=True, help_text=(
      "An integer field indicating the max times that tests will be run if tests are exposed."))
  maxExposedFailedTests = models.PositiveIntegerField(null=True, blank=True, help_text=(
      "An integer field indicating the limit of the number of failed tests that will be exposed to a student (nudge mode)."))

  buildID = models.PositiveIntegerField(default=0, help_text=(
      "An integer field making each environment build distinct"))
  
  # New fields for custom environment support
  image_name = models.CharField(max_length=255, null=True, blank=True, help_text="The Docker image name for this environment.")
  build_status = models.IntegerField(choices=((0, 'Not Built'), (1, 'Building'), (2, 'Success'), (3, 'Failed')), default=0)
  build_logs = models.TextField(default="", blank=True, help_text="Logs from the image build process.")
  last_built = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the last build.")
  requirements = models.TextField(default="", blank=True, help_text="Python requirements.txt content.")
  env_vars = JSONField(default=dict, blank=True, help_text="Dictionary of environment variables to set in the Docker container.")
  auto_detect = models.BooleanField(default=True, help_text="Automatically detect environment settings from submissions.")
  
  # Convergence tracking
  convergence_stats = JSONField(default=dict, blank=True, help_text="Tracks module error counts: {module_name: occurrence_count}")
  successful_runs = models.PositiveIntegerField(default=0, help_text="Count of successful submission runs (for stability check)")
  total_runs = models.PositiveIntegerField(default=0, help_text="Total submission runs")
  
  # Image versioning and rollback
  current_build_version = models.PositiveIntegerField(default=1, help_text="Current image version number")
  image_history = JSONField(default=list, blank=True, help_text="List of {version, image_name, requirements, built_at, status}")
  convergence_pending = models.BooleanField(default=False, help_text="True if waiting to validate convergence after auto-update")
  convergence_failed_notified = models.BooleanField(default=False, help_text="True if admin was notified of convergence failure")

  course = property(lambda self: self.assignment.course)


class AutograderExecutionEvent(BaseModel):
  """One row per autograder cache consultation or execution, powering the
  superadmin autograding stats dashboard. Insert-only; never updated."""
  if TYPE_CHECKING:
    id: int

  ERROR_CATEGORY_CHOICES = [
      ('timeout', 'Timeout'),
      ('missing_dependency', 'Missing Dependency'),
      ('compile_error', 'Compile Error'),
      ('runtime_error', 'Runtime Error'),
      ('marker_extraction', 'Marker Extraction'),
      ('infra', 'Infrastructure'),
      ('unknown', 'Unknown'),
  ]

  TRIGGER_CHOICES = [
      ('file_run', 'File Run'),
      ('submission_run', 'Submission Run'),
      ('test_run', 'Test Run'),
  ]

  # SET_NULL, not CASCADE: expired courses are hard-deleted hourly and platform
  # stats must survive them.
  course = models.ForeignKey(
      Course, on_delete=models.SET_NULL,
      null=True, blank=True,
      related_name='autograder_execution_events',
      help_text="The course this execution belonged to",
  )
  assignment = models.ForeignKey(
      'Assignment', on_delete=models.SET_NULL,
      null=True, blank=True,
      related_name='autograder_execution_events',
      help_text="The assignment this execution belonged to",
  )
  cached = models.BooleanField(help_text="True if served from cache, False if actually executed")
  success = models.BooleanField(help_text="Whether the execution (or cached result) succeeded")
  # Snapshot of Environment.language at record time — no choices on purpose:
  # the environment's choice list evolves and '' is a real auto-detect state.
  language = models.CharField(max_length=25, blank=True, default='',
                              help_text="Environment language at execution time")
  trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES,
                             help_text="Which execution path produced this event")
  error_category = models.CharField(max_length=32, choices=ERROR_CATEGORY_CHOICES,
                                    blank=True, default='',
                                    help_text="Classified error category for failed executions")
  error_message = models.CharField(max_length=500, blank=True, default='',
                                   help_text="Truncated sample of the error output")

  class Meta:
    indexes = [
        models.Index(fields=['created']),
        models.Index(fields=['course', 'created']),
    ]

  def __str__(self):
    return (f"AutograderExecutionEvent [{self.trigger}] cached={self.cached} "
            f"success={self.success} course={self.course_id}")


#### Data

# AssignmentDataSet

@receiver(models.signals.pre_delete, sender=Assignment)
def delete_assignment(sender, instance: Assignment, **kwargs):
  # Delete any data releate to the assignment within the system.
  # The database will handle deleting on cascade, but the docker images and datasets will need to be deleted manually.

  # Attached quizzes are SET_NULL on assignment delete, which would turn a gated quiz into an
  # open standalone one. Unpublish them first so a deleted assignment never silently opens a quiz.
  instance.quizzes.filter(isPublished=True).update(isPublished=False)

  if instance.dataSets.all() and instance.dataSets.all().count() > 0:
    # just delete the directory assignment/dataset
    try:
      data_path = os.path.join(MEDIA_ROOT, dataset_upload_path(instance, filename=""))
      logger.info(f"Deleting dataset directory: {data_path}")
      shutil.rmtree(data_path)
    except Exception as e:
      logger.error(f"Failed to delete dataset directory: {e}")

###############################################################################


def getCurrentFiles(submission: Submission):
  files = submission.files.all()
  currentFileByPath = {}
  for f in files:
    path = (re.sub(r'/^\/+|\/+$', '', f.path) + '/' + f.name) if f.path else f.name
    if (path not in currentFileByPath) or (f.created >= currentFileByPath[path].created):
      currentFileByPath[path] = f

  currentFiles = []
  for path in currentFileByPath:
    currentFiles.append(currentFileByPath[path])
  return currentFiles


def getLatestSubmissionTests(submission):
  submission_tests = submission.tests.all()
  current_subtest_by_case = {}
  for st in submission_tests:
    if st.testCase.id not in current_subtest_by_case:
      current_subtest_by_case[st.testCase.id] = st
    else:
      if st.created > current_subtest_by_case[st.testCase.id].created:
        current_subtest_by_case[st.testCase.id] = st

  return current_subtest_by_case.values()


def calculate_grade(submission: Submission) -> Decimal:
  if submission.pk is None:
    if submission.assignment.additiveGrading:
      return Decimal(0)
    else:
      return Decimal(submission.assignment.points)

  # key = category id (0 = no rubricComment), value = aggregate deductions
  deductions = {}
  deductions[0] = 0
  currentFiles = getCurrentFiles(submission)
  for file in currentFiles:
    for comment in file.comments.all():
      if comment.rubricComment:
        if comment.rubricComment.category.id in deductions.keys():
          deductions[
              comment.rubricComment.category.id] += comment.rubricComment.pointDelta
        else:
          deductions[
              comment.rubricComment.category.id] = comment.rubricComment.pointDelta
      elif comment.pointDelta:
        deductions[0] += comment.pointDelta

  # Apply category caps
  for key in deductions:
    if key != 0:
      category = RubricCategory.objects.get(id=key)
      if category.pointLimit is not None:
        if category.pointLimit < 0:
          deductions[key] = max(deductions[key], category.pointLimit)
        else:
          deductions[key] = min(deductions[key], category.pointLimit)

  # Now account for points corresponding to tests
  counter = Decimal(0)
  if submission.assignment.testsAffectGrade:
    tests = getLatestSubmissionTests(submission)
    for test in tests:
      # Use stored earned score directly when maxScore is present.
      # This is robust for partial-credit script tests even if pointsPass
      # metadata is stale or zero due to parsing mismatches.
      if test.maxScore and test.maxScore > 0:
        earned = Decimal(test.score)
        max_possible = Decimal(test.maxScore)
        if earned < 0:
          earned = Decimal(0)
        if earned > max_possible:
          earned = max_possible
        counter += earned
      else:
        # Fallback to binary pass/fail for legacy tests
        if test.passed:
          counter += Decimal(test.testCase.pointsPass)
        else:
          counter += Decimal(test.testCase.pointsFail)

  # Reduce to deduction
  if submission.assignment.additiveGrading:
    return Decimal(-1 * sum(deductions.values()) + counter)
  else:
    return Decimal(submission.assignment.points) - Decimal(sum(deductions.values())) + counter


def updateSubmissionHistory(submission: Submission):
  oldStudents = [
      x.student for x in submission.histories.all().prefetch_related('student')]
  newStudents = submission.students.all()
  for student in newStudents:
    if (student not in oldStudents):
      SubmissionHistory.objects.create(
          submission=submission, student=student, hasViewed=False)
  for student in oldStudents:
    if (student not in newStudents):
      SubmissionHistory.objects.filter(
          student=student, submission=submission).delete()
  return


############# Signals #########################################################

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
  if created:
    Profile.objects.get_or_create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
  # Ensure profile exists before trying to save
  try:
    instance.profile.save()
  except Profile.DoesNotExist:
    Profile.objects.create(user=instance)


class TestCategoryResource(BaseModel):
  __test__ = False

  if TYPE_CHECKING:
    id: int
    category: models.ForeignKey[TestCategory, TestCategory]
    file: Optional[models.ForeignKey[AssignmentFile, AssignmentFile]]
    dataset: Optional[models.ForeignKey[AssignmentDataSet, AssignmentDataSet]]

  category = models.ForeignKey("TestCategory", on_delete=models.CASCADE, related_name="resources", help_text="The related test category.")
  
  # A resource can be either a File or a DataSet
  file = models.ForeignKey("AssignmentFile", on_delete=models.CASCADE, null=True, blank=True, help_text="The source file.")
  dataset = models.ForeignKey("AssignmentDataSet", on_delete=models.CASCADE, null=True, blank=True, help_text="The source dataset.")
  
  # The path/name this resource will have in the test environment (Aliasing)
  target_path = models.CharField(max_length=512, help_text="The filename or path this resource will be saved as during test execution. Allows aliasing (e.g. use 'test1.txt' as 'input.txt').")

  def save(self, *args, **kwargs):
    if not self.file and not self.dataset:
      raise ValidationError("A TestCategoryResource must have either a file or a dataset.")
    if self.file and self.dataset:
      raise ValidationError("A TestCategoryResource cannot have both a file and a dataset.")

    # Automatically tag the linked resource as a test resource
    if self.file:
      self.file.is_test_resource = True 
      self.file.save()
    if self.dataset:
      self.dataset.is_test_resource = True
      self.dataset.save()

    super(TestCategoryResource, self).save(*args, **kwargs)

  class Meta:
    unique_together = [
        ('category', 'target_path') # Ensure no two resources try to write to the same path
    ]

############# TRIGGER GRADE COMPUTATION #######################################

# Don't need to handle delete cases, since deleting a rubric category => deletes
# rubric comments => modifies comment objects (by setting
# comment.rubricComment to null)


@receiver(post_save, sender=RubricCategory)
def update_submission_grade_after_rubricCategory_save(sender, instance, **kwargs):
  for rubricComment in instance.rubricComments.all():
    rubricComment.save()


@receiver(post_save, sender=RubricComment)
def update_submission_grade_after_rubricComment_save(sender, instance, **kwargs):
  for comment in instance.comments.all():
    comment.file.submission.save()

# We only want to update submission history if the students change


@receiver(m2m_changed, sender=Submission.students.through)
def update_histories_on_student_change(sender, instance, **kwargs):
  updateSubmissionHistory(instance)


@receiver(post_save, sender=Submission)
def update_assignment(sender, instance, **kwargs):
  instance.assignment.save()


@receiver(post_save, sender=File)
def save_submission_from_file(sender, instance, **kwargs):
  try:
    instance.submission.save()
  except AttributeError:
    # File doesn't have a submission (might be an AssignmentFile or CourseFile)
    pass


@receiver(post_delete, sender=File)
def save_submission_from_file_delete(sender, instance, **kwargs):
  try:
    # Check if this is a SubmissionFile (has a submission attribute)
    if hasattr(instance, 'submission') and instance.submission_id and instance.submission:
      instance.submission.save()
  except (Submission.DoesNotExist, Assignment.DoesNotExist, AttributeError):
    # Submission or assignment was already deleted during cascade
    pass


@receiver(post_save, sender=Comment)
def save_submission_from_comment(sender, instance, **kwargs):
  instance.file.submission.save()


@receiver(post_delete, sender=Comment)
def save_submission_from_comment_delete(sender, instance, **kwargs):
  try:
    if instance.file_id and instance.file.submission_id:
      instance.file.submission.save()
  except (Submission.DoesNotExist, SubmissionFile.DoesNotExist, File.DoesNotExist, AttributeError):
    pass


@receiver(post_save, sender=SubmissionTest)
def save_submission_from_test(sender, instance, **kwargs):
  """Trigger grade recalculation when test results change."""
  instance.submission.save()


@receiver(post_delete, sender=SubmissionTest)
def save_submission_from_test_delete(sender, instance, **kwargs):
  """Trigger grade recalculation when test results are deleted."""
  try:
    instance.submission.save()
  except Submission.DoesNotExist:
    pass


@receiver(post_save, sender=TestCategory)
def update_test_cases_from_script(sender, instance, **kwargs):
    """Parse test script and update TestCases on save."""
    import logging
    _logger = logging.getLogger(__name__)
    try:
        from autograder.services.TestParsingService import TestParsingService
        TestParsingService.update_test_cases(instance)
    except Exception as e:
        _logger.exception(f"Failed to update test cases for TestCategory {instance.pk}: {e}")


@receiver(pre_save, sender=Submission)
def updateRegradeResponse(sender, instance, **kwargs):
  if(not instance.questionResponse):
    return
  try:
    obj = Submission.objects.get(pk=instance.pk)
  except sender.DoesNotExist:
    pass
  else:
    if not obj.questionResponse == instance.questionResponse:
      instance.responseDate = now()


class CommentTemplate(BaseModel):
  if TYPE_CHECKING:
    id: int
    owner: models.ForeignKey[User, User]
    assignment: models.ForeignKey[Assignment, Assignment]
    rubricComment: Optional[models.ForeignKey[RubricComment, RubricComment]]
    sourceComment: Optional[models.ForeignKey[Comment, Comment]]

  text = models.TextField(help_text=("The text of the template."))
  owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="comment_templates", help_text=("The creator of the template."))
  assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="comment_templates", help_text=("The assignment this template belongs to."))
  isGlobal = models.BooleanField(default=False, help_text=("If True, this template is visible to all graders in the assignment."))
  cellId = models.CharField(max_length=36, null=True, blank=True, help_text=("Optional notebook cell ID. If set, template only shows for this cell."))
  filePath = models.CharField(max_length=500, null=True, blank=True, help_text=("Optional file path pattern. If set, template only shows for matching files."))
  
  pointDelta = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True, help_text=("Points delta for this template."))
  rubricComment = models.ForeignKey('RubricComment', null=True, blank=True, on_delete=models.SET_NULL, related_name="templates", help_text=("Optional linked rubric comment."))
  sourceComment = models.ForeignKey('Comment', null=True, blank=True, on_delete=models.SET_NULL, related_name="derived_templates", help_text=("The original comment this template was created from. Null if manually created or source was deleted."))

  def __str__(self):
    return self.text[:20] + "..."

# Export all model classes for wildcard imports
__all__ = [
    "BaseModel",
    "Organization",
    "Profile",
    "Course",
    "Section",
    "Assignment",
    "RubricCategory",
    "RubricComment",
    "Submission",
    "File",
    "SubmissionFile",
    "AssignmentFile",
    "CourseFile",
    "CommentTag",
    "Comment",
    "TestCategory",
    "TestCase",
    "SubmissionTest",
    "SubmissionHistory",
    "AssignmentDataSet",
    "CachedExecutionResult",
    "AutograderExecutionEvent",
    "CommentTemplate",
    "MaintenanceBanner",
]


class MaintenanceBanner(models.Model):
    """
    Singleton model — always use pk=1.
    Controls the maintenance banner shown to all frontend users.
    Manage via Django admin (/admin/).

    Schedule fields (both optional):
    - starts_at: banner only shows after this UTC datetime (None = show immediately)
    - ends_at:   banner auto-hides after this UTC datetime (None = no expiry)

    active_now() factors in both the manual toggle and the schedule window.
    """

    SEVERITY_INFO = "info"
    SEVERITY_WARNING = "warning"
    SEVERITY_CRITICAL = "critical"
    SEVERITY_CHOICES = [
        (SEVERITY_INFO, "Info"),
        (SEVERITY_WARNING, "Warning"),
        (SEVERITY_CRITICAL, "Critical"),
    ]

    active = models.BooleanField(default=False, help_text="Manually enable/disable the banner.")
    message = models.TextField(
        default="codePost is currently undergoing maintenance. Some features may be unavailable.",
        help_text="The message displayed in the banner.",
    )
    color = models.CharField(
        max_length=30,
        default="#0e704c",
        help_text="Background colour (any CSS value, e.g. #0e704c or red).",
    )
    severity = models.CharField(
        max_length=10,
        choices=SEVERITY_CHOICES,
        default=SEVERITY_INFO,
        help_text="Visual severity of the banner (affects the icon shown to users).",
    )
    starts_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="If set, the banner will not appear before this UTC time even when active=True.",
    )
    ends_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="If set, the banner auto-hides after this UTC time.",
    )

    class Meta:
        verbose_name = "Maintenance Banner"
        verbose_name_plural = "Maintenance Banner"

    def save(self, *args, **kwargs):
        # Force singleton: always pk=1
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "MaintenanceBanner":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def is_active_now(self) -> bool:
        """True only when the manual toggle is on AND within the optional schedule window."""
        if not self.active:
            return False
        from django.utils import timezone
        now = timezone.now()
        if self.starts_at is not None and now < self.starts_at:
            return False
        if self.ends_at is not None and now > self.ends_at:
            return False
        return True

    def __str__(self):
        status = "ACTIVE" if self.active else "inactive"
        return f"MaintenanceBanner [{status}]"


class AIUsageRecord(BaseModel):
  """Tracks individual AI API calls for usage reporting and cost estimation."""
  if TYPE_CHECKING:
    id: int

  REQUEST_TYPE_CHOICES = [
      ('comment_generation', 'Comment Generation'),
      ('suggested_comments', 'Suggested Comments'),
      ('submission_summary', 'Submission Summary'),
      ('assignment_description', 'Assignment Description'),
      ('quiz_generation', 'Quiz Question Suggestions'),
      ('code_review', 'Code Review'),
      ('feedback', 'Feedback'),
      ('provider_test', 'Provider Test'),
      ('other', 'Other'),
  ]

  organization = models.ForeignKey(
      Organization, on_delete=models.SET_NULL, null=True, blank=True,
      related_name='ai_usage_records',
      help_text="The organization associated with this usage record"
  )
  course = models.ForeignKey(
      'Course', on_delete=models.SET_NULL, null=True, blank=True,
      related_name='ai_usage_records',
      help_text="The course associated with this usage record"
  )
  assignment = models.ForeignKey(
      'Assignment', on_delete=models.SET_NULL, null=True, blank=True,
      related_name='ai_usage_records',
      help_text="The assignment associated with this usage record"
  )
  user = models.ForeignKey(
      User, on_delete=models.SET_NULL, null=True, blank=True,
      related_name='ai_usage_records',
      help_text="The user who triggered this AI request"
  )
  provider = models.CharField(
      max_length=32, help_text="AI provider used (e.g., openai, gemini)"
  )
  model = models.CharField(
      max_length=64, help_text="Model name used for this request"
  )
  request_type = models.CharField(
      max_length=32, choices=REQUEST_TYPE_CHOICES, default='other',
      help_text="Type of AI request"
  )
  input_tokens = models.PositiveIntegerField(
      default=0, help_text="Number of input/prompt tokens"
  )
  output_tokens = models.PositiveIntegerField(
      default=0, help_text="Number of output/completion tokens"
  )
  total_tokens = models.PositiveIntegerField(
      default=0, help_text="Total tokens (input + output)"
  )
  cached_tokens = models.PositiveIntegerField(
      default=0, help_text="Number of input tokens served from provider cache (reduced cost)"
  )
  estimated_cost = models.DecimalField(
      max_digits=10, decimal_places=6, default=Decimal('0'),
      help_text="Estimated cost in USD for this API call"
  )
  status = models.CharField(
      max_length=16, default='success',
      help_text="Status of the request: success, error"
  )
  error_message = models.TextField(
      blank=True, null=True,
      help_text="Error message if the request failed"
  )
  prompt_variant = models.ForeignKey(
      'SystemPromptVariant', null=True, blank=True, on_delete=models.SET_NULL,
      related_name='usage_records',
      help_text="The prompt variant used for this request (for A/B tracking).",
  )
  experiment = models.ForeignKey(
      'PromptExperiment', null=True, blank=True, on_delete=models.SET_NULL,
      related_name='usage_records',
      help_text="The A/B experiment this request was part of, if any.",
  )

  class Meta:
    ordering = ('-created',)
    indexes = [
        models.Index(fields=['organization', 'created']),
        models.Index(fields=['course', 'created']),
        models.Index(fields=['assignment', 'created']),
        models.Index(fields=['created']),
    ]

  def __str__(self):
    return f"AIUsage [{self.provider}/{self.model}] {self.total_tokens} tokens"


############# AI Grading Assistance ###############################################

class SuggestedComment(BaseModel):
  """An AI-generated comment suggestion for graders. Not visible to students.
  Graders can accept (converting to a real Comment) or reject suggestions."""
  if TYPE_CHECKING:
    id: int
    submission: Submission
    file: SubmissionFile
    rubricComment: RubricComment | None
    acceptedBy: User | None
    acceptedComment: Comment | None
    promptVariant: 'SystemPromptVariant | None'

  SUGGESTION_STATUS_CHOICES = [
      ('pending', 'Pending'),
      ('accepted', 'Accepted'),
      ('rejected', 'Rejected'),
  ]

  submission: Submission = models.ForeignKey(  # type: ignore[assignment]
      Submission, on_delete=models.CASCADE,
      related_name='suggested_comments',
      help_text="The submission this suggestion belongs to."
  )
  file: SubmissionFile = models.ForeignKey(  # type: ignore[assignment]
      SubmissionFile, on_delete=models.CASCADE,
      related_name='suggested_comments',
      help_text="The file this suggestion targets."
  )
  text = models.TextField(
      blank=True,
      help_text="The AI-generated comment text."
  )
  startLine = models.IntegerField(
      help_text="The line or position where the suggestion begins (same semantics as Comment.startLine)."
  )
  endLine = models.IntegerField(
      help_text="The line or position where the suggestion ends (same semantics as Comment.endLine)."
  )
  startChar = models.IntegerField(
      default=0,
      help_text="The starting character offset (same semantics as Comment.startChar)."
  )
  endChar = models.IntegerField(
      default=0,
      help_text="The ending character offset (same semantics as Comment.endChar)."
  )
  rubricComment: RubricComment | None = models.ForeignKey(  # type: ignore[assignment]
      RubricComment, null=True, blank=True, on_delete=models.SET_NULL,
      related_name='suggested_comments',
      help_text="Optional rubric comment the AI mapped this suggestion to."
  )
  pointDelta = models.DecimalField(
      max_digits=5, decimal_places=2, blank=True, null=True,
      help_text="AI-suggested point delta. Null if linked to a rubricComment."
  )
  status = models.CharField(
      max_length=10, choices=SUGGESTION_STATUS_CHOICES, default='pending',
      help_text="Current status of this suggestion."
  )
  acceptedBy: User | None = models.ForeignKey(  # type: ignore[assignment]
      User, null=True, blank=True, on_delete=models.SET_NULL,
      related_name='accepted_suggestions',
      help_text="The grader who accepted this suggestion."
  )
  acceptedComment: Comment | None = models.ForeignKey(  # type: ignore[assignment]
      Comment, null=True, blank=True, on_delete=models.SET_NULL,
      related_name='source_suggestion',
      help_text="The real Comment created when this suggestion was accepted."
  )
  generationMetadata = models.JSONField(
      blank=True, null=True,
      help_text="Metadata about the generation (model used, tokens, confidence, etc.)."
  )
  promptVariant: 'SystemPromptVariant | None' = models.ForeignKey(  # type: ignore[assignment]
      'SystemPromptVariant', null=True, blank=True, on_delete=models.SET_NULL,
      related_name='suggested_comments',
      help_text="The prompt variant used to generate this suggestion."
  )
  generationBatch = models.UUIDField(
      null=True, blank=True,
      help_text="UUID grouping all suggestions from a single generation call."
  )
  firstViewedAt = models.DateTimeField(
      null=True, blank=True,
      help_text="Timestamp when a grader first saw this suggestion (set on list fetch)."
  )

  course = property(lambda self: self.file.course)

  class Meta:
    ordering = ('file', 'startLine', 'startChar')
    indexes = [
        models.Index(fields=['submission', 'status']),
        models.Index(fields=['promptVariant', 'status']),
        models.Index(fields=['generationBatch']),
    ]

  def __str__(self):
    return f"SuggestedComment [{self.status}] file={self.file_id} L{self.startLine}-{self.endLine}"


class SubmissionSummary(BaseModel):
  """AI-generated summary of a submission to help graders understand the student's work."""
  if TYPE_CHECKING:
    id: int
    submission: Submission

  submission: Submission = models.OneToOneField(  # type: ignore[assignment]
      Submission, on_delete=models.CASCADE,
      related_name='summary',
      help_text="The submission this summary describes."
  )
  text = models.TextField(
      help_text="Markdown-formatted summary of the submission."
  )
  generationMetadata = models.JSONField(
      blank=True, null=True,
      help_text="Metadata about the generation (model used, tokens, etc.)."
  )
  regenerationCount = models.PositiveIntegerField(
      default=0,
      help_text="Number of times this summary has been regenerated."
  )

  course = property(lambda self: self.submission.assignment.course)

  class Meta:
    verbose_name_plural = 'submission summaries'

  def __str__(self):
    return f"SubmissionSummary for submission={self.submission_id}"


############# Course Audit Log ####################################################

class CourseAuditEvent(BaseModel):
  """Tracks student activities within a course for instructor data analysis."""
  if TYPE_CHECKING:
    id: int

  EVENT_TYPE_CHOICES = [
      ('submission_attempt', 'Submission Attempt'),
      ('submission_failed', 'Submission Failed'),
      ('file_view', 'File View'),
      ('feedback_view', 'Feedback View'),
      ('regrade_request', 'Regrade Request'),
      ('regrade_deleted', 'Regrade Deleted'),
      ('autograder_triggered', 'Autograder Triggered'),
      ('autograder_completed', 'Autograder Completed'),
      ('autograder_failed', 'Autograder Failed'),
      ('late_day_used', 'Late Day Used'),
      ('comment_feedback', 'Comment Feedback'),
      ('assignment_state_changed', 'Assignment State Changed'),
      ('assignment_feedback_changed', 'Assignment Feedback Changed'),
      ('quiz_created', 'Quiz Created'),
      ('quiz_updated', 'Quiz Updated'),
      ('quiz_published', 'Quiz Published'),
      ('quiz_unpublished', 'Quiz Unpublished'),
      ('quiz_deleted', 'Quiz Deleted'),
      ('quiz_access_code_changed', 'Quiz Access Code Changed'),
      ('quiz_attempt_started', 'Quiz Attempt Started'),
      ('quiz_attempt_started_late', 'Quiz Attempt Started Late'),
      ('quiz_attempt_submitted', 'Quiz Attempt Submitted'),
      ('quiz_attempt_autosubmitted', 'Quiz Attempt Auto-Submitted'),
      ('quiz_attempt_seb_blocked', 'Quiz Attempt SEB Blocked'),
      ('quiz_attempts_reset', 'Quiz Attempts Reset'),
      ('quiz_response_graded', 'Quiz Response Graded'),
      ('quiz_response_grade_reopened', 'Quiz Response Grade Reopened'),
      ('quiz_generated_set_approved', 'Generated Question Set Approved'),
      ('quiz_generated_set_unapproved', 'Generated Question Set Unapproved'),
      ('quiz_generated_set_regenerated', 'Generated Question Set Regenerated'),
      ('quiz_generated_sets_published', 'Generated Question Sets Published'),
      # MCP agent layer: one event per agent-initiated write (alongside the
      # domain event the viewset itself records), and one per denied write —
      # repeated denials are the signal that a key leaked or a model is probing.
      ('agent_write', 'Agent Write'),
      ('agent_write_denied', 'Agent Write Denied'),
      # Tier-3 human confirmations: who allowed/blocked a destructive agent
      # operation, whether via the in-chat dialog or the dashboard buttons.
      ('agent_action_approved', 'Agent Action Approved'),
      ('agent_action_denied', 'Agent Action Denied'),
  ]

  course = models.ForeignKey(
      Course, on_delete=models.CASCADE,
      related_name='audit_events',
      help_text="The course this event belongs to",
  )
  assignment = models.ForeignKey(
      'Assignment', on_delete=models.SET_NULL,
      null=True, blank=True,
      related_name='audit_events',
      help_text="The assignment associated with this event",
  )
  quiz = models.ForeignKey(
      'Quiz', on_delete=models.SET_NULL,
      null=True, blank=True,
      related_name='audit_events',
      help_text="The quiz associated with this event",
  )
  submission = models.ForeignKey(
      'Submission', on_delete=models.SET_NULL,
      null=True, blank=True,
      related_name='audit_events',
      help_text="The submission associated with this event",
  )
  user = models.ForeignKey(
      User, on_delete=models.SET_NULL,
      null=True, blank=True,
      related_name='audit_events',
      help_text="The user who performed the action",
  )
  event_type = models.CharField(
      max_length=32, choices=EVENT_TYPE_CHOICES,
      help_text="The type of event",
  )
  meta = models.JSONField(
      blank=True, null=True,
      help_text="Extra context for the event (error messages, counts, etc.)",
  )

  class Meta:
    ordering = ('-created',)
    indexes = [
        models.Index(fields=['course', '-created']),
        models.Index(fields=['course', 'event_type']),
        models.Index(fields=['course', 'user']),
    ]

  def __str__(self):
    return f"AuditEvent [{self.event_type}] course={self.course_id} user={self.user_id}"


# -----------------------------------------------------------------------
# Prompt A/B Testing & Feedback
# -----------------------------------------------------------------------

class SystemPromptVariant(BaseModel):
  """A versioned, platform-global AI prompt template.

  Prompts are stored in the DB so they can be updated live without
  redeployment.  At most one variant per ``prompt_type`` may be ``active``
  at any time (enforced via ``unique_together`` + partial-unique constraint
  in the migration).
  """
  if TYPE_CHECKING:
    id: int

  # Populated dynamically from the prompt registry so new prompt types
  # only need a single file in core/prompts/.
  PROMPT_TYPE_CHOICES = prompt_registry.choices()

  STATUS_CHOICES = [
      ('draft', 'Draft'),
      ('active', 'Active'),
      ('candidate', 'Candidate'),
      ('retired', 'Retired'),
  ]

  prompt_type = models.CharField(
      max_length=32, choices=PROMPT_TYPE_CHOICES,
      help_text="Which AI feature this prompt is used for.",
  )
  name = models.CharField(
      max_length=128,
      help_text="Human-readable label (e.g. 'Concise Feedback v2').",
  )
  text = models.TextField(
      help_text="The prompt template. May contain {placeholder} variables.",
  )
  status = models.CharField(
      max_length=16, choices=STATUS_CHOICES, default='draft',
      help_text="Lifecycle status. Only one variant per prompt_type may be 'active'.",
  )
  version = models.PositiveIntegerField(
      default=1,
      help_text="Version number within this prompt's lineage.",
  )
  parent = models.ForeignKey(
      'self', null=True, blank=True, on_delete=models.SET_NULL,
      related_name='children',
      help_text="The variant this was derived from (for lineage tracking).",
  )
  created_by = models.ForeignKey(
      User, null=True, blank=True, on_delete=models.SET_NULL,
      related_name='created_prompt_variants',
      help_text="The staff user who created this variant.",
  )
  metadata = models.JSONField(
      default=dict, blank=True,
      help_text="Arbitrary metadata (auto-generation context, improvement notes, etc.).",
  )

  class Meta:
    ordering = ('-created',)
    indexes = [
        models.Index(fields=['prompt_type', 'status']),
    ]
    constraints = [
        models.UniqueConstraint(
            fields=['prompt_type'],
            condition=models.Q(status='active'),
            name='unique_active_prompt_per_type',
        ),
    ]

  def __str__(self):
    return f"PromptVariant [{self.prompt_type}] {self.name} v{self.version} ({self.status})"


class PromptExperiment(BaseModel):
  """An A/B test comparing two SystemPromptVariant instances.

  When ``status='running'``, a fraction (``sample_rate``) of AI requests
  for the given ``prompt_type`` will generate outputs from both variants
  and present them to the grader for preference feedback.
  """
  if TYPE_CHECKING:
    id: int

  STATUS_CHOICES = [
      ('running', 'Running'),
      ('paused', 'Paused'),
      ('completed', 'Completed'),
  ]

  name = models.CharField(
      max_length=128,
      help_text="Human-readable experiment name.",
  )
  prompt_type = models.CharField(
      max_length=32, choices=SystemPromptVariant.PROMPT_TYPE_CHOICES,
      help_text="Which AI feature is being tested.",
  )
  variant_a = models.ForeignKey(
      SystemPromptVariant, on_delete=models.CASCADE,
      related_name='experiments_as_a',
      help_text="The control variant (usually the current active prompt).",
  )
  variant_b = models.ForeignKey(
      SystemPromptVariant, on_delete=models.CASCADE,
      related_name='experiments_as_b',
      help_text="The challenger variant.",
  )
  status = models.CharField(
      max_length=16, choices=STATUS_CHOICES, default='paused',
      help_text="Current experiment lifecycle state.",
  )
  sample_rate = models.FloatField(
      default=0.1,
      help_text="Fraction of requests that trigger dual generation (0.0–1.0).",
  )
  started_by = models.ForeignKey(
      User, null=True, blank=True, on_delete=models.SET_NULL,
      related_name='started_experiments',
      help_text="Staff user who started this experiment.",
  )
  completed_at = models.DateTimeField(
      null=True, blank=True,
      help_text="When the experiment was completed.",
  )

  class Meta:
    ordering = ('-created',)
    indexes = [
        models.Index(fields=['prompt_type', 'status']),
    ]
    constraints = [
        models.UniqueConstraint(
            fields=['prompt_type'],
            condition=models.Q(status='running'),
            name='unique_running_experiment_per_type',
        ),
    ]

  def __str__(self):
    return f"Experiment [{self.prompt_type}] {self.name} ({self.status})"


class PromptFeedback(BaseModel):
  """User feedback on AI-generated output, optionally tied to an A/B experiment.

  Two feedback pools:
    - ``is_custom_context=False`` (default pool): assignment uses the
      platform default prompt.  Fed into auto-improvement.
    - ``is_custom_context=True`` (custom pool): assignment has a custom
      ``ai_system_prompt``.  Visible to staff for manual insight extraction.
  """
  if TYPE_CHECKING:
    id: int

  experiment = models.ForeignKey(
      PromptExperiment, null=True, blank=True, on_delete=models.SET_NULL,
      related_name='feedback',
      help_text="The experiment this feedback is part of (null for standalone rating).",
  )
  variant_used = models.ForeignKey(
      SystemPromptVariant, null=True, blank=True, on_delete=models.SET_NULL,
      related_name='feedback_received',
      help_text="The variant that produced the rated output (null when variant is unknown, e.g. pre-existing summaries).",
  )
  chosen_variant = models.ForeignKey(
      SystemPromptVariant, null=True, blank=True, on_delete=models.SET_NULL,
      related_name='feedback_chosen',
      help_text="For A/B: the variant the user preferred.",
  )
  user = models.ForeignKey(
      User, on_delete=models.SET_NULL, null=True, blank=True,
      related_name='prompt_feedback',
      help_text="The grader who provided this feedback.",
  )
  rating = models.SmallIntegerField(
      null=True, blank=True,
      help_text="Standalone rating: 1 = thumbs up, -1 = thumbs down.",
  )
  feedback_text = models.TextField(
      blank=True, default='',
      help_text="Optional free-text explanation.",
  )
  ai_output_a = models.TextField(
      blank=True, default='',
      help_text="Generated text from variant A (or the only variant for non-A/B).",
  )
  ai_output_b = models.TextField(
      blank=True, default='',
      help_text="Generated text from variant B (A/B test only).",
  )
  usage_record = models.ForeignKey(
      AIUsageRecord, null=True, blank=True, on_delete=models.SET_NULL,
      related_name='prompt_feedback',
      help_text="Link to the AIUsageRecord for cost/token tracking.",
  )
  prompt_type = models.CharField(
      max_length=32, choices=SystemPromptVariant.PROMPT_TYPE_CHOICES,
      help_text="Denormalized for efficient querying.",
  )
  is_custom_context = models.BooleanField(
      default=False,
      help_text="True when the assignment had a custom ai_system_prompt override.",
  )
  context_hash = models.CharField(
      max_length=64, blank=True, default='',
      help_text="Hash of the input context to detect equivalent re-tests.",
  )

  class Meta:
    ordering = ('-created',)
    indexes = [
        models.Index(fields=['prompt_type', 'is_custom_context']),
        models.Index(fields=['experiment', '-created']),
    ]

  def __str__(self):
    return f"PromptFeedback [{self.prompt_type}] user={self.user_id} rating={self.rating}"


class PromptLabSettings(models.Model):
  """Singleton config for Prompt Lab auto-improvement (always pk=1).

  Controls scheduled and threshold-based automatic prompt generation.
  Manage via the Prompt Lab admin UI or Django admin.
  """

  auto_improve_enabled = models.BooleanField(
      default=False,
      help_text="Master switch for all automatic prompt improvement.",
  )
  schedule_enabled = models.BooleanField(
      default=True,
      help_text="Enable periodic (Celery beat) auto-improvement runs.",
  )
  schedule_interval_hours = models.PositiveIntegerField(
      default=168,  # weekly
      help_text="How often (in hours) the scheduled task checks for improvements.",
  )
  threshold_enabled = models.BooleanField(
      default=True,
      help_text="Enable auto-improvement when new feedback count crosses the threshold.",
  )
  feedback_threshold = models.PositiveIntegerField(
      default=50,
      help_text="Number of new default-pool feedback entries (since last auto-improve) to trigger generation.",
  )
  min_feedback = models.PositiveIntegerField(
      default=5,
      help_text="Minimum total feedback required before any auto-improvement can run.",
  )

  # AI provider config for auto-improvement (platform-level, not per-course)
  AI_PROVIDER_CHOICES = [
      ('gemini', 'Google Gemini'),
      ('openai', 'OpenAI'),
  ]
  ai_provider = models.CharField(
      max_length=32, blank=True, default='',
      choices=AI_PROVIDER_CHOICES,
      help_text="AI provider used for auto-improvement and prompt generation.",
  )
  ai_api_key = EncryptedCharField(
      max_length=512, blank=True, default='',
      help_text="API key for the auto-improvement AI provider (stored encrypted).",
  )
  ai_model = models.CharField(
      max_length=64, blank=True, default='',
      help_text="Model to use for auto-improvement (e.g. gemini-2.5-pro, gpt-4o).",
  )

  class Meta:
    verbose_name = "Prompt Lab Settings"
    verbose_name_plural = "Prompt Lab Settings"

  def save(self, *args, **kwargs):
    self.pk = 1
    super().save(*args, **kwargs)

  @classmethod
  def load(cls) -> "PromptLabSettings":
    obj, _ = cls.objects.get_or_create(pk=1)
    return obj

  def __str__(self):
    status = "ENABLED" if self.auto_improve_enabled else "disabled"
    return f"PromptLabSettings [{status}]"


# How much a CourseAPIKey may do. Ordered weakest to strongest; an agent tool
# declares the minimum scope that may call it, and tools above a key's scope are
# never advertised to it at all.
COURSE_API_KEY_SCOPE_CHOICES = [
    ('read', 'Read only'),
    ('write', 'Read and write (no deletes, no emailing students)'),
    ('admin', 'Full course admin (deletes, resets, emailing students)'),
]


class CourseAPIKey(BaseModel):
  """A named, course-scoped API key.

  The raw key is shown only once on creation.  We store a SHA-256 hash
  for verification and the first 8 characters as a prefix for fast lookup.
  Key format: ``cpk_<course_id>_<uuid4_hex>``
  """
  if TYPE_CHECKING:
    id: int

  course = models.ForeignKey(Course, on_delete=models.CASCADE,
      related_name="api_keys", help_text="The course this key is scoped to.")
  name = models.CharField(max_length=128, help_text="A human-readable label for this key.")
  key_prefix = models.CharField(max_length=32, db_index=True, help_text=(
      "First characters of the raw key, used for fast lookup."))
  hashed_key = models.CharField(max_length=128, help_text="SHA-256 hash of the full raw key.")
  created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
      related_name="created_course_api_keys", help_text="The admin who created this key.")
  is_active = models.BooleanField(default=True, help_text="If False, the key is revoked.")
  last_used_at = models.DateTimeField(null=True, blank=True, help_text="Last time this key was used to authenticate.")
  scope = models.CharField(
      max_length=8, choices=COURSE_API_KEY_SCOPE_CHOICES, default='read',
      help_text=("How much this key may do. Agent tools are filtered by it: a key never "
                 "even sees the tools above its scope. 'read' is the safe default."))

  class Meta:
    unique_together = ('course', 'name')
    verbose_name = "Course API Key"
    verbose_name_plural = "Course API Keys"

  @staticmethod
  def generate_key(course_id: int) -> str:
    """Return a new raw key string: ``cpk_<course_id>_<hex>``."""
    return f"cpk_{course_id}_{uuid.uuid4().hex}"

  @staticmethod
  def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()

  def verify(self, raw_key: str) -> bool:
    return self.hashed_key == self.hash_key(raw_key)

  def __str__(self):
    return f"CourseAPIKey '{self.name}' for course {self.course_id}"



class PendingAgentAction(BaseModel):
  """A human confirmation gate for a Tier-3 agent operation (dashboard path).

  When an MCP agent asks for something unrecoverable (deleting an assignment,
  resetting quiz attempts, mass-emailing students) and the client cannot show
  an in-chat approval dialog (MCP elicitation), the server refuses and creates
  one of these instead. The instructor approves or denies it in Course
  Settings → Pending agent actions — a channel the agent cannot touch (the
  endpoints reject course-scoped credentials) — and the agent's retry then
  observes the decision.

  Single-use (``redeemed_at``), expires after ``EXPIRY_MINUTES``, and bound to
  the exact operation: ``args_hash`` pins the tool arguments and ``plan_hash``
  pins the computed blast radius, so if the world changes between preview and
  approval (say a TA grades six more submissions) the approval dies and the
  agent must re-preview. A denial (``denied_at``) sticks until the row
  expires, so the agent cannot nag by re-requesting.
  """
  if TYPE_CHECKING:
    id: int

  EXPIRY_MINUTES = 10

  course = models.ForeignKey(Course, on_delete=models.CASCADE,
      related_name="pending_agent_actions",
      help_text="The course the action would run in.")
  tool = models.CharField(max_length=64, help_text="The agent tool name.")
  args_hash = models.CharField(max_length=64,
      help_text="SHA-256 of the canonicalised tool arguments.")
  plan_hash = models.CharField(max_length=32,
      help_text="Fingerprint of the computed plan; a changed plan kills the approval.")
  plan = JSONField(default=dict, blank=True,
      help_text="Human-readable plan summary rendered in the dashboard panel.")
  # Legacy confirmation code, superseded by dashboard approval. Kept nullable
  # for one release so old processes survive a rolling deploy; drop after.
  code = models.CharField(max_length=12, null=True, blank=True,
      help_text="Unused legacy field (pre-approval-button confirmation code).")
  requested_by = models.ForeignKey(User, null=True, blank=True,
      on_delete=models.SET_NULL, related_name="pending_agent_actions",
      help_text="The principal the agent was acting as (service account or instructor).")
  expires_at = models.DateTimeField(help_text="Requests die quietly after this.")
  approved_at = models.DateTimeField(null=True, blank=True,
      help_text="Set when a course admin clicks Approve in the dashboard.")
  approved_by = models.ForeignKey(User, null=True, blank=True,
      on_delete=models.SET_NULL, related_name="approved_agent_actions",
      help_text="The course admin who approved it.")
  denied_at = models.DateTimeField(null=True, blank=True,
      help_text="Set when a course admin clicks Deny; blocks re-requests until expiry.")
  redeemed_at = models.DateTimeField(null=True, blank=True,
      help_text="Set once the approval is consumed; it never authorises again.")

  class Meta:
    ordering = ('-created',)
    indexes = [models.Index(fields=['course', 'expires_at'])]

  @property
  def is_active(self) -> bool:
    from django.utils import timezone
    return (self.redeemed_at is None and self.denied_at is None
            and self.expires_at > timezone.now())

  def __str__(self):
    return f"PendingAgentAction {self.tool} course={self.course_id}"


############# Quizzes #############################################################
# Phase 1 (authoring): instructors create/manage quiz questions, reusable question
# banks, and quizzes; import from Canvas (QTI); attach a quiz to an assignment; and
# get AI-suggested questions (mirrors SuggestedComment). No student-taking yet.

# Where a quiz record originated. Pure provenance — staff-internal, never restricts
# editing and never surfaced to students.
QUIZ_SOURCE_CHOICES = [
    ('manual', 'Manually authored'),
    ('imported', 'Imported'),
    ('ai', 'Accepted from an AI suggestion'),
]

QUESTION_TYPE_CHOICES = [
    ('multiple_choice', 'Multiple Choice (one correct)'),
    ('multiple_answers', 'Multiple Answers (several correct)'),
    ('true_false', 'True / False'),
    ('short_answer', 'Short Answer'),
    ('essay', 'Essay'),
    ('numerical', 'Numerical'),
    ('code', 'Code'),
]


class QuestionBank(BaseModel):
  """A course-level pool of quiz questions. Each question belongs to exactly one bank
  (see Question.bank); reusing a question elsewhere copies it."""
  if TYPE_CHECKING:
    id: int
    course: Course
    questions: RelatedManager[Question]

  course: Course = models.ForeignKey(Course, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="questionBanks", help_text=("The related course_id."))
  name = models.CharField(max_length=128, help_text=("The name of the question bank."))
  description = models.TextField(blank=True, help_text=("Optional description of the bank."))
  assignments = models.ManyToManyField('Assignment', related_name="questionBanks", blank=True,
      help_text=("Assignments this bank serves. Auto-added when the bank is used in a quiz attached "
                 "to an assignment; editable by instructors. Used as AI-generation context."))
  source = models.CharField(max_length=16, choices=QUIZ_SOURCE_CHOICES, default='manual',
      help_text=("Where this bank originated (staff-internal provenance)."))
  createdBy = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
      related_name="created_question_banks", help_text=("The staff user who created this bank."))

  class Meta:
    unique_together = ('course', 'name')
    ordering = ('name',)

  def __str__(self):
    return f"QuestionBank '{self.name}' (course {self.course_id})"


class Question(BaseModel):
  """A reusable quiz question that lives in exactly one bank (and may be used in
  multiple quizzes via QuizQuestion). 'Reusing' a question in another bank is a copy."""
  if TYPE_CHECKING:
    id: int
    course: Course
    bank: QuestionBank
    choices: RelatedManager[QuestionChoice]
    quizMemberships: RelatedManager[QuizQuestion]

  course: Course = models.ForeignKey(Course, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="questions", help_text=("The related course_id (mirrors bank.course)."))
  bank: QuestionBank = models.ForeignKey(QuestionBank, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="questions", help_text=("The bank this question belongs to (exactly one)."))
  questionType = models.CharField(max_length=20, choices=QUESTION_TYPE_CHOICES,
      default='multiple_choice', help_text=("The type of question."))
  text = models.TextField(help_text=("The question stem/prompt — single font (may contain HTML from Canvas)."))
  description = models.TextField(blank=True, help_text=(
      "Optional Markdown description shown beneath the stem (rich content: code blocks, lists, formatting)."))
  points = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('1'),
      help_text=("Point value of the question."))
  generalFeedback = models.TextField(blank=True, help_text=("Feedback shown regardless of answer."))
  partialCredit = models.BooleanField(default=False, help_text=(
      "For multiple_answers questions: score right-minus-wrong partial credit "
      "((correct − incorrect selections) / total correct × points, floored at 0) "
      "instead of all-or-nothing."))
  numericTolerance = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True,
      help_text=("For numerical questions: accept answers within ± this of an accepted value. "
                 "Null/0 requires an exact match."))
  # Code-question fields (used only when questionType == 'code').
  language = models.CharField(max_length=25, blank=True, null=True,
      help_text=("For code questions: the language (matches Environment.language values). "
                 "When null, resolve from the attached assignment's environment."))
  starterCode = models.TextField(blank=True, null=True,
      help_text=("For code questions: optional starter code shown to students."))
  referenceSolution = models.TextField(blank=True, null=True,
      help_text=("For code questions: optional reference solution (authoring-only, not auto-graded)."))
  source = models.CharField(max_length=16, choices=QUIZ_SOURCE_CHOICES, default='manual',
      help_text=("Where this question originated (staff-internal provenance)."))
  createdBy = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
      related_name="created_questions", help_text=("The staff user who authored/accepted this question."))
  metadata = JSONField(default=dict, blank=True,
      help_text=("Staff-internal metadata (Canvas IDs, AI provenance). Never shown to students."))

  class Meta:
    ordering = ('id',)

  def __str__(self):
    return f"Question [{self.questionType}] (course {self.course_id})"


class QuestionChoice(BaseModel):
  """An answer option for a question. For short_answer/numerical the accepted answers
  are stored as isCorrect=True choices; essay and code questions have no choices."""
  if TYPE_CHECKING:
    id: int
    question: Question

  question: Question = models.ForeignKey(Question, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="choices", help_text=("The related question_id."))
  text = models.TextField(help_text=("The choice text (or accepted answer value)."))
  isCorrect = models.BooleanField(default=False, help_text=("Whether this choice is a correct answer."))
  sortKey = models.IntegerField(default=0, help_text=("Order of this choice within the question."))
  feedback = models.TextField(blank=True, help_text=("Optional per-choice feedback."))

  course = property(lambda self: self.question.course)

  class Meta:
    ordering = ('sortKey', 'id')

  def __str__(self):
    return f"QuestionChoice (question {self.question_id})"


class Quiz(BaseModel):
  """An authoring container of questions. Optionally attached to one assignment."""
  if TYPE_CHECKING:
    id: int
    course: Course
    assignment: Assignment | None
    quizQuestions: RelatedManager[QuizQuestion]
    questionGroups: RelatedManager[QuizQuestionGroup]
    generatedSections: RelatedManager[QuizGeneratedSection]
    generatedSets: RelatedManager[GeneratedQuestionSet]

  ASSIGNMENT_TRIGGER_CHOICES = [
      ('during', 'During the assignment'),
      ('after_assignment', 'After the assignment closes'),
      ('after_submission', 'After the student submits'),
      ('after_feedback', 'After feedback is released (whole assignment)'),
      ('after_student_feedback', "After each student's feedback is ready (self-paced)"),
  ]
  PASSING_SCORE_UNIT_CHOICES = [
      ('percent', 'Percent'),
      ('points', 'Points'),
  ]
  SCORING_POLICY_CHOICES = [
      ('highest', 'Highest attempt counts'),
      ('latest', 'Latest attempt counts'),
      ('average', 'Average of attempts'),
  ]
  MULTI_ATTEMPT_SCORE_METHOD_CHOICES = [
      ('by_unit', 'By passing unit (percentage, or points)'),
      ('pooled', 'Pooled points across attempts'),
  ]
  CLOSE_EVENT_CHOICES = [
      ('none', 'No automatic close'),
      ('assignment_due', "At the assignment's deadline"),
      ('submission', 'After the student submits'),
      ('feedback_released', 'When feedback is released'),
      ('fixed_date', 'At a fixed date & time'),
  ]

  course: Course = models.ForeignKey(Course, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="quizzes", help_text=("The related course_id."))
  assignment: Assignment | None = models.ForeignKey(Assignment, null=True, blank=True,  # type: ignore[assignment]
      on_delete=models.SET_NULL, related_name="quizzes",
      help_text=("The assignment this quiz is attached to, if any."))
  title = models.CharField(max_length=128, help_text=("The quiz title."))
  description = models.TextField(blank=True, help_text=("Optional quiz description."))
  questions = models.ManyToManyField(Question, through='QuizQuestion', related_name="quizzes",
      help_text=("The questions in this quiz (ordered via QuizQuestion)."))
  source = models.CharField(max_length=16, choices=QUIZ_SOURCE_CHOICES, default='manual',
      help_text=("Where this quiz originated (staff-internal provenance)."))
  createdBy = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
      related_name="created_quizzes", help_text=("The staff user who created this quiz."))
  metadata = JSONField(default=dict, blank=True,
      help_text=("Staff-internal metadata (Canvas IDs, etc.)."))

  # --- Availability ---
  # Attached quizzes open relative to the assignment lifecycle (assignmentTrigger);
  # standalone quizzes use an explicit window (availableFrom/availableUntil).
  assignmentTrigger = models.CharField(max_length=32, choices=ASSIGNMENT_TRIGGER_CHOICES,
      default='during',
      help_text=("When an attached quiz becomes available, relative to the assignment lifecycle. "
                 "Ignored for standalone quizzes."))
  availableFrom = models.DateTimeField(null=True, blank=True,
      help_text=("Standalone quizzes: when the quiz opens."))
  availableUntil = models.DateTimeField(null=True, blank=True,
      help_text=("Standalone quizzes: when the quiz closes / is due. Also the close time when "
                 "an attached quiz's closeEvent is 'fixed_date'."))
  # Attached-quiz close: an event (optionally offset) after which the quiz stops being available.
  closeEvent = models.CharField(max_length=20, choices=CLOSE_EVENT_CHOICES, default='none',
      help_text=("When an attached quiz closes. Ignored for standalone quizzes."))
  closeOffsetMinutes = models.PositiveIntegerField(default=0,
      help_text=("Minutes added to the close event (e.g. 10080 = one week after). "
                 "Ignored for 'none' and 'fixed_date'."))
  endAttemptsAtClose = models.BooleanField(default=False,
      help_text=("If true, reaching the close time ends/auto-submits in-progress attempts "
                 "(hard deadline); if false it only blocks new attempts."))
  accessCode = models.CharField(max_length=16, null=True, blank=True,
      help_text=("Optional code that lets a late student start this quiz after it has closed. "
                 "Null/blank = no late access. Staff generate/rotate it via generateAccessCode; "
                 "a correct code bypasses only the close, nothing else."))

  # --- Exam security (Safe Exam Browser) ---
  requireSebBrowser = models.BooleanField(default=False,
      help_text=("If true, taking this quiz (starting, answering, submitting, and reading an "
                 "in-progress attempt) requires Safe Exam Browser: requests must carry a "
                 "valid X-SafeExamBrowser-ConfigKeyHash header matching sebConfigKey."))
  sebConfigKey = models.CharField(max_length=64, null=True, blank=True,
      help_text=("SEB Config Key (64 hex chars) pasted by the instructor from the SEB Config "
                 "Tool. Requests verify as SHA256(request URL + this key). Null/blank with "
                 "requireSebBrowser on blocks students until a key is set."))

  # --- Standard options (apply to every quiz) ---
  timeLimitMinutes = models.PositiveIntegerField(null=True, blank=True,
      help_text=("Time limit in minutes. Null = untimed."))
  attemptsAllowed = models.PositiveIntegerField(default=1,
      help_text=("Number of attempts allowed. 0 = unlimited."))
  shuffleQuestions = models.BooleanField(default=False,
      help_text=("Randomize question order per attempt."))
  oneQuestionAtATime = models.BooleanField(default=False,
      help_text=("Sequential mode: show one question at a time instead of all on one page."))
  allowBacktracking = models.BooleanField(default=True,
      help_text=("When sequential, whether students may return to previous questions."))
  showCorrectAnswers = models.BooleanField(default=True,
      help_text=("Whether the correct-answer key is shown when a student reviews a submitted "
                 "attempt. Reveal timing follows sealResultsUntilClose."))
  sealResultsUntilClose = models.BooleanField(default=False,
      help_text=("Hold scores, per-question points, and the answer key until the quiz closes "
                 "for the student. When false, results release as soon as an attempt is "
                 "submitted. Stops students with attempts remaining from mining the key."))
  showResponses = models.BooleanField(default=True,
      help_text=("Whether students may review their questions and answers after submitting. "
                 "When false, students only see scores (per the reveal policy) — the question "
                 "content is never shown again after submission."))
  allowSubmissionReview = models.BooleanField(default=True,
      help_text=("Whether students may reopen and review a submitted attempt afterward. When "
                 "false, submitting shows a confirmation only — the past attempt cannot be "
                 "reopened (scores may still surface on the quiz card per the reveal policy)."))
  passingScore = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True,
      help_text=("Optional pass threshold. Interpreted per passingScoreUnit "
                 "(a percentage 0–100, or an absolute point value)."))
  passingScoreUnit = models.CharField(max_length=8, choices=PASSING_SCORE_UNIT_CHOICES,
      default='percent', help_text=("Whether passingScore is a percentage or an absolute point value."))
  scoringPolicy = models.CharField(max_length=8, choices=SCORING_POLICY_CHOICES, default='highest',
      help_text=("Which attempt counts as the official score when multiple attempts are allowed."))
  multiAttemptScoreMethod = models.CharField(max_length=8, choices=MULTI_ATTEMPT_SCORE_METHOD_CHOICES,
      default='by_unit',
      help_text=("How multiple attempts combine into the official score: 'by_unit' compares/"
                 "averages by the passing unit (percentage, or points); 'pooled' totals points "
                 "earned over total points possible across attempts."))
  isPublished = models.BooleanField(default=False,
      help_text=("If false the quiz is a draft (author-only); students only see published quizzes."))

  # --- Per-student generated questions (see QuizGeneratedSection) ---
  gradersCanReviewGenerated = models.BooleanField(default=False,
      help_text=("If true, graders may review/edit/approve per-student generated question sets "
                 "on this quiz; if false, only course admins may."))
  gradersCanGenerate = models.BooleanField(default=False,
      help_text=("If true, graders may trigger the Generate-missing backfill on this quiz "
                 "(generation spends AI credits), letting them generate, review, and release "
                 "sets for their section; if false, only course admins may generate."))
  autoPublishGenerated = models.BooleanField(default=False,
      help_text=("If true, per-student generated question sets are approved automatically on "
                 "generation (no staff review gate)."))
  manualGeneration = models.BooleanField(default=True,
      help_text=("If true (the default), per-student question sets never generate "
                 "automatically (no per-submission trigger, no backfill on publish or "
                 "section creation) — staff generate via generateForStudent/generateMissing, "
                 "or the one-time generationDate run."))
  generationDate = models.DateTimeField(null=True, blank=True,
      help_text=("Manual mode only: one-time scheduled run that generates question sets for "
                 "students who have a submission but no set yet, at/after this time."))
  scheduledGenerationRanAt = models.DateTimeField(null=True, blank=True,
      help_text=("When the scheduled generationDate run last fired (one-shot stamp). "
                 "Moving generationDate past this re-arms the run."))

  class Meta:
    ordering = ('title',)

  def __str__(self):
    return f"Quiz '{self.title}' (course {self.course_id})"


class QuizQuestion(BaseModel):
  """Through model linking a Quiz to a Question, with ordering and optional point override.
  Questions are reusable across quizzes."""
  if TYPE_CHECKING:
    id: int
    quiz: Quiz
    question: Question

  quiz: Quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="quizQuestions", help_text=("The related quiz_id."))
  question: Question = models.ForeignKey(Question, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="quizMemberships", help_text=("The related question_id."))
  sortKey = models.IntegerField(default=0, help_text=("Order of this question within the quiz."))
  pointsOverride = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
      help_text=("Optional per-quiz point override. Null uses Question.points."))

  course = property(lambda self: self.quiz.course)

  class Meta:
    unique_together = ('quiz', 'question')
    ordering = ('sortKey', 'id')

  def __str__(self):
    return f"QuizQuestion quiz={self.quiz_id} question={self.question_id}"


class QuizQuestionGroup(BaseModel):
  """A 'random draw' on a quiz: at quiz time, pick ``pickCount`` random questions from
  ``bank``, each worth ``pointsPerQuestion`` (Canvas-style question group). Phase 1 is
  authoring-only — the per-attempt random selection happens in the student-taking phase."""
  if TYPE_CHECKING:
    id: int
    quiz: Quiz
    bank: QuestionBank

  quiz: Quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="questionGroups", help_text=("The related quiz_id."))
  bank: QuestionBank = models.ForeignKey(QuestionBank, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="quiz_groups", help_text=("The bank questions are drawn from."))
  name = models.CharField(max_length=128, blank=True,
      help_text=("Optional label for this group (e.g. 'Chapter 3 — pick 3')."))
  pickCount = models.PositiveIntegerField(default=1,
      help_text=("How many questions to randomly draw from the bank."))
  pointsPerQuestion = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('1'),
      help_text=("Points each drawn question is worth."))
  sortKey = models.IntegerField(default=0, help_text=("Order of this group within the quiz."))

  course = property(lambda self: self.quiz.course)

  class Meta:
    ordering = ('sortKey', 'id')

  def __str__(self):
    return f"QuizQuestionGroup quiz={self.quiz_id} bank={self.bank_id} pick={self.pickCount}"


class QuizAttempt(BaseModel):
  """One student's attempt at a Quiz. Auto-gradable responses are scored on submit;
  essay/code responses are flagged for manual grading by quiz graders / course admins."""
  if TYPE_CHECKING:
    id: int
    quiz: Quiz
    responses: RelatedManager[QuizResponse]

  STATUS_CHOICES = [
      ('in_progress', 'In progress'),
      ('submitted', 'Submitted'),
  ]

  quiz: Quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="attempts", help_text=("The quiz being attempted."))
  student = models.ForeignKey(User, on_delete=models.CASCADE,
      related_name="quiz_attempts", help_text=("The student taking the quiz."))
  attemptNumber = models.PositiveIntegerField(default=1,
      help_text=("1-based attempt index for this (quiz, student)."))
  status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='in_progress',
      help_text=("Attempt lifecycle state."))
  startedAt = models.DateTimeField(default=now,
      help_text=("When the student started this attempt."))
  deadline = models.DateTimeField(null=True, blank=True,
      help_text=("Hard stop = startedAt + quiz.timeLimitMinutes. Null when untimed."))
  submittedAt = models.DateTimeField(null=True, blank=True,
      help_text=("When the attempt was submitted."))
  score = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
      help_text=("Auto-graded points earned (set on submit)."))
  maxScore = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
      help_text=("Total points possible in this attempt (snapshot)."))
  needsManualGrading = models.BooleanField(default=False,
      help_text=("True if any response (essay/code) awaits manual grading."))
  passed = models.BooleanField(null=True, blank=True,
      help_text=("Whether the attempt met quiz.passingScore. Null until fully graded or if no threshold."))
  furthestIndex = models.PositiveIntegerField(default=0,
      help_text=("Highest response sortKey the student has reached; enforces sequential "
                 "navigation (oneQuestionAtATime / no-backtracking) server-side."))
  isOfficialOverride = models.BooleanField(default=False,
      help_text=("Staff-pinned: this attempt is the student's official score, overriding "
                 "the quiz's scoringPolicy. At most one per (quiz, student)."))
  closeBypassed = models.BooleanField(default=False,
      help_text=("Started with the quiz access code after the close; its deadline is not "
                 "capped at the close time."))
  lockdownVerified = models.BooleanField(default=False,
      help_text=("Started with a verified Safe Exam Browser signature. False on attempts "
                 "started before requireSebBrowser was enabled (or by exempt students)."))

  course = property(lambda self: self.quiz.course)

  class Meta:
    unique_together = ('quiz', 'student', 'attemptNumber')
    ordering = ('attemptNumber',)

  def __str__(self):
    return f"QuizAttempt quiz={self.quiz_id} student={self.student_id} #{self.attemptNumber}"


class QuizAccommodation(BaseModel):
  """A per-student extra-time accommodation for a course's timed quizzes.

  The multiplier scales every quiz's timeLimitMinutes when the student starts an attempt
  (e.g. 1.5 turns a 40-minute quiz into 60). It does NOT move a quiz's close time —
  ``endAttemptsAtClose`` still caps the attempt deadline at the close."""
  if TYPE_CHECKING:
    id: int
    course: Course
    student: User

  course: Course = models.ForeignKey(Course, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="quizAccommodations", help_text=("The course this accommodation applies to."))
  student: User = models.ForeignKey(User, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="quizAccommodations", help_text=("The accommodated student."))
  timeMultiplier = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal('1'),
      help_text=("Multiplier applied to every timed quiz's time limit for this student."))
  sebExempt = models.BooleanField(default=False,
      help_text=("Exempt this student from Safe Exam Browser requirements (e.g. they use a "
                 "platform SEB doesn't run on, like Linux or ChromeOS). Their attempts show "
                 "lockdownVerified=False to staff."))

  class Meta:
    unique_together = ('course', 'student')

  def __str__(self):
    return f"QuizAccommodation course={self.course_id} student={self.student_id} ×{self.timeMultiplier}"


class QuizSebLaunch(BaseModel):
  """One student's Safe Exam Browser launch of a quiz: the served .seb config (whose
  startURL embeds a one-time token for the auth handoff into SEB's fresh session) plus
  the Config Key computed from it. Per-launch because each config's bytes — and thus its
  key — are unique to the embedded token; verification accepts any of the student's
  unexpired launch keys alongside the quiz's manually pasted key."""
  if TYPE_CHECKING:
    id: int
    quiz: Quiz
    student: User
    token: OneTimeToken

  quiz: 'Quiz' = models.ForeignKey(Quiz, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name='sebLaunches', help_text=("The quiz this launch opens."))
  student: User = models.ForeignKey(User, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name='sebLaunches', help_text=("The launching student."))
  token: OneTimeToken = models.ForeignKey(OneTimeToken, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name='sebLaunches', help_text=("The one-time token embedded in the config's "
                                             "startURL, exchanged for a session inside SEB."))
  configKey = models.CharField(max_length=64,
      help_text=("SEB Config Key computed from configPlist; request hashes verify against it."))
  configPlist = models.TextField(
      help_text=("The exact .seb plist served for this launch — stored verbatim so the "
                 "served bytes always match the computed configKey."))
  expiresAt = models.DateTimeField(
      help_text=("When this launch's configKey stops verifying (generous — it must outlive "
                 "the whole quiz session, unlike the embedded auth token)."))

  class Meta:
    indexes = [models.Index(fields=['quiz', 'student'])]

  def __str__(self):
    return f"QuizSebLaunch quiz={self.quiz_id} student={self.student_id}"


class QuizResponse(BaseModel):
  """A student's answer to one question within a QuizAttempt.

  The presented question — stem, choices (with their correct flags), points — is fully
  snapshotted into ``questionSnapshot`` at attempt-build time, so editing or deleting the
  live Question afterward never alters or destroys an in-flight or graded attempt. Grading
  and student rendering read the snapshot, not the live question."""
  if TYPE_CHECKING:
    id: int
    attempt: QuizAttempt
    question: Question | None
    generatedQuestion: 'GeneratedQuizQuestion | None'

  attempt: QuizAttempt = models.ForeignKey(QuizAttempt, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="responses", help_text=("The attempt this response belongs to."))
  question: Question | None = models.ForeignKey(Question, on_delete=models.SET_NULL,  # type: ignore[assignment]
      null=True, blank=True, related_name="+",
      help_text=("The live question this was drawn from (analytics only; may be deleted). "
                 "Grading/rendering use questionSnapshot, so this is nullable and SET_NULL."))
  generatedQuestion: 'GeneratedQuizQuestion | None' = models.ForeignKey('GeneratedQuizQuestion',  # type: ignore[assignment]
      on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
      help_text=("The per-student generated question this was drawn from (analytics/answer-key "
                 "link only; may be deleted on regeneration). Grading/rendering use "
                 "questionSnapshot, so this is nullable and SET_NULL."))
  questionSnapshot = models.JSONField(default=dict,
      help_text=("Immutable copy of the presented question at attempt time: "
                 "{questionId, type, text, description, starterCode, language, generalFeedback, "
                 "choices:[{id, text, isCorrect, feedback, sortKey}]}."))
  sortKey = models.IntegerField(default=0,
      help_text=("Presentation order within the attempt (randomized when shuffleQuestions)."))
  points = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('1'),
      help_text=("Points this question is worth in this attempt (snapshot of override/base)."))
  selectedChoiceKeys = models.JSONField(default=list,
      help_text=("Selected option id(s) into questionSnapshot.choices, for choice-based questions."))
  answerText = models.TextField(blank=True,
      help_text=("Typed answer for short-answer/numerical/essay/code questions."))
  pointsEarned = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
      help_text=("Points awarded after grading. Null until graded."))
  isCorrect = models.BooleanField(null=True, blank=True,
      help_text=("Whether the auto-graded answer was correct. Null for manual/ungraded."))
  needsManualGrading = models.BooleanField(default=False,
      help_text=("True for essay/code responses awaiting manual grading."))
  graderFeedback = models.TextField(blank=True,
      help_text=("Optional feedback from the grader on a manually graded response."))
  gradedBy = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
      related_name="+", help_text=("The staff user who manually graded this response."))
  gradedAt = models.DateTimeField(null=True, blank=True,
      help_text=("When the manual grade was recorded. Null until manually graded; cleared "
                 "when the response is reopened."))
  codeExecution = models.JSONField(null=True, blank=True,
      help_text=("Staff-triggered sandbox run of a code answer: {status: running|success|"
                 "error, stdout, stderr, images, error, executionTime, requestedBy, "
                 "requestedAt, finishedAt}. Staff-internal — never shown to students."))

  course = property(lambda self: self.attempt.quiz.course)

  class Meta:
    unique_together = ('attempt', 'question')
    ordering = ('sortKey', 'id')

  def __str__(self):
    return f"QuizResponse attempt={self.attempt_id} question={self.question_id}"


class SuggestedQuizQuestion(BaseModel):
  """An AI-generated quiz question suggestion for instructors. Not a quiz question until
  accepted. Mirrors SuggestedComment's pending/accepted/rejected workflow."""
  if TYPE_CHECKING:
    id: int
    assignment: Assignment | None
    sourceQuestion: Question | None
    acceptedQuestion: Question | None
    acceptedBy: User | None
    promptVariant: 'SystemPromptVariant | None'

  SUGGESTION_STATUS_CHOICES = [
      ('pending', 'Pending'),
      ('accepted', 'Accepted'),
      ('rejected', 'Rejected'),
  ]

  assignment: Assignment | None = models.ForeignKey(Assignment, null=True, blank=True,  # type: ignore[assignment]
      on_delete=models.CASCADE, related_name="suggested_quiz_questions",
      help_text=("The assignment this suggestion was generated for (fresh generation)."))
  sourceQuestion: Question | None = models.ForeignKey(Question, null=True, blank=True,  # type: ignore[assignment]
      on_delete=models.SET_NULL, related_name="regeneration_suggestions",
      help_text=("The existing question this suggestion was generated from (cross-semester refresh)."))
  questionType = models.CharField(max_length=20, choices=QUESTION_TYPE_CHOICES,
      default='multiple_choice', help_text=("The suggested question type."))
  text = models.TextField(help_text=("The suggested question stem/prompt."))
  description = models.TextField(blank=True, help_text=(
      "Optional Markdown description shown beneath the stem (rich content: code blocks, lists, formatting)."))
  choicesData = JSONField(default=list, blank=True,
      help_text=("Proposed choices: list of {text, isCorrect, feedback}."))
  points = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('1'),
      help_text=("Suggested point value."))
  language = models.CharField(max_length=25, blank=True, null=True,
      help_text=("For code suggestions: the language."))
  starterCode = models.TextField(blank=True, null=True, help_text=("Suggested starter code."))
  referenceSolution = models.TextField(blank=True, null=True, help_text=("Suggested reference solution."))
  status = models.CharField(max_length=10, choices=SUGGESTION_STATUS_CHOICES, default='pending',
      help_text=("Current status of this suggestion."))
  acceptedBy: User | None = models.ForeignKey(User, null=True, blank=True,  # type: ignore[assignment]
      on_delete=models.SET_NULL, related_name="accepted_quiz_suggestions",
      help_text=("The instructor who accepted this suggestion."))
  acceptedQuestion: Question | None = models.ForeignKey(Question, null=True, blank=True,  # type: ignore[assignment]
      on_delete=models.SET_NULL, related_name="source_suggestion",
      help_text=("The real Question created or updated when this suggestion was accepted."))
  generationMetadata = JSONField(null=True, blank=True,
      help_text=("Metadata about the generation (model, tokens, etc.). Staff-internal."))
  promptVariant: 'SystemPromptVariant | None' = models.ForeignKey('SystemPromptVariant',  # type: ignore[assignment]
      null=True, blank=True, on_delete=models.SET_NULL, related_name="suggested_quiz_questions",
      help_text=("The prompt variant used to generate this suggestion."))
  generationBatch = models.UUIDField(null=True, blank=True,
      help_text=("UUID grouping all suggestions from a single generation call."))

  @property
  def course(self):
    if self.assignment_id is not None:
      return self.assignment.course
    if self.sourceQuestion_id is not None:
      return self.sourceQuestion.course
    return None

  class Meta:
    ordering = ('-created',)
    indexes = [
        models.Index(fields=['assignment', 'status']),
        models.Index(fields=['sourceQuestion', 'status']),
        models.Index(fields=['generationBatch']),
    ]

  def __str__(self):
    return f"SuggestedQuizQuestion [{self.status}] ({self.questionType})"


def quiz_import_upload_path(instance: QuizImportJob, filename: str) -> str:
  """Hierarchical upload path for Canvas QTI import files:
  quiz_imports/<org_shortname>/<course_name>/<filename>"""
  course = instance.course
  org = course.organization
  org_safe = slugify(org.shortname)
  course_safe = slugify(course.name)
  filename_safe = os.path.basename(filename)
  return f'quiz_imports/{org_safe}/{course_safe}/{filename_safe}'


class QuizImportJob(BaseModel):
  """Tracks an async QTI / Common Cartridge import (status + audit)."""
  if TYPE_CHECKING:
    id: int
    course: Course
    targetBank: QuestionBank | None

  JOB_STATUS_CHOICES = [
      ('pending', 'Pending'),
      ('running', 'Running'),
      ('completed', 'Completed'),
      ('failed', 'Failed'),
  ]

  course: Course = models.ForeignKey(Course, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="quiz_import_jobs", help_text=("The related course_id."))
  createdBy = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
      related_name="created_quiz_import_jobs", help_text=("The staff user who started the import."))
  file = models.FileField(upload_to=quiz_import_upload_path,
      help_text=("The uploaded QTI / Common Cartridge export."))
  status = models.CharField(max_length=16, choices=JOB_STATUS_CHOICES, default='pending',
      help_text=("Current status of the import job."))
  taskId = models.CharField(max_length=191, blank=True, help_text=("Celery task id for polling."))
  targetBank: QuestionBank | None = models.ForeignKey(QuestionBank, null=True, blank=True,  # type: ignore[assignment]
      on_delete=models.SET_NULL, related_name="import_jobs",
      help_text=("The bank imported questions land in. Created if absent."))
  createdQuizCount = models.PositiveIntegerField(default=0, help_text=("Number of quizzes created."))
  createdQuestionCount = models.PositiveIntegerField(default=0, help_text=("Number of questions created."))
  errorMessage = models.TextField(blank=True, help_text=("Error detail if the job failed."))
  summary = JSONField(default=dict, blank=True,
      help_text=("Per-item parse report, including skipped/unsupported types."))

  class Meta:
    ordering = ('-created',)

  def __str__(self):
    return f"QuizImportJob [{self.status}] (course {self.course_id})"


# Labels deliberately differ from QuizImportJob's JOB_STATUS_CHOICES so
# ENUM_NAME_OVERRIDES can match each choices list to its own enum name.
SUGGESTION_JOB_STATUS_CHOICES = [
    ('pending', 'Queued'),
    ('running', 'Generating'),
    ('completed', 'Completed'),
    ('failed', 'Failed'),
]


class QuizSuggestionJob(BaseModel):
  """Tracks one AI quiz-suggestion generation run (status + error reporting).

  The generation task runs async and previously had no way to report failures —
  clients polled the suggestion list and inferred 'disabled?' from emptiness.
  The generate endpoints now create one of these and return it; the task updates
  it on every exit path so clients can poll and surface the actual outcome."""
  if TYPE_CHECKING:
    id: int
    course: Course
    assignment: Assignment | None
    sourceQuestion: Question | None

  course: Course = models.ForeignKey(Course, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="quiz_suggestion_jobs", help_text=("The related course_id."))
  assignment: Assignment | None = models.ForeignKey(Assignment, null=True, blank=True,  # type: ignore[assignment]
      on_delete=models.CASCADE, related_name="suggestion_jobs",
      help_text=("The assignment suggestions were generated for (fresh generation)."))
  sourceQuestion: Question | None = models.ForeignKey(Question, null=True, blank=True,  # type: ignore[assignment]
      on_delete=models.CASCADE, related_name="suggestion_jobs",
      help_text=("The existing question a refresh suggestion was generated from."))
  quiz = models.ForeignKey('Quiz', null=True, blank=True, on_delete=models.CASCADE,
      related_name="suggestion_jobs",
      help_text=("The quiz a section-prompt test preview was run for (preview runs only)."))
  requestedBy = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
      related_name="quiz_suggestion_jobs", help_text=("The staff user who requested the generation."))
  status = models.CharField(max_length=16, choices=SUGGESTION_JOB_STATUS_CHOICES, default='pending',
      help_text=("Current status of the generation run."))
  taskId = models.CharField(max_length=191, blank=True, help_text=("Celery task id, for debugging."))
  createdCount = models.PositiveIntegerField(default=0,
      help_text=("Number of suggestions the run produced."))
  errorMessage = models.TextField(blank=True,
      help_text=("Friendly error detail if the run failed."))
  generationBatch = models.UUIDField(null=True, blank=True,
      help_text=("Batch UUID of the suggestions this run created."))
  resultData = JSONField(default=dict, blank=True,
      help_text=("Section-preview output: example questions, resolved prompt, seed info."))

  class Meta:
    ordering = ('-created',)

  def __str__(self):
    return f"QuizSuggestionJob [{self.status}] (course {self.course_id})"


# --- Auto-link assignments to question banks ---------------------------------
# When a bank is used in a quiz that's attached to an assignment, that assignment is
# added to the bank's `assignments` (instructors can remove it). This drives the
# AI-generation context for "Suggest questions" on the bank page.

@receiver(post_save, sender=QuizQuestion)
def _autolink_bank_from_quiz_question(sender, instance, **kwargs):
  aid = instance.quiz.assignment_id
  if aid:
    instance.question.bank.assignments.add(aid)


@receiver(post_save, sender=QuizQuestionGroup)
def _autolink_bank_from_quiz_group(sender, instance, **kwargs):
  aid = instance.quiz.assignment_id
  if aid:
    instance.bank.assignments.add(aid)


@receiver(post_save, sender=Quiz)
def _autolink_banks_on_quiz_attach(sender, instance, update_fields=None, **kwargs):
  # Only act when the quiz is attached to an assignment and that link may have just
  # changed (create => update_fields is None; attach => 'assignment' in update_fields).
  if not instance.assignment_id:
    return
  if update_fields is not None and 'assignment' not in update_fields:
    return
  aid = instance.assignment_id
  for qq in instance.quizQuestions.select_related('question__bank').all():
    qq.question.bank.assignments.add(aid)
  for g in instance.questionGroups.select_related('bank').all():
    g.bank.assignments.add(aid)


def quiz_image_upload_path(instance: 'QuizImage', filename: str) -> str:
  """Upload path for description images: quiz_images/<org>/<course>/<token><ext>."""
  course = instance.course
  org = course.organization
  ext = os.path.splitext(filename)[1].lower()
  return f'quiz_images/{slugify(org.shortname)}/{slugify(course.name)}/{instance.token.hex}{ext}'


class QuizImage(BaseModel):
  """An instructor-uploaded image referenced from a quiz/question/bank Markdown
  description. Served publicly at an unguessable token URL so it renders inline
  in Markdown (browsers can't send the Authorization header on <img> requests)."""
  if TYPE_CHECKING:
    id: int
    course: Course

  course: Course = models.ForeignKey(Course, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="quiz_images", help_text=("The related course_id."))
  token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True,
      help_text=("Unguessable public token used in the image URL."))
  image = models.ImageField(upload_to=quiz_image_upload_path, help_text=("The uploaded image file."))
  originalName = models.CharField(max_length=255, blank=True, help_text=("Original filename."))
  contentType = models.CharField(max_length=100, blank=True, help_text=("Image MIME type."))
  uploadedBy = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
      related_name="uploaded_quiz_images", help_text=("The staff user who uploaded it."))

  class Meta:
    ordering = ('-created',)

  def delete(self, *args, **kwargs):
    # Remove the file from storage when the record is deleted.
    if self.image:
      try:
        self.image.delete(save=False)
      except Exception:
        pass
    super().delete(*args, **kwargs)

  def __str__(self):
    return f"QuizImage {self.token} (course {self.course_id})"


# --- Per-student AI-generated questions ---------------------------------------
# A quiz may contain "generated sections" — a third component type alongside fixed
# questions (QuizQuestion) and random draws (QuizQuestionGroup). Each section holds an
# instructor-authored prompt template; on assignment submission, numQuestions questions
# are generated per student from their own submission. Staff review/edit/approve each
# student's GeneratedQuestionSet before their quiz opens. Like SuggestedQuizQuestion,
# generated questions are NOT bank Questions and carry staff-internal provenance only.

GENERATED_SET_STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('generating', 'Generating'),
    ('ready', 'Ready for review'),
    ('approved', 'Approved'),
    ('failed', 'Failed'),
]


class QuizGeneratedSection(BaseModel):
  """A per-student generation config on a quiz: generate ``numQuestions`` questions per
  student from the instructor's ``systemPrompt`` template (may contain {variables} — see
  core/prompts/variables.py), each worth ``pointsPerQuestion``. Requires the quiz to be
  attached to an assignment (the student's submission is the generation seed)."""
  if TYPE_CHECKING:
    id: int
    quiz: Quiz
    generatedQuestions: RelatedManager[GeneratedQuizQuestion]

  quiz: Quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="generatedSections", help_text=("The related quiz_id."))
  name = models.CharField(max_length=128, blank=True,
      help_text=("Optional label for this section (e.g. 'About your solution — 3 questions')."))
  systemPrompt = models.TextField(
      help_text=("Instructor-authored prompt template. May contain {variables} such as "
                 "{assignment_file:name} or {submission_files}, resolved per student at "
                 "generation time."))
  numQuestions = models.PositiveIntegerField(default=3,
      help_text=("How many questions to generate per student."))
  pointsPerQuestion = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('1'),
      help_text=("Points each generated question is worth."))
  questionTypes = JSONField(default=list, blank=True,
      help_text=("Optional subset of question types to generate (QUESTION_TYPE_CHOICES keys). "
                 "Empty = let the model choose."))
  sortKey = models.IntegerField(default=0, help_text=("Order of this section within the quiz."))

  course = property(lambda self: self.quiz.course)

  class Meta:
    ordering = ('sortKey', 'id')

  def __str__(self):
    return f"QuizGeneratedSection quiz={self.quiz_id} num={self.numQuestions}"


class GeneratedQuestionSet(BaseModel):
  """One student's generated questions for a quiz (spanning all of its generated
  sections). The approval gate: the quiz only opens for the student once their set is
  approved by staff (or auto-approved when Quiz.autoPublishGenerated)."""
  if TYPE_CHECKING:
    id: int
    quiz: Quiz
    student: User
    submission: Submission | None
    approvedBy: User | None
    promptVariant: 'SystemPromptVariant | None'
    questions: RelatedManager[GeneratedQuizQuestion]

  quiz: Quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="generatedSets", help_text=("The related quiz_id."))
  student: User = models.ForeignKey(User, on_delete=models.CASCADE,  # type: ignore[assignment]
      related_name="generated_question_sets", help_text=("The student this set was generated for."))
  submission: Submission | None = models.ForeignKey(Submission, null=True, blank=True,  # type: ignore[assignment]
      on_delete=models.SET_NULL, related_name="generated_question_sets",
      help_text=("The submission this set was generated from (seed). Null if the submission "
                 "was later deleted; regeneration then requires a new submission."))
  status = models.CharField(max_length=12, choices=GENERATED_SET_STATUS_CHOICES, default='pending',
      help_text=("Lifecycle: pending -> generating -> ready (awaiting review) -> approved; "
                 "or failed."))
  approvedBy: User | None = models.ForeignKey(User, null=True, blank=True,  # type: ignore[assignment]
      on_delete=models.SET_NULL, related_name="approved_generated_sets",
      help_text=("The staff user who approved this set. Null when auto-published."))
  approvedAt = models.DateTimeField(null=True, blank=True,
      help_text=("When this set was approved."))
  generationBatch = models.UUIDField(null=True, blank=True,
      help_text=("UUID of the generation run that produced/claimed this set. A stale task "
                 "whose batch no longer matches must not write results."))
  generationMetadata = JSONField(null=True, blank=True,
      help_text=("Metadata about the generation (model, tokens, etc.). Staff-internal."))
  promptVariant: 'SystemPromptVariant | None' = models.ForeignKey('SystemPromptVariant',  # type: ignore[assignment]
      null=True, blank=True, on_delete=models.SET_NULL, related_name="generated_question_sets",
      help_text=("The platform prompt variant used to generate this set."))
  errorMessage = models.TextField(blank=True, help_text=("Error detail if generation failed."))

  course = property(lambda self: self.quiz.course)

  class Meta:
    unique_together = ('quiz', 'student')
    ordering = ('-created',)
    indexes = [
        models.Index(fields=['quiz', 'status']),
    ]

  def __str__(self):
    return f"GeneratedQuestionSet [{self.status}] quiz={self.quiz_id} student={self.student_id}"


class GeneratedQuizQuestion(BaseModel):
  """One generated question in a student's set. Staff-editable until (and after) approval;
  NOT a bank Question. At attempt time it is snapshotted into QuizResponse.questionSnapshot
  (question FK = None), so later edits never disturb an in-progress attempt."""
  if TYPE_CHECKING:
    id: int
    set: GeneratedQuestionSet
    section: QuizGeneratedSection

  set: GeneratedQuestionSet = models.ForeignKey(GeneratedQuestionSet,  # type: ignore[assignment]
      on_delete=models.CASCADE, related_name="questions", help_text=("The related set_id."))
  section: QuizGeneratedSection = models.ForeignKey(QuizGeneratedSection,  # type: ignore[assignment]
      on_delete=models.CASCADE, related_name="generatedQuestions",
      help_text=("The section this question was generated for."))
  questionType = models.CharField(max_length=20, choices=QUESTION_TYPE_CHOICES,
      default='multiple_choice', help_text=("The type of question."))
  text = models.TextField(help_text=("The question stem/prompt."))
  description = models.TextField(blank=True, help_text=(
      "Optional Markdown description shown beneath the stem."))
  choicesData = JSONField(default=list, blank=True,
      help_text=("Choices: list of {text, isCorrect, feedback}."))
  points = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('1'),
      help_text=("Point value (seeded from the section's pointsPerQuestion, staff-editable)."))
  sortKey = models.IntegerField(default=0, help_text=("Order of this question within the set."))
  # Code-question fields (used only when questionType == 'code').
  language = models.CharField(max_length=25, blank=True, null=True,
      help_text=("For code questions: the language."))
  starterCode = models.TextField(blank=True, null=True,
      help_text=("For code questions: optional starter code shown to students."))
  referenceSolution = models.TextField(blank=True, null=True,
      help_text=("Grader-facing answer key generated alongside the question: the correct "
                 "answer/code and, for hand-computation questions, the worked steps. "
                 "NEVER shown to students and never snapshotted into questionSnapshot."))

  course = property(lambda self: self.set.quiz.course)

  class Meta:
    ordering = ('sortKey', 'id')

  def __str__(self):
    return f"GeneratedQuizQuestion [{self.questionType}] set={self.set_id}"
