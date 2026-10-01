# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Tests for AIService:
- Config resolution (course own vs org inheritance)
- Cost estimation
- Usage recording
- is_configured property
"""
import json
from types import SimpleNamespace
from typing import cast
from decimal import Decimal
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.db.models.signals import post_save

import factory.django

from core.models import Course, AIUsageRecord
from core.services.ai_service import AIService, GenerationResult
from core.tests.factories import (
    CourseFactory,
    OrganizationFactory,
)


# ---------------------------------------------------------------------------
# Helpers — build lightweight namespace objects for pure-unit tests where we
# don't need the database but DO need the same attribute interface.
# ---------------------------------------------------------------------------

def _make_org(**overrides):
    defaults = dict(
        ai_provider=None,
        ai_api_key=None,
        ai_base_url=None,
        ai_model=None,
        ai_disabled=False,
        ai_comments_disabled=False,
        ai_course_policy='none',
        ai_enabled_courses=SimpleNamespace(filter=lambda **kw: SimpleNamespace(exists=lambda: False)),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_course(**overrides):
    defaults = dict(
        pk=999,
        ai_provider=None,
        ai_api_key=None,
        ai_base_url=None,
        ai_model=None,
        ai_disabled=False,
        ai_comments_disabled=False,
        ai_use_own_settings=False,
        organization=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ===========================================================================
# Pure unit tests (no DB required)
# ===========================================================================

class TestAIServiceConfigResolution(TestCase):
    """Test that AIService.__init__ correctly resolves config based on
    course.ai_use_own_settings and organization settings."""

    # --- Course uses its own key ---

    def test_course_uses_own_when_flag_true_and_provider_set(self):
        course = _make_course(
            ai_use_own_settings=True,
            ai_provider='openai',
            ai_api_key='course-key',
            ai_model='gpt-4o',
        )
        svc = AIService(cast(Course, course))
        self.assertEqual(svc.provider, 'openai')
        self.assertEqual(svc.api_key, 'course-key')
        self.assertEqual(svc.model, 'gpt-4o')
        self.assertTrue(svc.is_configured)

    def test_course_use_own_true_no_provider_falls_through(self):
        """ai_use_own_settings=True but no provider → uses course fields (empty)."""
        course = _make_course(ai_use_own_settings=True)
        svc = AIService(cast(Course, course))
        self.assertIsNone(svc.provider)
        self.assertFalse(svc.is_configured)

    # --- Org inheritance (course.ai_use_own_settings=False) ---

    def test_inherits_from_org_when_policy_all(self):
        org = _make_org(
            ai_provider='gemini',
            ai_api_key='org-key',
            ai_model='gemini-2.5-flash',
            ai_course_policy='all',
        )
        course = _make_course(ai_use_own_settings=False, organization=org)
        svc = AIService(cast(Course, course))
        self.assertEqual(svc.provider, 'gemini')
        self.assertEqual(svc.api_key, 'org-key')
        self.assertEqual(svc.model, 'gemini-2.5-flash')
        self.assertTrue(svc.is_configured)

    def test_inherits_from_org_when_policy_selected_and_course_in_list(self):
        """When policy='selected' and the course IS in the enabled list."""
        org = _make_org(
            ai_provider='openai',
            ai_api_key='org-openai-key',
            ai_model='gpt-4o-mini',
            ai_course_policy='selected',
            # Simulate the M2M filter returning True
            ai_enabled_courses=SimpleNamespace(
                filter=lambda **kw: SimpleNamespace(exists=lambda: True)
            ),
        )
        course = _make_course(ai_use_own_settings=False, organization=org)
        svc = AIService(cast(Course, course))
        self.assertEqual(svc.provider, 'openai')
        self.assertEqual(svc.api_key, 'org-openai-key')

    def test_does_not_inherit_when_policy_selected_and_course_not_in_list(self):
        """When policy='selected' but the course is NOT in the enabled list,
        falls back to course fields (empty)."""
        org = _make_org(
            ai_provider='openai',
            ai_api_key='org-key',
            ai_course_policy='selected',
            ai_enabled_courses=SimpleNamespace(
                filter=lambda **kw: SimpleNamespace(exists=lambda: False)
            ),
        )
        course = _make_course(ai_use_own_settings=False, organization=org)
        svc = AIService(cast(Course, course))
        self.assertIsNone(svc.provider)
        self.assertFalse(svc.is_configured)

    def test_does_not_inherit_when_policy_none(self):
        org = _make_org(
            ai_provider='gemini',
            ai_api_key='org-key',
            ai_course_policy='none',
        )
        course = _make_course(ai_use_own_settings=False, organization=org)
        svc = AIService(cast(Course, course))
        self.assertIsNone(svc.provider)
        self.assertFalse(svc.is_configured)

    def test_does_not_inherit_when_org_disabled(self):
        org = _make_org(
            ai_provider='gemini',
            ai_api_key='org-key',
            ai_course_policy='all',
            ai_disabled=True,
        )
        course = _make_course(ai_use_own_settings=False, organization=org)
        svc = AIService(cast(Course, course))
        self.assertIsNone(svc.provider)
        self.assertFalse(svc.is_configured)

    def test_does_not_inherit_when_org_has_no_key(self):
        org = _make_org(
            ai_provider='gemini',
            ai_api_key=None,
            ai_course_policy='all',
        )
        course = _make_course(ai_use_own_settings=False, organization=org)
        svc = AIService(cast(Course, course))
        self.assertIsNone(svc.provider)
        self.assertFalse(svc.is_configured)

    def test_course_with_no_org_uses_own_fields(self):
        """Course not attached to any organization → uses its own fields."""
        course = _make_course(
            ai_use_own_settings=False,
            organization=None,
            ai_provider='gemini',
            ai_api_key='solo-key',
        )
        svc = AIService(cast(Course, course))
        self.assertEqual(svc.provider, 'gemini')
        self.assertEqual(svc.api_key, 'solo-key')
        self.assertTrue(svc.is_configured)

    def test_org_model_provides_default_model(self):
        """When org has a provider but no model, default model is assigned."""
        org = _make_org(
            ai_provider='openai',
            ai_api_key='org-key',
            ai_model=None,
            ai_course_policy='all',
        )
        course = _make_course(ai_use_own_settings=False, organization=org)
        svc = AIService(cast(Course, course))
        self.assertEqual(svc.model, 'gpt-4o-mini')

    def test_course_own_provides_default_model(self):
        """When course has its own provider but no model, default is assigned."""
        course = _make_course(
            ai_use_own_settings=True,
            ai_provider='gemini',
            ai_api_key='key',
            ai_model=None,
        )
        svc = AIService(cast(Course, course))
        self.assertEqual(svc.model, 'gemini-3-flash-preview')

    def test_portkey_provider_configures_correctly(self):
        """Portkey provider should resolve with URL and optional key."""
        course = _make_course(
            ai_use_own_settings=True,
            ai_provider='portkey',
            ai_api_key='pk-test-key',
            ai_base_url='https://portkey.rutgers.edu/v1',
            ai_model=None,
        )
        svc = AIService(cast(Course, course))
        self.assertEqual(svc.provider, 'portkey')
        self.assertEqual(svc.api_key, 'pk-test-key')
        self.assertEqual(svc.base_url, 'https://portkey.rutgers.edu/v1')
        self.assertEqual(svc.model, 'default')
        self.assertTrue(svc.is_configured)

    def test_portkey_provider_without_api_key(self):
        """Portkey (self-hosted gateway) should be configured with only a URL, no API key."""
        course = _make_course(
            ai_use_own_settings=True,
            ai_provider='portkey',
            ai_api_key=None,
            ai_base_url='http://portkey-gateway.local:8787',
            ai_model='gpt-4o',
        )
        svc = AIService(cast(Course, course))
        self.assertEqual(svc.provider, 'portkey')
        self.assertFalse(svc.api_key)
        self.assertEqual(svc.base_url, 'http://portkey-gateway.local:8787')
        self.assertEqual(svc.model, 'gpt-4o')
        self.assertTrue(svc.is_configured)

    def test_portkey_blank_base_url_is_configured(self):
        """Portkey with a blank base URL (official Portkey API) is a supported
        config — _call_portkey falls back to DEFAULT_PORTKEY_URL — so
        is_configured and feature resolution must treat it as configured."""
        course = _make_course(
            ai_use_own_settings=True,
            ai_provider='portkey',
            ai_api_key='pk-key',
            ai_base_url=None,
            ai_model='@gcp/gemini-2.5-flash',
        )
        svc = AIService(cast(Course, course))
        self.assertTrue(svc.is_configured)
        self.assertTrue(svc.is_feature_enabled('quiz_generation'))
        # default-off feature turns on once the course opts in
        course.ai_feature_config = {'personalized_quiz_generation': True}
        self.assertTrue(svc.is_feature_enabled('personalized_quiz_generation'))

    def test_undecrypted_api_key_produces_actionable_error(self):
        """An EncryptedCharField read with the wrong FIELD_ENCRYPTION_KEY silently
        yields Fernet ciphertext; the provider then rejects it as an invalid key.
        The friendly error must name the real cause instead of blaming the key."""
        course = _make_course(
            ai_use_own_settings=True,
            ai_provider='gemini',
            ai_api_key='gAAAAABoffFakeCiphertextValue',
        )
        svc = AIService(cast(Course, course))
        self.assertTrue(svc.api_key_looks_undecrypted)
        msg = svc._parse_error(Exception('API key not valid. Please pass a valid API key.'))
        self.assertIn('FIELD_ENCRYPTION_KEY', msg)

        # A normal key keeps the standard message.
        course.ai_api_key = 'AIzaRealLookingKey'
        svc = AIService(cast(Course, course))
        self.assertFalse(svc.api_key_looks_undecrypted)
        msg = svc._parse_error(Exception('API key not valid. Please pass a valid API key.'))
        self.assertIn('Invalid API key', msg)

    def test_ollama_blank_base_url_is_configured(self):
        """Ollama with a blank base URL falls back to DEFAULT_OLLAMA_URL in
        _call_ollama, so provider alone counts as configured."""
        course = _make_course(
            ai_use_own_settings=True,
            ai_provider='ollama',
            ai_api_key=None,
            ai_base_url=None,
            ai_model='llama3',
        )
        svc = AIService(cast(Course, course))
        self.assertTrue(svc.is_configured)


class TestForConfigConnectionTest(TestCase):
    """AIService.for_config + test_connection (course-less instances)."""

    def test_for_config_portkey_blank_base_url_is_testable(self):
        # _call_portkey falls back to DEFAULT_PORTKEY_URL when base_url is
        # blank — test_connection must accept this config.
        async def fake_dispatch(self, system_prompt, user_prompt, images=()):
            return ('OK', 3, 1, 4, 0)

        svc = AIService.for_config('portkey', api_key='pk-key', base_url='', model='')
        self.assertEqual(svc.model, 'default')
        with patch('core.services.ai_service.AIService._dispatch_provider', new=fake_dispatch):
            result = async_to_sync(svc.test_connection)()
        self.assertTrue(result['success'])
        self.assertEqual(result['response'], 'OK')
        self.assertIsNotNone(result['latencyMs'])
        self.assertIsNone(result['reportedModel'])
        self.assertEqual(result['requestUserPrompt'], 'Reply with exactly: OK')
        self.assertTrue(result['requestSystemPrompt'])

    def test_custom_prompt_used_and_clamped(self):
        captured = {}

        async def fake_dispatch(self, system_prompt, user_prompt, images=()):
            captured['user_prompt'] = user_prompt
            return ('4', 3, 1, 4, 0)

        svc = AIService.for_config('portkey', api_key='pk-key')
        long_prompt = 'y' * 600
        with patch('core.services.ai_service.AIService._dispatch_provider', new=fake_dispatch):
            result = async_to_sync(svc.test_connection)(prompt=long_prompt)
        self.assertTrue(result['success'])
        self.assertEqual(len(captured['user_prompt']), AIService.TEST_PROMPT_MAX_CHARS)
        self.assertEqual(result['requestUserPrompt'], captured['user_prompt'])

    def test_system_prompt_differs_for_custom_prompt(self):
        async def fake_dispatch(self, system_prompt, user_prompt, images=()):
            return ('OK', 3, 1, 4, 0)

        svc = AIService.for_config('portkey', api_key='pk-key')
        with patch('core.services.ai_service.AIService._dispatch_provider', new=fake_dispatch):
            default_run = async_to_sync(svc.test_connection)()
            custom_run = async_to_sync(svc.test_connection)(prompt='What is 2+2?')
        self.assertIn('connectivity test', default_run['requestSystemPrompt'])
        self.assertIn('helpful assistant', custom_run['requestSystemPrompt'])

    def test_whitespace_prompt_falls_back_to_default_ping(self):
        async def fake_dispatch(self, system_prompt, user_prompt, images=()):
            return ('OK', 3, 1, 4, 0)

        svc = AIService.for_config('portkey', api_key='pk-key')
        with patch('core.services.ai_service.AIService._dispatch_provider', new=fake_dispatch):
            result = async_to_sync(svc.test_connection)(prompt='   ')
        self.assertEqual(result['requestUserPrompt'], 'Reply with exactly: OK')
        self.assertIn('connectivity test', result['requestSystemPrompt'])

    def test_model_override_applies_to_request_and_result(self):
        captured = {}

        async def fake_dispatch(self, system_prompt, user_prompt, images=()):
            captured['model'] = self.model
            return ('OK', 3, 1, 4, 0)

        svc = AIService.for_config('portkey', api_key='pk-key', model='default')
        with patch('core.services.ai_service.AIService._dispatch_provider', new=fake_dispatch):
            result = async_to_sync(svc.test_connection)(model='gpt-4o')
        self.assertEqual(captured['model'], 'gpt-4o')
        self.assertEqual(result['model'], 'gpt-4o')

    def test_reported_model_surfaced_from_provider_meta(self):
        async def fake_dispatch(self, system_prompt, user_prompt, images=()):
            self._last_provider_meta = {'model': 'gemini-3-flash-preview-001'}
            return ('OK', 3, 1, 4, 0)

        svc = AIService.for_config('portkey', api_key='pk-key', model='default')
        with patch('core.services.ai_service.AIService._dispatch_provider', new=fake_dispatch):
            result = async_to_sync(svc.test_connection)()
        self.assertTrue(result['success'])
        self.assertEqual(result['reportedModel'], 'gemini-3-flash-preview-001')

    def test_unconfigured_provider_fails_without_calling_provider(self):
        svc = AIService.for_config('')
        result = async_to_sync(svc.test_connection)()
        self.assertFalse(result['success'])
        self.assertIn('No AI provider', result['error'])
        # No request was attempted, so no request prompts are reported
        self.assertIsNone(result['requestSystemPrompt'])
        self.assertIsNone(result['requestUserPrompt'])

    def test_openai_without_key_fails_without_calling_provider(self):
        svc = AIService.for_config('openai')
        result = async_to_sync(svc.test_connection)()
        self.assertFalse(result['success'])
        self.assertIn('No API key', result['error'])


# ===========================================================================
# Per-feature model resolution
# ===========================================================================

class TestPerFeatureModelResolution(TestCase):
    """Test AIService.model_for_feature and the set_request_context hook."""

    def _org_inherited_service(self, course_models=None, org_models=None):
        """Course inheriting AI config from its org (policy 'all')."""
        org = _make_org(
            ai_provider='gemini',
            ai_api_key='org-key',
            ai_model='gemini-2.5-flash',
            ai_course_policy='all',
            ai_feature_models=org_models or {},
        )
        course = _make_course(
            ai_use_own_settings=False,
            organization=org,
            ai_feature_models=course_models or {},
        )
        return AIService(cast(Course, course))

    def test_no_override_uses_base_model(self):
        svc = self._org_inherited_service()
        self.assertEqual(svc.model_for_feature('quiz_generation'), 'gemini-2.5-flash')

    def test_org_override_applies_when_inheriting(self):
        svc = self._org_inherited_service(org_models={'quiz_generation': 'gemini-2.5-pro'})
        self.assertEqual(svc.model_for_feature('quiz_generation'), 'gemini-2.5-pro')
        # Other features keep the base model
        self.assertEqual(svc.model_for_feature('comment_generation'), 'gemini-2.5-flash')

    def test_course_override_wins_over_org(self):
        svc = self._org_inherited_service(
            course_models={'quiz_generation': 'gemini-3-pro-preview'},
            org_models={'quiz_generation': 'gemini-2.5-pro'},
        )
        self.assertEqual(svc.model_for_feature('quiz_generation'), 'gemini-3-pro-preview')

    def test_org_override_ignored_when_course_uses_own_settings(self):
        """A course on its own provider must not pick up org feature models."""
        org = _make_org(
            ai_provider='gemini',
            ai_api_key='org-key',
            ai_model='gemini-2.5-flash',
            ai_course_policy='all',
            ai_feature_models={'quiz_generation': 'gemini-2.5-pro'},
        )
        course = _make_course(
            ai_use_own_settings=True,
            ai_provider='openai',
            ai_api_key='course-key',
            ai_model='gpt-4o',
            organization=org,
        )
        svc = AIService(cast(Course, course))
        self.assertEqual(svc.model_for_feature('quiz_generation'), 'gpt-4o')

    def test_file_suggestions_maps_to_suggested_comments(self):
        svc = self._org_inherited_service(course_models={'suggested_comments': 'gemini-2.5-pro'})
        self.assertEqual(svc.model_for_feature('file_suggestions'), 'gemini-2.5-pro')

    def test_set_request_context_switches_and_resets_model(self):
        svc = self._org_inherited_service(course_models={'quiz_generation': 'gemini-2.5-pro'})
        svc.set_request_context(request_type='quiz_generation')
        self.assertEqual(svc.model, 'gemini-2.5-pro')
        # A subsequent request for a feature without an override resolves
        # from the base model, not the previous override.
        svc.set_request_context(request_type='comment_generation')
        self.assertEqual(svc.model, 'gemini-2.5-flash')

    def test_unknown_feature_key_falls_back_to_base(self):
        svc = self._org_inherited_service(course_models={'quiz_generation': 'gemini-2.5-pro'})
        self.assertEqual(svc.model_for_feature('nonexistent_feature'), 'gemini-2.5-flash')

    def test_get_feature_models_covers_registry(self):
        from core.ai_features.registry import ai_feature_registry
        svc = self._org_inherited_service(org_models={'submission_summary': 'gemini-2.5-pro'})
        resolved = svc.get_feature_models()
        self.assertEqual(set(resolved.keys()), set(ai_feature_registry.keys()))
        self.assertEqual(resolved['submission_summary'], 'gemini-2.5-pro')
        self.assertEqual(resolved['comment_generation'], 'gemini-2.5-flash')


# ===========================================================================
# Cost estimation
# ===========================================================================

class TestEstimateCost(TestCase):
    """Test AIService.estimate_cost static method."""

    def test_known_model_returns_cost(self):
        # gpt-4o-mini: input $0.15/M, output $0.60/M
        cost = AIService.estimate_cost('openai', 'gpt-4o-mini', 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 0.75, places=4)

    def test_unknown_model_returns_zero(self):
        cost = AIService.estimate_cost('openai', 'unknown-model', 500, 500)
        self.assertEqual(cost, 0.0)

    def test_ollama_returns_zero(self):
        cost = AIService.estimate_cost('ollama', 'llama3.2', 10000, 10000)
        self.assertEqual(cost, 0.0)

    def test_zero_tokens_returns_zero(self):
        cost = AIService.estimate_cost('openai', 'gpt-4o-mini', 0, 0)
        self.assertEqual(cost, 0.0)

    def test_gemini_flash_cost(self):
        # gemini-2.5-flash: input $0.15/M, output $0.60/M
        cost = AIService.estimate_cost('gemini', 'gemini-2.5-flash', 100_000, 50_000)
        expected = (100_000 / 1_000_000) * 0.15 + (50_000 / 1_000_000) * 0.60
        self.assertAlmostEqual(cost, expected, places=6)

    def test_gateway_model_falls_back_to_basename_default(self):
        # Portkey-style slash-prefixed id should match the bare TOKEN_RATES entry
        cost = AIService.estimate_cost('portkey', 'some-gateway/gemini-2.5-flash', 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 0.15 + 0.60, places=4)

    def test_gateway_model_exact_custom_rate_wins_over_basename(self):
        custom = {'some-gateway/gemini-2.5-flash': {'input': 1.0, 'output': 2.0}}
        cost = AIService.estimate_cost(
            'portkey', 'some-gateway/gemini-2.5-flash', 1_000_000, 1_000_000, custom_rates=custom)
        self.assertAlmostEqual(cost, 3.0, places=4)

    def test_gateway_model_basename_custom_rate_matches(self):
        custom = {'gemini-2.5-flash': {'input': 1.0, 'output': 2.0}}
        cost = AIService.estimate_cost(
            'portkey', 'some-gateway/gemini-2.5-flash', 1_000_000, 1_000_000, custom_rates=custom)
        self.assertAlmostEqual(cost, 3.0, places=4)


# ===========================================================================
# Usage recording (requires DB)
# ===========================================================================

@factory.django.mute_signals(post_save)
class TestRecordUsage(TestCase):
    """Test AIService.record_usage persists AIUsageRecord."""

    def setUp(self):
        self.org = OrganizationFactory(name='TestOrg', shortname='testorg')
        self.course = CourseFactory(
            name='TestCourse',
            period='s2026',
            organization=self.org,
        )
        self.user = self.course.courseAdmins.first()

        # Configure course with its own AI
        self.course.ai_provider = 'openai'
        self.course.ai_api_key = 'test-key'
        self.course.ai_model = 'gpt-4o-mini'
        self.course.ai_use_own_settings = True
        self.course.save()

    def test_record_usage_creates_record(self):
        svc = AIService(self.course)
        result = GenerationResult(
            text='Some feedback',
            success=True,
            input_tokens=500,
            output_tokens=200,
            total_tokens=700,
        )
        svc.record_usage(result, self.user, request_type='comment_generation')

        records = AIUsageRecord.objects.filter(course=self.course)
        self.assertEqual(records.count(), 1)

        record = records.first()
        self.assertEqual(record.provider, 'openai')
        self.assertEqual(record.model, 'gpt-4o-mini')
        self.assertEqual(record.request_type, 'comment_generation')
        self.assertEqual(record.input_tokens, 500)
        self.assertEqual(record.output_tokens, 200)
        self.assertEqual(record.total_tokens, 700)
        self.assertEqual(record.status, 'success')
        self.assertIsNone(record.error_message)
        self.assertEqual(record.user, self.user)
        self.assertEqual(record.organization, self.org)
        self.assertGreater(record.estimated_cost, Decimal('0'))

    def test_record_usage_error_result(self):
        svc = AIService(self.course)
        result = GenerationResult(
            text='',
            success=False,
            error='API rate limit exceeded',
            input_tokens=100,
            output_tokens=0,
            total_tokens=100,
        )
        svc.record_usage(result, self.user, request_type='test_generation')

        record = AIUsageRecord.objects.filter(course=self.course).first()
        self.assertEqual(record.status, 'error')
        self.assertEqual(record.error_message, 'API rate limit exceeded')
        self.assertEqual(record.request_type, 'test_generation')

    def test_org_token_rates_produce_nonzero_course_cost(self):
        self.org.ai_token_rates = {'my-custom-model': {'input': 1.0, 'output': 2.0}}
        self.org.save()
        self.course.ai_model = 'my-custom-model'
        self.course.save()

        svc = AIService(self.course)
        result = GenerationResult(
            text='x', success=True,
            input_tokens=1_000_000, output_tokens=1_000_000, total_tokens=2_000_000,
        )
        svc.record_usage(result, self.user)

        record = AIUsageRecord.objects.filter(course=self.course).first()
        self.assertEqual(record.estimated_cost, Decimal('3.000000'))

    def test_course_token_rates_override_org(self):
        self.org.ai_token_rates = {'my-custom-model': {'input': 1.0, 'output': 2.0}}
        self.org.save()
        self.course.ai_model = 'my-custom-model'
        self.course.ai_token_rates = {'my-custom-model': {'input': 2.0, 'output': 4.0}}
        self.course.save()

        svc = AIService(self.course)
        result = GenerationResult(
            text='x', success=True,
            input_tokens=1_000_000, output_tokens=1_000_000, total_tokens=2_000_000,
        )
        svc.record_usage(result, self.user)

        record = AIUsageRecord.objects.filter(course=self.course).first()
        self.assertEqual(record.estimated_cost, Decimal('6.000000'))

    def test_for_config_uses_explicit_organization_rates(self):
        # Regression: org-scoped (course-less) usage must see the org's rates.
        self.org.ai_token_rates = {'org-only-model': {'input': 1.0, 'output': 2.0}}
        self.org.save()

        svc = AIService.for_config('openai', api_key='k', model='org-only-model')
        result = GenerationResult(
            text='x', success=True,
            input_tokens=1_000_000, output_tokens=1_000_000, total_tokens=2_000_000,
        )
        svc.record_usage(result, self.user, organization=self.org)

        record = AIUsageRecord.objects.filter(organization=self.org, course=None).first()
        self.assertIsNotNone(record)
        self.assertEqual(record.estimated_cost, Decimal('3.000000'))

    def test_record_usage_multiple_creates_multiple_records(self):
        svc = AIService(self.course)
        for i in range(3):
            result = GenerationResult(
                text=f'Feedback {i}', success=True,
                input_tokens=100 * (i + 1), output_tokens=50, total_tokens=100 * (i + 1) + 50,
            )
            svc.record_usage(result, self.user)

        self.assertEqual(AIUsageRecord.objects.filter(course=self.course).count(), 3)


# ===========================================================================
# Portkey request metadata (observability headers)
# ===========================================================================

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient that records the outbound request."""

    def __init__(self, capture):
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None, timeout=None):
        self._capture['url'] = url
        self._capture['headers'] = headers
        self._capture['json'] = json
        return _FakeResponse({
            'choices': [{'message': {'content': 'hello'}}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8},
        })


def _portkey_course(**overrides):
    defaults = dict(
        ai_use_own_settings=True,
        ai_provider='portkey',
        ai_api_key='pk-test-key',
        ai_base_url='https://portkey.rutgers.edu/v1',
        ai_model='gpt-4o',
        id=7,
    )
    defaults.update(overrides)
    return _make_course(**defaults)


class TestPortkeyRequestMetadata(TestCase):
    """The Portkey call should emit request context as x-portkey-metadata."""

    def _call(self, svc):
        """Drive _call_portkey with a mocked client; return the captured request."""
        capture: dict = {}
        with patch('httpx.AsyncClient', lambda *a, **k: _FakeAsyncClient(capture)):
            text, *_ = async_to_sync(svc._call_portkey)('system', 'user')
        capture['text'] = text
        return capture

    def test_metadata_header_includes_full_context(self):
        svc = AIService(cast(Course, _portkey_course()))
        user = SimpleNamespace(email='grader@rutgers.edu', id=42)
        svc.set_request_context(
            user=cast('object', user),
            request_type='quiz_generation',
            instructions='make it harder',
        )

        capture = self._call(svc)

        self.assertEqual(capture['text'], 'hello')
        headers = capture['headers']
        self.assertIn('x-portkey-metadata', headers)
        metadata = json.loads(headers['x-portkey-metadata'])
        self.assertEqual(metadata['_user'], 'grader@rutgers.edu')
        self.assertEqual(metadata['feature'], 'quiz_generation')
        self.assertEqual(metadata['course'], '7')
        self.assertEqual(metadata['instructions'], 'make it harder')
        # No assignment passed → key omitted
        self.assertNotIn('assignment', metadata)
        # Trace id present and non-empty
        self.assertTrue(headers.get('x-portkey-trace-id'))

    def test_no_context_omits_metadata_headers(self):
        """Without set_request_context the request stays as before (no metadata)."""
        svc = AIService(cast(Course, _portkey_course()))

        capture = self._call(svc)

        headers = capture['headers']
        self.assertNotIn('x-portkey-metadata', headers)
        self.assertNotIn('x-portkey-trace-id', headers)
        # Auth header still set
        self.assertEqual(headers['x-portkey-api-key'], 'pk-test-key')

    def test_instructions_truncated_to_200_chars(self):
        svc = AIService(cast(Course, _portkey_course()))
        svc.set_request_context(request_type='quiz_generation', instructions='x' * 250)

        capture = self._call(svc)

        metadata = json.loads(capture['headers']['x-portkey-metadata'])
        self.assertEqual(len(metadata['instructions']), 200)

    def test_custom_provider_omits_portkey_metadata(self):
        """The shared 'custom' path must not emit vendor-specific headers."""
        svc = AIService(cast(Course, _portkey_course(ai_provider='custom')))
        svc.set_request_context(
            user=cast('object', SimpleNamespace(email='g@r.edu', id=1)),
            request_type='quiz_generation',
            instructions='hi',
        )

        capture = self._call(svc)

        self.assertNotIn('x-portkey-metadata', capture['headers'])
