# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Tests for the superadmin autograding stats endpoint, the error classifier,
and the execution-event recorder.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import AutograderExecutionEvent, Course, Organization
from core.tests.factories import AutograderExecutionEventFactory

STATS_URL = '/dashboard/autograding_stats/'


class AutogradingStatsPermissionsTestCase(APITestCase):

    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username='super@codepost.io', email='super@codepost.io', password='SuperPass1!')
        self.regular_user = User.objects.create_user(
            username='regular@rutgers.edu', email='regular@rutgers.edu', password='TestPass1!')

    def test_anonymous_denied(self):
        response = self.client.get(STATS_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_denied(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.get(STATS_URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_superuser_allowed(self):
        self.client.force_authenticate(user=self.superuser)
        response = self.client.get(STATS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class AutogradingStatsAggregationTestCase(APITestCase):

    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username='super@codepost.io', email='super@codepost.io', password='SuperPass1!')
        self.client.force_authenticate(user=self.superuser)

        # In-window events:
        # 3 cache hits (python), 2 successful executions (python),
        # 2 failed executions (java, missing_dependency + runtime_error),
        # 1 failed execution with empty language.
        AutograderExecutionEventFactory.create_batch(3, cached=True, language='python-3.12')
        AutograderExecutionEventFactory.create_batch(2, cached=False, language='python-3.12')
        AutograderExecutionEventFactory(
            cached=False, success=False, language='java-17',
            error_category='missing_dependency',
            error_message="ModuleNotFoundError: No module named 'pandas'")
        AutograderExecutionEventFactory(
            cached=False, success=False, language='java-17',
            error_category='missing_dependency',
            error_message='package org.junit does not exist')
        AutograderExecutionEventFactory(
            cached=False, success=False, language='',
            error_category='runtime_error', error_message='NullPointerException')

    def test_aggregates(self):
        response = self.client.get(STATS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(data['totalRequests'], 8)
        self.assertEqual(data['cacheHits'], 3)
        self.assertEqual(data['actualExecutions'], 5)
        self.assertEqual(data['failedExecutions'], 3)
        self.assertAlmostEqual(data['cacheHitRate'], 3 / 8, places=4)

        usage = {row['language']: row['count'] for row in data['languageUsage']}
        self.assertEqual(usage, {'python-3.12': 5, 'java-17': 2, 'unknown': 1})
        # Ordered by count descending
        self.assertEqual(data['languageUsage'][0]['language'], 'python-3.12')

        failures = {row['language']: row for row in data['failuresPerLanguage']}
        self.assertEqual(set(failures), {'java-17', 'unknown'})
        self.assertEqual(failures['java-17']['failures'], 2)
        self.assertEqual(failures['java-17']['executions'], 2)
        self.assertAlmostEqual(failures['java-17']['failureRate'], 1.0, places=4)
        # Languages with no failures are excluded
        self.assertNotIn('python-3.12', failures)

        self.assertEqual(data['topErrors'][0]['category'], 'missing_dependency')
        self.assertEqual(data['topErrors'][0]['count'], 2)
        # Sample is the most recent message in that category
        self.assertEqual(data['topErrors'][0]['sampleMessage'],
                         'package org.junit does not exist')
        self.assertEqual(data['topErrors'][1]['category'], 'runtime_error')

    def test_date_filtering_excludes_out_of_range(self):
        old_event = AutograderExecutionEventFactory(cached=True)
        AutograderExecutionEvent.objects.filter(pk=old_event.pk).update(
            created=timezone.now() - timedelta(days=90))

        response = self.client.get(STATS_URL)  # default: last 30 days
        self.assertEqual(response.json()['totalRequests'], 8)

        response = self.client.get(STATS_URL, {
            'dateFrom': (timezone.now() - timedelta(days=120)).isoformat(),
            'dateTo': timezone.now().isoformat(),
        })
        self.assertEqual(response.json()['totalRequests'], 9)

    def test_invalid_range_rejected(self):
        response = self.client.get(STATS_URL, {
            'dateFrom': timezone.now().isoformat(),
            'dateTo': (timezone.now() - timedelta(days=1)).isoformat(),
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_date_only_params_accepted(self):
        response = self.client.get(STATS_URL, {
            'dateFrom': (timezone.now() - timedelta(days=31)).date().isoformat(),
            'dateTo': (timezone.now() + timedelta(days=1)).date().isoformat(),
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['totalRequests'], 8)

    def test_course_deletion_preserves_events(self):
        org = Organization.objects.create(name="Rutgers", shortname="rutgers")
        course = Course.objects.create(name="CS111", period="F2026", organization=org)
        event = AutograderExecutionEventFactory(course=course, cached=True)

        course.delete()
        event.refresh_from_db()
        self.assertIsNone(event.course)

        response = self.client.get(STATS_URL)
        self.assertEqual(response.json()['totalRequests'], 9)


class ErrorClassifierTestCase(TestCase):

    def test_categories(self):
        from autograder.services.error_classifier import classify_error
        cases = [
            ("Execution timeout or incomplete", 'timeout'),
            ("SoftTimeLimitExceeded()", 'timeout'),
            ("Traceback (most recent call last):\n  File \"x.py\"\nModuleNotFoundError: No module named 'numpy'", 'missing_dependency'),
            ("Error: Cannot find module 'express'", 'missing_dependency'),
            ("Error in library(dplyr) : there is no package called 'dplyr'", 'missing_dependency'),
            ("Main.java:3: error: package org.junit does not exist", 'missing_dependency'),
            ("  File \"solution.py\", line 2\n    def f(:\nSyntaxError: invalid syntax", 'compile_error'),
            ("Main.java:10: error: cannot find symbol", 'compile_error'),
            ("Failed to extract results: missing markers. Stdout preview: ...", 'marker_extraction'),
            ("Failed to extract results: missing markers. Stdout preview:  Stderr tail: exec /usr/local/bin/python: argument list too long", 'infra'),
            ("docker: Error response from daemon: image not found", 'infra'),
            ("No executor found for file: main.xyz", 'infra'),
            ("Cache save failed: disk full", 'infra'),
            ("Traceback (most recent call last):\n  File \"x.py\"\nZeroDivisionError: division by zero", 'runtime_error'),
            ("some completely novel failure output", 'runtime_error'),
        ]
        for text, expected in cases:
            category, message = classify_error(text)
            self.assertEqual(category, expected, msg=f"{text!r} -> {category}, expected {expected}")
            self.assertTrue(message)

    def test_empty_input_is_unknown(self):
        from autograder.services.error_classifier import classify_error
        self.assertEqual(classify_error(None), ('unknown', ''))
        self.assertEqual(classify_error('   '), ('unknown', ''))

    def test_marker_error_leads_with_real_cause(self):
        """extract_json_result now puts the last stderr line first, so the sampled
        message names the cause instead of the generic marker complaint."""
        from autograder.services.executors.base import NotebookExecutor
        from autograder.services.error_classifier import classify_error
        result = NotebookExecutor.extract_json_result(
            stdout="",
            stderr="some earlier noise\nexec /usr/local/bin/python: argument list too long\n")
        assert result.err is not None
        self.assertTrue(result.err.startswith("exec /usr/local/bin/python: argument list too long\n"))
        self.assertIn("missing markers", result.err)
        category, message = classify_error(result.err)
        self.assertEqual(category, 'infra')
        self.assertEqual(message, "exec /usr/local/bin/python: argument list too long")

    def test_python_traceback_sample_is_last_line(self):
        from autograder.services.error_classifier import classify_error
        _, message = classify_error(
            "Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: bad input")
        self.assertEqual(message, "ValueError: bad input")

    def test_message_truncated_to_500(self):
        from autograder.services.error_classifier import classify_error
        _, message = classify_error("x" * 2000)
        self.assertEqual(len(message), 500)


class RecorderResilienceTestCase(TestCase):

    def test_recording_failure_is_logged_not_raised(self):
        from autograder.services.execution_events import record_execution_event
        with mock.patch.object(AutograderExecutionEvent.objects, 'create',
                               side_effect=RuntimeError('db down')):
            with self.assertLogs('autograder.services.execution_events', level='ERROR') as logs:
                record_execution_event(trigger='file_run', cached=False, success=True)
        self.assertIn('Failed to record autograder execution event', logs.output[0])
        self.assertEqual(AutograderExecutionEvent.objects.count(), 0)

    def test_failed_event_without_error_text_is_unknown(self):
        from autograder.services.execution_events import record_execution_event
        record_execution_event(trigger='file_run', cached=False, success=False)
        event = AutograderExecutionEvent.objects.get()
        self.assertEqual(event.error_category, 'unknown')
        self.assertEqual(event.error_message, '')
