# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.

from rest_framework import status
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from drf_spectacular.utils import extend_schema, OpenApiResponse

from django.utils.http import urlsafe_base64_decode
from django.contrib.auth.tokens import default_token_generator

from core.logging import logEvent
from core.models import User, Organization, Course
from core.utils import is_course_member, email_passes_whitelist
from core.throttles import AuthAnonRateThrottle
from core.forms.forms import (
    ChangePasswordForm,
    EmailForm,
    EmailTokenForm,
    ValidateTokenForm,
    CreateAdminForm,
    SetPasswordFromTokenForm,
    ValidationResponseForm,
    SetCredentialsForm,
)
from core.serializers.registration import (
    EmailRegistrationRequestSerializer,
    EmailRegistrationResponseSerializer,
    VerifyRegistrationTokenRequestSerializer,
    VerifyRegistrationTokenResponseSerializer,
    RegisterAndSetPasswordRequestSerializer,
    RegisterAndSetPasswordResponseSerializer,
    SetCredentialsRequestSerializer,
    SetCredentialsResponseSerializer,
    EmptyResponseSerializer,
    ValidateNewAdminUserRequestSerializer,
    ValidateNewAdminUserResponseSerializer,
    HandleValidationResponseSerializer,
    CheckStatusNewAdminUserResponseSerializer,
    EmailPasswordResetRequestSerializer,
    EmailPasswordResetResponseSerializer,
    VerifyResetTokenRequestSerializer,
    VerifyResetTokenResponseSerializer,
    ResetPasswordRequestSerializer,
    ResetPasswordResponseSerializer,
)

from core.emails import AdminAlreadyEmail, AdminChangeOrganizationEmail, ExistingOrgAdminRequestEmail, NewAdminActivationEmail, NewOrgAdminRequestEmail, PasswordResetEmail, PendingConfirmationEmail, UserAddedToCourseEmail
from core.tasks import send_user_added_to_course_email

from core.permissions.helpers import (
    returnNotAuthorized,
)
from core.permissions.helpers import isAuthenticated, can_elevate_permissions

from log.models import Event
import json

import logging
##########################################################################
#####################################     JOIN FLOW     ##################
#####################################                   ##################
##########################################################################


@extend_schema(
    request=EmailRegistrationRequestSerializer,
    responses={
        200: EmailRegistrationResponseSerializer,
        400: OpenApiResponse(description='Invalid form data'),
        403: OpenApiResponse(description='Email not on whitelist or invalid code'),
    }
)
@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthAnonRateThrottle])
def emailRegistration(request):
    """
    Request body includes: email.

    Function to take in email and send activation email in response, if user exists but is inactive
    (which indicates that they have been added to a course by a courseAdmin, but yet to create their
    account.)

    This is intended to allow users who missed their initial activation emails to re-send one
    to themselves.

    """
    form = EmailTokenForm(request.data)
    if form.is_valid():
        try:
            course = Course.objects.get(
                inviteCode=form.cleaned_data["token"], inviteCodeEnabled=True
            )
            try:
                user = User.objects.get(email=form.cleaned_data["email"])
                if email_passes_whitelist(user.email, course.emailWhitelist):
                    course.students.add(user)
                    return Response(
                        {"success": True, "code_valid": True, "email_valid": True},
                        status=status.HTTP_200_OK,
                    )
                else:
                    return Response(
                        {"success": False, "code_valid": True, "email_valid": False},
                        status=status.HTTP_403_FORBIDDEN,
                    )
            except User.DoesNotExist:
                if email_passes_whitelist(
                    form.cleaned_data["email"], course.emailWhitelist
                ):
                    newUser = User.objects.create(
                        username=form.cleaned_data["email"],
                        email=form.cleaned_data["email"],
                        is_active=False,
                    )
                    newUser.profile.organization = course.courseAdmins.all()[
                        0
                    ].profile.organization
                    newUser.profile.isPasswordSet = False
                    newUser.save()
                    course.students.add(newUser)
                    # Send activation email to new user
                    send_user_added_to_course_email.delay(
                        newUser.pk,
                        course_name=course.name,
                        course_period=course.period,
                        user_type="student",
                    )
                    return Response(
                        {"success": True, "code_valid": True, "email_valid": True},
                        status=status.HTTP_200_OK,
                    )
                else:
                    return Response(
                        {"success": False, "code_valid": True, "email_valid": False},
                        status=status.HTTP_403_FORBIDDEN,
                    )

        except Course.DoesNotExist:
            # Used to email the user, but now we just log the event
            logEvent(
                "Codepost Registration Error",
                level=logging.WARNING,
                message=json.dumps(
                    {
                        "email": form.cleaned_data["email"],
                        "inviteCode": form.cleaned_data["token"],
                    }
                ),
                event_type='audit',
            )

            return Response(
                {"success": False, "code_valid": False, "email_valid": False},
                status=status.HTTP_403_FORBIDDEN,
            )
    else:
        return Response(
            {"success": False, "errors": form.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )


@extend_schema(
    request=VerifyRegistrationTokenRequestSerializer,
    responses={200: VerifyRegistrationTokenResponseSerializer}
)
@api_view(["POST"])
@permission_classes([AllowAny])
def verifyRegistrationToken(request):
    """
    Handle valid verify email links sent after account creation.

    Function takes a (uid, token) as input and determines if the pair is valid.

    This is used to inform the client whether a user presenting (uid, token) should be shown a form
    to set their password.

    """
    form = ValidateTokenForm(request.data)
    if form.is_valid():
        uid_int = urlsafe_base64_decode(form.cleaned_data["uid"]).decode()
        try:
            user = User.objects.get(id=uid_int)
            isValid = default_token_generator.check_token(
                user, form.cleaned_data["token"]
            )
            return Response(
                {"isValid": isValid, "email": user.email}, status=status.HTTP_200_OK
            )
        except User.DoesNotExist:
            return Response({"isValid": False}, status=status.HTTP_200_OK)
    else:
        return Response(
            {"isValid": False, "errors": form.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )


@extend_schema(
    request=RegisterAndSetPasswordRequestSerializer,
    responses={200: RegisterAndSetPasswordResponseSerializer}
)
@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthAnonRateThrottle])
def registerAndSetPassword(request):
    """
    Function takes a (uid, token) as authorization and, if authorization is valid, sets the associated
    user's password to the password payload.

    This is used in response to account activation emails (both generated via the "join" signup flow
    and automatically generated by roster additions)

    """
    form = SetPasswordFromTokenForm(request.data)
    if form.is_valid():
        uid_int = urlsafe_base64_decode(form.cleaned_data["uid"]).decode()
        try:
            user = User.objects.get(id=uid_int)
            isValid = default_token_generator.check_token(
                user, form.cleaned_data["token"]
            )
            if isValid:
                user.is_active = True
                user.set_password(form.cleaned_data["password1"])
                user.profile.isPasswordSet = True
                user.save()
                return Response({"isValid": True}, status=status.HTTP_200_OK)
            else:
                return Response(
                    {"isValid": False, "errors": {"token": "invalid token"}},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except User.DoesNotExist:
            return Response(
                {"isValid": False, "errors": {"token": "invalid token"}},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        return Response(
            {"isValid": False, "errors": form.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )


## CIP specific ##
@extend_schema(
    request=SetCredentialsRequestSerializer,
    responses={200: SetCredentialsResponseSerializer}
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def setCredentials(request):
    """
    If a user is logged in by hasn't yet set a usable password, they can use this endpoint to do so, as well
    as specify their organization.

    """
    user = request.user
    if not isAuthenticated(user):
        return returnNotAuthorized()

    # only want this to work if a user doesn't already have a usable password
    if user.password and user.has_usable_password():
        return Response(
            "You don't have permission to perform this action. Password already set.",
            status.HTTP_403_FORBIDDEN,
        )

    form = SetCredentialsForm(request.data)
    if form.is_valid():
        org_name = form.cleaned_data["organization"]
        try:
            org = Organization.objects.get(shortname=org_name)
        except Organization.DoesNotExist:
            org = Organization.objects.create(name=org_name, shortname=org_name)

        user.is_active = True
        user.set_password(form.cleaned_data["password1"])
        user.profile.organization = org
        user.profile.isPasswordSet = True
        user.save()

        return Response({"isValid": True}, status=status.HTTP_200_OK)
    else:
        return Response(
            {"isValid": False, "errors": form.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )


## CIP specific ##
@extend_schema(request=None, responses={200: EmptyResponseSerializer})
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def graderToAdmin(request):
    """
    Allows a user who is only a grader to elevate their status to level of admin within their organization.
    The role elevation allows the admin to create new courses, but not join existing courses.
    """

    user = request.user
    if not isAuthenticated(user):
        return returnNotAuthorized()

    if can_elevate_permissions(user):
        user.profile.canModifyRosters = True
        user.profile.canCreateCourses = True
        user.save()
        return Response({}, status=status.HTTP_200_OK)
    else:
        return Response({}, status=status.HTTP_400_BAD_REQUEST)



##########################################################################
#####################################    CREATE FLOW    ##################
#####################################                   ##################
##########################################################################



@extend_schema(
    request=ValidateNewAdminUserRequestSerializer,
    responses={200: ValidateNewAdminUserResponseSerializer}
)
@api_view(["POST"])
@permission_classes([AllowAny])
def validateNewAdminUser(request):
    """
    Function is used to trigger manual account validation in response to a user requesting their account
    be granted course creation privileges.

    Currently Vurnable to abuse, any user can call this endpoint and create an admin account. 
    """
    action_id = []
    is_new_org = False

    form = CreateAdminForm(request.data)  # organization, email
    if form.is_valid():
        rawName = form.cleaned_data["organization"]
        shortnameFromForm = rawName.replace(" ", "").lower()[0 : min(12, len(rawName))]

        # Case 1: user exists
        try:
            user = User.objects.get(email=form.cleaned_data["email"])
            org = user.profile.organization
            action_id.append(1)
            _is_student_or_grader = (user.student_courses.count() > 0) or (
                user.grader_courses.count() > 0
            )
            # If they do, check to see if their saved organization matches the
            # organization they are trying to sign up to
            if not (
                user.profile.organization
                and user.profile.organization.shortname.lower() == shortnameFromForm
            ):
                action_id.append(1)

                # Send user an email asking them to confirm they want to change their

                AdminChangeOrganizationEmail(
                    user=user,
                    
                ).send_email(organization_name=rawName)  


                logEvent(
                    "Admin Change Organization Request",
                    level= logging.WARNING,
                    message=json.dumps(
                        {
                            "user": user.email,
                            "old_organization": user.profile.organization.shortname if user.profile.organization else "",
                            "new_organization": rawName,
                            "shortname": shortnameFromForm,
                            "email": user.email,
                        }
                    ),
                    event_type='audit',
                )
                

                return Response(
                    {"success": True, "action_id": ".".join(map(str, action_id))},
                    status=status.HTTP_200_OK,
                )
            else:
                action_id.append(2)

        # Case 2: user does not exist, so create them
        except User.DoesNotExist:
            action_id.append(2)
            # If they don't, create them
            user = User.objects.create(
                username=form.cleaned_data["email"], email=form.cleaned_data["email"]
            )
            user.is_active = False
            _is_student_or_grader = (user.student_courses.count() > 0) or (
                user.grader_courses.count() > 0
            )

            # Case 2a: the organization the user is trying to join already exists.
            try:
                org = Organization.objects.get(shortname=shortnameFromForm)
                user.profile.organization = org
                user.save()
                action_id.append(1)

            # Case 2b: the organization the user is trying to join does not exist, so
            # create it
            except Organization.DoesNotExist:
                org = Organization.objects.create(
                    name=rawName, shortname=shortnameFromForm
                )
                user.profile.organization = org
                user.save()
                action_id.append(2)
                is_new_org = True

        # From now on, we can assume user exists and organization matches specified org

        if user.is_active and user.profile.canModifyRosters:
            action_id.append(1)
            # If user already exists and has been validated, then email them

            AdminAlreadyEmail(user=user).send_email()

            logEvent(
                "Admin Already Exists",
                level=logging.WARNING,
                message=json.dumps(
                    {
                        "user": user.email,
                        "organization": org.name,
                        "shortname": shortnameFromForm,
                    }
                ),
                event_type='audit',
            )

            return Response(
                {"success": True, "action_id": ".".join(map(str, action_id)), "is_new_org": False, "pending": False},
                status=status.HTTP_200_OK,
            )
        else:
            action_id.append(2)

            # Set user as pending validation — do NOT auto-activate
            user.profile.pendingValidation = True
            user.profile.canModifyRosters = False
            user.save()

            if is_new_org:
                # NEW ORG: Email CodePost staff only
                NewOrgAdminRequestEmail(
                    user=user
                ).send_email(organization_name=org.name)

                logEvent(
                    "New Org Admin Request",
                    level=logging.INFO,
                    message=json.dumps(
                        {
                            "user": user.email,
                            "organization": org.name,
                            "is_new_org": True,
                        }
                    ),
                    event_type='audit',
                )
            else:
                # EXISTING ORG: Email Org Staff + CodePost staff
                ExistingOrgAdminRequestEmail(
                    user=user
                ).send_email(organization_name=org.name)

                logEvent(
                    "Existing Org Admin Request",
                    level=logging.INFO,
                    message=json.dumps(
                        {
                            "user": user.email,
                            "organization": org.name,
                            "is_new_org": False,
                        }
                    ),
                    event_type='audit',
                )

            # Send confirmation email to the requesting user
            PendingConfirmationEmail(
                user=user
            ).send_email(organization_name=org.name, is_new_org=is_new_org)

            return Response(
                {"success": True, "action_id": ".".join(map(str, action_id)), "is_new_org": is_new_org, "pending": True},
                status=status.HTTP_200_OK,
            )
    else:
        logEvent(
            "Admin New Request Error",
            level=logging.ERROR,
            message=json.dumps(
                {
                    "errors": form.errors,
                    "action_id": ".".join(map(str, action_id)),
                }
            ),
            event_type='audit',
        )
     
        return Response(
            {
                "success": False,
                "action_id": ".".join(map(str, action_id)),
                "errors": form.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )


@extend_schema(responses={200: HandleValidationResponseSerializer})
@api_view(["GET"])
@permission_classes([AllowAny])
def handleValidationResponse(request):
    """
    Function is used to respond to validation instructions from codePost admins (sent via URL).

    In response to a valid activation grant, set user.canModifyRosters = True and user.pendingValidation = False.

    In response to a valid activation deny, set user.pendingValidation = False and if user is not a member of
    any courses, delete that user.

    """
    form = ValidationResponseForm(request.query_params)
    if form.is_valid():
        uid_int = urlsafe_base64_decode(form.cleaned_data["uid"]).decode()
        try:
            user = User.objects.get(id=uid_int)
            isValid = default_token_generator.check_token(
                user, form.cleaned_data["token"]
            )
            if user.profile.pendingValidation:
                if isValid:
                    if form.cleaned_data["activate"]:
                        # granting privilege
                        org = user.profile.organization
                        approve_new_admin_user(user, org_name=org.name if org else "")

                    else:
                        # denying privilege
                        Event.objects.create(
                            category="registration",
                            user=str(user),
                            description="New admin denied",
                        )

                        logEvent(
                            "Admin New Request Denied",
                            level=logging.WARNING,
                            message=json.dumps(
                                {
                                    "user": user.email,
                                    "organization": user.profile.organization.name if user.profile.organization else "",
                                }
                            ),
                            event_type='audit',
                        )
                     

                        # to the privilege request. Since we are denying that request,
                        # delete them.
                        if not is_course_member(user):
                            user.delete()

                return Response({"isValid": isValid}, status=status.HTTP_200_OK)
            else:
                return Response(
                    {
                        "isValid": False,
                        "message": "This user is not pending validation.",
                    },
                    status=status.HTTP_200_OK,
                )
        except User.DoesNotExist:
            return Response({"isValid": False}, status=status.HTTP_200_OK)
    else:
        return Response({"errors": form.errors}, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(responses={200: CheckStatusNewAdminUserResponseSerializer})
@api_view(["GET"])
@permission_classes([AllowAny])
def checkStatusNewAdminUser(request):
    """
    Allows the client to check on the status of a validation request for a given user.

    This view is invoked by the UI to monitor progress of validation requests.

    """
    email = request.query_params.get("email", None)
    form = EmailForm({"email": email})
    if form.is_valid():
        try:
            user = User.objects.get(email=email)
            isPending = user.profile.pendingValidation

            # we don't want to show that verification has failed if we passed the user through to the
            # join flow
            is_student_or_grader = (user.student_courses.count() > 0) or (
                user.grader_courses.count() > 0
            )

            isActivated = (
                False
                if isPending
                else (user.profile.canModifyRosters or is_student_or_grader)
            )
            return Response(
                {"pending": isPending, "status": isActivated}, status=status.HTTP_200_OK
            )
        except User.DoesNotExist:
            return Response(
                {"pending": False, "status": False}, status=status.HTTP_200_OK
            )
    else:
        return Response({"errors": form.errors}, status=status.HTTP_400_BAD_REQUEST)


def approve_new_admin_user(user, auto_approved=False, org_name=""):
    """
    Approve the creation of a new user who has the ability to create new courses.
    """
    user.profile.canModifyRosters = True
    user.profile.canCreateCourses = True
    user.profile.pendingValidation = False
    user.is_active = True
    user.save()
    user.profile.save()

    # send registration email
    org = user.profile.organization
    resolved_org_name = org_name or (org.name if org else "Unknown")

    NewAdminActivationEmail(
        user=user
    ).send_email(organization_name=resolved_org_name)

    logEvent(
        "Admin New Request Approved",
        level=logging.WARNING,
        message=json.dumps(
            {
                "user": user.email,
                "organization": org.name if org else "",
                "shortname": org.shortname if org else "",
            }
        ),
        event_type='audit',
    )

    meta = {"auto_approved": auto_approved}

    Event.objects.create(
        category="registration",
        user=str(user),
        description="New admin signup",
        meta=json.dumps(meta),
    )


##########################################################################
##################################### PW RESET FUNCTIONS #################
#####################################                   ##################
##########################################################################


@extend_schema(
    request=EmailPasswordResetRequestSerializer,
    responses={200: EmailPasswordResetResponseSerializer}
)
@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthAnonRateThrottle])
def emailPasswordReset(request):

    form = EmailForm(request.data)
    if form.is_valid():
        try:
            user = User.objects.get(email=form.cleaned_data["email"])

            PasswordResetEmail(user=user).send_email()

        except User.DoesNotExist:
            pass

        return Response({"success": True}, status=status.HTTP_200_OK)
    else:
        return Response(
            {"success": False, "errors": form.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )


@extend_schema(
    request=VerifyResetTokenRequestSerializer,
    responses={200: VerifyResetTokenResponseSerializer}
)
@api_view(["POST"])
@permission_classes([AllowAny])
def verifyResetToken(request):
    """
    Handle valid verify email links sent after password reset requests.
    """
    form = ValidateTokenForm(request.data)
    if form.is_valid():
        uid_int = urlsafe_base64_decode(form.cleaned_data["uid"]).decode()
        try:
            user = User.objects.get(id=uid_int)
            isValid = default_token_generator.check_token(
                user, form.cleaned_data["token"]
            )
            return Response(
                {"isValid": isValid, "email": user.email}, status=status.HTTP_200_OK
            )
        except User.ObjectDoesNotExist:
            return Response({"isValid": False}, status=status.HTTP_200_OK)
    else:
        return Response({"isValid": False}, status=status.HTTP_200_OK)


@extend_schema(
    request=ResetPasswordRequestSerializer,
    responses={200: ResetPasswordResponseSerializer}
)
@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthAnonRateThrottle])
def resetPassword(request):
    form = ChangePasswordForm(request.data)
    if form.is_valid():
        uid_int = urlsafe_base64_decode(form.cleaned_data["uid"]).decode()
        try:
            user = User.objects.get(id=uid_int)
            isValid = default_token_generator.check_token(
                user, form.cleaned_data["token"]
            )
            if isValid:
                # Use password2 (validated by PasswordMatchForm._post_clean)
                # instead of the unvalidated password field
                password = form.cleaned_data.get("password2") or form.cleaned_data["password"]
                user.set_password(password)
                user.profile.isPasswordSet = True
                user.save()
            return Response(
                {"isValid": isValid, "success": True}, status=status.HTTP_200_OK
            )
        except User.DoesNotExist:
            return Response(
                {"isValid": False, "success": False}, status=status.HTTP_200_OK
            )
    else:
        return Response({"isValid": False, "succes": False}, status=status.HTTP_200_OK)
