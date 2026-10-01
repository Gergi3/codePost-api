# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
AI Service for Comment Generation

Supports multiple AI providers through a unified interface:
- Google Gemini (default)
- OpenAI
- Ollama (self-hosted)
- Custom providers via Portkey

Usage:
    from core.services.ai_service import AIService
    
    service = AIService(course)
    result = service.generate_comment(
        selected_content="def foo():\n    pass",
        grader_draft="This function is incomplete",
        rubric_context="Missing implementation",
    )
"""

from __future__ import annotations

from encodings.base64_codec import base64_decode
import hashlib
import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional, Sequence, cast
from dataclasses import dataclass
from core.constants import DEFAULT_OLLAMA_URL, DEFAULT_PORTKEY_URL
from core.models import Course, Assignment, Submission, SubmissionFile, User

import asyncio
import random

if TYPE_CHECKING:
    import pymupdf
    from core.models import PromptExperiment
    from core.prompts.variables import ImageAttachment


# -----------------------------------------------------------------------
# Curated list of models per provider.
# Each entry: (model_id, display_label, is_default)
# -----------------------------------------------------------------------
AI_MODELS: dict[str, list[tuple[str, str, bool]]] = {
    'gemini': [
        ('gemini-3-pro-preview', 'Gemini 3 Pro (Preview)', False),
        ('gemini-3-flash-preview', 'Gemini 3 Flash (Preview)', True),
        ('gemini-2.5-flash', 'Gemini 2.5 Flash', False),
        ('gemini-2.5-pro', 'Gemini 2.5 Pro', False),
        ('gemini-2.0-flash', 'Gemini 2.0 Flash', False),
        ('gemini-1.5-flash', 'Gemini 1.5 Flash', False),
        ('gemini-1.5-pro', 'Gemini 1.5 Pro', False),
    ],
    'openai': [
        ('gpt-4o-mini', 'GPT-4o Mini', True),
        ('gpt-4o', 'GPT-4o', False),
        ('gpt-4.1', 'GPT-4.1', False),
        ('gpt-4.1-mini', 'GPT-4.1 Mini', False),
        ('gpt-4.1-nano', 'GPT-4.1 Nano', False),
        ('o3-mini', 'o3 Mini', False),
    ],
    'ollama': [
        ('llama3.2', 'Llama 3.2', True),
        ('llama3.1', 'Llama 3.1', False),
        ('mistral', 'Mistral', False),
        ('codellama', 'Code Llama', False),
        ('deepseek-coder-v2', 'DeepSeek Coder V2', False),
        ('qwen2.5-coder', 'Qwen 2.5 Coder', False),
        ('phi3', 'Phi-3', False),
    ],
    'portkey': [
        ('default', 'Default (gateway-configured)', True),
    ],
    'custom': [
        ('default', 'Default', True),
    ],
}

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# Provider model listing (queries the provider's API for available models)
# -----------------------------------------------------------------------

async def list_provider_models(
    provider: str,
    api_key: str = '',
    base_url: str = '',
) -> list[dict[str, str]]:
    """
    Query a provider's API for available models.
    Returns a list of dicts: [{'id': '...', 'name': '...'}]
    Raises on network/auth errors.
    """
    if provider == 'gemini':
        return await _list_gemini_models(api_key)
    elif provider == 'openai':
        return await _list_openai_models(api_key)
    elif provider == 'ollama':
        return await _list_ollama_models(base_url)
    elif provider == 'portkey':
        return await _list_portkey_models(api_key, base_url)
    return []


async def _list_gemini_models(api_key: str) -> list[dict[str, str]]:
    """List models from Google Gemini API."""
    from google import genai
    client = genai.Client(api_key=api_key)
    models = []
    pager = await client.aio.models.list()
    async for m in pager:
        model_id = m.name or ''
        # The API returns "models/gemini-2.5-flash" — strip the prefix
        if model_id.startswith('models/'):
            model_id = model_id[len('models/'):]
        display_name = m.display_name or model_id
        # Only include generative models (skip embedding/retrieval models)
        if m.supported_actions and 'generateContent' not in m.supported_actions:
            continue
        models.append({'id': model_id, 'name': display_name})
    return models


async def _list_openai_models(api_key: str) -> list[dict[str, str]]:
    """List models from OpenAI API."""
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)
    response = await client.models.list()
    models = []
    for m in response.data:
        models.append({'id': m.id, 'name': m.id})
    # Sort alphabetically
    models.sort(key=lambda x: x['id'])
    return models


async def _list_ollama_models(base_url: str) -> list[dict[str, str]]:
    """List locally installed models from Ollama."""
    import httpx
    url = (base_url or DEFAULT_OLLAMA_URL).rstrip('/')
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{url}/api/tags", timeout=10.0)
        response.raise_for_status()
        data = response.json()
    models = []
    for m in data.get('models', []):
        model_name = m.get('name', '')
        # Strip ":latest" suffix for cleaner display
        display = model_name.replace(':latest', '') if model_name.endswith(':latest') else model_name
        models.append({'id': model_name, 'name': display})
    return models


async def _list_portkey_models(api_key: str, base_url: str) -> list[dict[str, str]]:
    """List models from Portkey gateway (OpenAI-compatible /v1/models)."""
    import httpx
    url = (base_url or DEFAULT_PORTKEY_URL).rstrip('/')
    if url.endswith('/v1'):
        endpoint = f"{url}/models"
    else:
        endpoint = f"{url}/v1/models"

    headers: dict[str, str] = {}
    if api_key:
        headers['x-portkey-api-key'] = api_key

    async with httpx.AsyncClient() as client:
        response = await client.get(endpoint, headers=headers, timeout=10.0)
        response.raise_for_status()
        data = response.json()
    models = []
    for m in data.get('data', []):
        model_id = m.get('id', '')
        models.append({'id': model_id, 'name': model_id})
    models.sort(key=lambda x: x['id'])
    return models


@dataclass
class GenerationContext:
    """Context for AI comment generation."""
    selected_content: str  # The selected lines/cell
    grader_draft: str = ""  # Grader's draft text to improve
    rubric_context: str = ""  # Rubric comment info if linked
    file_content: str = ""  # Entire file (for 'file' context level)
    all_files_content: str = ""  # All submission + assignment files (for 'all_files' level)
    assignment_name: str = ""
    file_name: str = ""


@dataclass 
class GenerationResult:
    """Result from AI comment generation."""
    text: str
    success: bool
    error: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    variant_id: Optional[int] = None
    # The instructor prompt after {variable} substitution — what the model actually saw
    # (personalized quiz generation records it for staff review).
    resolved_prompt: Optional[str] = None


class AIService:
    """
    AI service for generating grading comments.
    Supports multiple providers through a unified interface.
    """
    # This is to ensure the ai response is consistently in markdown format
    GLOBAL_SYSTEM_PROMPT =""""""

    # Timeout for connection tests (test_connection); shorter than the 60s
    # generation timeouts because a human is waiting on a button.
    TEST_TIMEOUT_SECONDS = 20

    def __init__(self, course: Course, assignment: Optional[Assignment] = None):
        self.course = course
        self.assignment = assignment

        # Resolve effective AI config: course-own settings or org-level
        self._config_from_org = False
        if course.ai_use_own_settings and course.ai_provider:
            # Course explicitly uses its own key
            self.provider = course.ai_provider
            self.api_key = course.ai_api_key
            self.base_url = course.ai_base_url
            self.model = course.ai_model or self._get_default_model()
        elif not course.ai_use_own_settings and course.organization:
            # Try to inherit from org
            org = course.organization
            if (
                org.ai_provider
                and (org.ai_api_key or org.ai_provider in ('ollama', 'portkey'))
                and not org.ai_disabled
                and self._org_allows_course(org, course)
            ):
                self.provider = org.ai_provider
                self.api_key = org.ai_api_key
                self.base_url = org.ai_base_url
                self.model = org.ai_model or self._get_default_model_for(org.ai_provider)
                self._config_from_org = True
            else:
                # Org not available, fall back to course fields (may be empty)
                self.provider = course.ai_provider
                self.api_key = course.ai_api_key
                self.base_url = course.ai_base_url
                self.model = course.ai_model or self._get_default_model()
        else:
            # Default: use course fields (ai_use_own_settings=True but no provider,
            # or no organization attached)
            self.provider = course.ai_provider
            self.api_key = course.ai_api_key
            self.base_url = course.ai_base_url
            self.model = course.ai_model or self._get_default_model()

        # Base model before any per-feature override; set_request_context()
        # swaps self.model to the feature-specific model when one is set.
        self.base_model = self.model

        # Optional request context, attached to outbound provider requests as
        # observability metadata (currently consumed by Portkey). Populated by
        # callers via set_request_context() right before generating.
        self.request_user: Optional[User] = None
        self.request_type: Optional[str] = None
        self.request_instructions: str = ''
        self._trace_id: Optional[str] = None
        # Metadata from the most recent _call_* response (e.g. the model id
        # the provider reported back). Consumed by test_connection.
        self._last_provider_meta: Optional[dict] = None

    @classmethod
    def for_config(cls, provider: str, api_key: str = '', base_url: str = '', model: str = '') -> 'AIService':
        """Build an AIService bound to an explicit config, with no course.

        Used by the connection-test endpoints (e.g. org-level settings, which
        have no course). Only the generation plumbing (_dispatch_provider /
        _call_*) is safe on the returned instance; course-dependent helpers
        (feature toggles, record_usage, prompt resolution) must not be called.
        """
        svc = cls.__new__(cls)
        svc.course = cast(Course, None)
        svc.assignment = None
        svc._config_from_org = False
        svc.provider = provider
        svc.api_key = api_key
        svc.base_url = base_url
        svc.model = model or svc._get_default_model()
        svc.base_model = svc.model
        svc.request_user = None
        svc.request_type = None
        svc.request_instructions = ''
        svc._trace_id = None
        svc._last_provider_meta = None
        return svc

    # Custom test prompts are clamped to keep test-call costs bounded.
    TEST_PROMPT_MAX_CHARS = 500

    async def test_connection(self, prompt: Optional[str] = None, model: Optional[str] = None) -> dict:
        """Fire a minimal completion through the configured provider.

        ``prompt`` optionally replaces the default connectivity prompt so the
        caller can eyeball a real generation. ``model`` optionally overrides
        ``self.model`` for this test only (nothing is saved); length is
        enforced by AIProviderTestRequestSerializer.
        Returns a camelCase dict ready for AIProviderTestResultSerializer.
        Never raises and never records usage (record_usage is caller-driven).
        """
        import time

        if model and model.strip():
            self.model = model.strip()

        user_prompt = (prompt or '').strip()[:self.TEST_PROMPT_MAX_CHARS]
        if user_prompt:
            # A real prompt: don't prime the model with connectivity-test
            # framing or it answers "Connection successful." instead.
            system_prompt = 'You are a helpful assistant. Answer briefly in plain text.'
        else:
            system_prompt = 'You are a connectivity test. Answer briefly in plain text.'
            user_prompt = 'Reply with exactly: OK'

        result: dict = {
            'success': False,
            'provider': self.provider or '',
            'model': self.model or '',
            'reportedModel': None,
            'latencyMs': None,
            'requestSystemPrompt': None,
            'requestUserPrompt': None,
            'response': None,
            'error': None,
            'errorDetail': None,
        }
        # Deliberately not is_configured: ollama/portkey fall back to default
        # base URLs in _call_* when base_url is blank, so provider alone is
        # enough there; only gemini/openai strictly require an API key.
        if not self.provider:
            result['error'] = 'No AI provider is configured.'
            return result
        if self.provider in ('gemini', 'openai') and not self.api_key:
            result['error'] = 'No API key is configured for this provider.'
            return result

        self._last_provider_meta = None
        result['requestSystemPrompt'] = system_prompt
        result['requestUserPrompt'] = user_prompt
        start = time.perf_counter()
        try:
            text, in_tok, out_tok, total_tok, cached_tok = await asyncio.wait_for(
                self._dispatch_provider(system_prompt, user_prompt),
                timeout=self.TEST_TIMEOUT_SECONDS,
            )
            result['latencyMs'] = round((time.perf_counter() - start) * 1000, 1)
            result['success'] = True
            result['response'] = (text or '').strip()[:2000]
            result['reportedModel'] = (self._last_provider_meta or {}).get('model')
            # Private keys for the caller's record_usage — not serialized
            # (AIProviderTestResultSerializer only outputs declared fields).
            result['_inputTokens'] = in_tok
            result['_outputTokens'] = out_tok
            result['_totalTokens'] = total_tok
            result['_cachedTokens'] = cached_tok
        except asyncio.TimeoutError:
            result['latencyMs'] = round((time.perf_counter() - start) * 1000, 1)
            result['error'] = f'The provider did not respond within {self.TEST_TIMEOUT_SECONDS} seconds.'
            result['errorDetail'] = 'TimeoutError'
        except Exception as e:
            result['latencyMs'] = round((time.perf_counter() - start) * 1000, 1)
            result['error'] = self._parse_error(e)
            result['errorDetail'] = f'{type(e).__name__}: {str(e)[:500]}'
        return result

    def set_request_context(
        self,
        user: Optional[User] = None,
        request_type: Optional[str] = None,
        instructions: str = '',
    ) -> 'AIService':
        """Attach caller context for provider-side observability.

        ``user`` and ``request_type`` mirror the values passed to
        ``record_usage``; ``instructions`` is the user's free-text request.
        Generates a fresh trace id per call. Returns ``self`` for chaining.

        Also switches ``self.model`` to the per-feature model override for
        ``request_type``, if the course or org configured one.
        """
        import uuid
        self.request_user = user
        self.request_type = request_type
        self.request_instructions = instructions or ''
        self._trace_id = str(uuid.uuid4())
        self.model = self.model_for_feature(request_type) if request_type else self.base_model
        return self

    # Request types that don't match their governing feature key 1:1.
    REQUEST_TYPE_TO_FEATURE = {
        'file_suggestions': 'suggested_comments',
    }

    def model_for_feature(self, feature_key: Optional[str]) -> str:
        """Resolve the model to use for *feature_key*.

        Precedence (mirrors ``_resolve_feature_toggle``):
        1. Course ``ai_feature_models[key]`` if set
        2. Org ``ai_feature_models[key]`` if the effective config is the org's
        3. The base model (course/org ``ai_model`` or provider default)
        """
        if not feature_key:
            return self.base_model
        feature_key = self.REQUEST_TYPE_TO_FEATURE.get(feature_key, feature_key)
        course_models = getattr(self.course, 'ai_feature_models', None) or {}
        override = course_models.get(feature_key)
        if override:
            return override
        if self._config_from_org and self.course.organization:
            org_models = getattr(self.course.organization, 'ai_feature_models', None) or {}
            override = org_models.get(feature_key)
            if override:
                return override
        return self.base_model

    def get_feature_models(self) -> dict[str, str]:
        """Return the resolved model for every registered feature."""
        from core.ai_features.registry import ai_feature_registry
        return {
            entry.key: self.model_for_feature(entry.key)
            for entry in ai_feature_registry.all()
        }

    def _build_portkey_metadata(self) -> dict[str, str]:
        """Build the Portkey metadata dict from the current request context.

        Portkey metadata values must be strings; ``_user`` is Portkey's
        reserved key for per-user analytics. Free text is truncated to keep
        within Portkey's per-value size limits.
        """
        md: dict[str, str] = {}
        if self.request_user is not None:
            md['_user'] = self.request_user.email or str(self.request_user.id)
        if self.request_type:
            md['feature'] = self.request_type
        if self.course is not None:
            md['course'] = str(self.course.id)
        if self.assignment is not None:
            md['assignment'] = str(self.assignment.id)
        if self.request_instructions:
            md['instructions'] = self.request_instructions[:200]
        return md

    @staticmethod
    def _org_allows_course(org, course: Course) -> bool:
        """Check if the org's course policy allows this course to use the org AI key."""
        if org.ai_course_policy == 'all':
            return True
        if org.ai_course_policy == 'selected':
            return org.ai_enabled_courses.filter(pk=course.pk).exists()
        return False

    def _get_default_model_for(self, provider: str) -> str:
        """Get default model for a given provider string."""
        models = AI_MODELS.get(provider, [])
        for model_id, _, is_default in models:
            if is_default:
                return model_id
        return 'default'
        
    def _get_default_model(self) -> str:
        """Get default model for the configured provider."""
        return self._get_default_model_for(self.provider)

    # ------------------------------------------------------------------
    # Prompt resolution & A/B experiment support
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_prompt(prompt_type: str) -> tuple[str, int | None]:
        """Resolve the active prompt text for the given prompt_type.

        Returns ``(prompt_text, variant_id)``. Falls back to the default
        template from the prompt registry when no active DB variant exists.

        Results are cached in Django's cache framework for 60 s.
        """
        from django.core.cache import cache as django_cache
        from core.models import SystemPromptVariant
        from core.prompts.registry import prompt_registry

        cache_key = f'active_prompt:{prompt_type}'
        cached = django_cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        try:
            variant = SystemPromptVariant.objects.get(
                prompt_type=prompt_type, status='active',
            )
            result = (variant.text, variant.id)
        except SystemPromptVariant.DoesNotExist:
            fallback = prompt_registry.get_default_template(prompt_type)
            result = (fallback, None)

        django_cache.set(cache_key, result, timeout=60)
        return result

    @staticmethod
    def check_experiment(prompt_type: str) -> 'PromptExperiment | None':
        """Return a running experiment for *prompt_type* if the random roll hits.

        The experiment object (or ``None``) is cached for 30 s so we don't
        query the DB on every request.  The *sample_rate* roll is **not**
        cached — each request has an independent chance of triggering A/B.
        """
        from django.core.cache import cache as django_cache
        from core.models import PromptExperiment

        cache_key = f'running_experiment:{prompt_type}'
        sentinel = '__none__'
        cached = django_cache.get(cache_key, sentinel)

        if cached is sentinel:
            try:
                experiment = PromptExperiment.objects.select_related(
                    'variant_a', 'variant_b',
                ).get(prompt_type=prompt_type, status='running')
            except PromptExperiment.DoesNotExist:
                experiment = None
            # Cache for 30 s (even None, to avoid repeated misses)
            django_cache.set(cache_key, experiment, timeout=30)
        else:
            experiment = cached

        if experiment is None:
            return None

        # Independent random roll against sample_rate
        if random.random() < experiment.sample_rate:
            return experiment
        return None

    async def generate_ab_comment(
        self,
        context: 'GenerationContext',
        experiment: 'PromptExperiment',
    ) -> tuple['GenerationResult', 'GenerationResult', int]:
        """Generate two outputs using both experiment variants concurrently.

        Returns ``(result_a, result_b, experiment_id)``.
        """
        text_a = experiment.variant_a.text
        text_b = experiment.variant_b.text

        # Build system + user prompts for both variants
        system_a = self._format_system_prompt(text_a, context)
        system_b = self._format_system_prompt(text_b, context)

        _template = (
            self.assignment.ai_system_prompt
            if self.assignment and self.assignment.ai_system_prompt
            else self.resolve_prompt('comment_generation')[0]
        )
        user_prompt_a = self.build_user_prompt(context, text_a)
        user_prompt_b = self.build_user_prompt(context, text_b)

        async def _call(system: str, user: str) -> GenerationResult:
            try:
                text, inp, out, tot, cached = await self._dispatch_provider(system, user)
                return GenerationResult(
                    text=text, success=True,
                    input_tokens=inp, output_tokens=out,
                    total_tokens=tot, cached_tokens=cached,
                )
            except Exception as e:
                return GenerationResult(text='', success=False, error=self._parse_error(e))

        result_a, result_b = await asyncio.gather(
            _call(system_a, user_prompt_a),
            _call(system_b, user_prompt_b),
        )
        return result_a, result_b, experiment.id

    def _format_system_prompt(self, template: str, context: 'GenerationContext') -> str:
        """Format a prompt template with context values, with fallback on error."""
        try:
            formatted = template.format(
                assignment_name=context.assignment_name,
                file_name=context.file_name,
                file_content=context.file_content,
                selected_content=context.selected_content,
                rubric_context=context.rubric_context,
                grader_draft=context.grader_draft,
                all_files=context.all_files_content,
            )
            return "\n\n".join([formatted, self.GLOBAL_SYSTEM_PROMPT])
        except Exception as e:
            logger.warning(f"Failed to format prompt template: {e}")
            return "\n\n".join([template, self.GLOBAL_SYSTEM_PROMPT])

    async def _dispatch_provider(self, system_prompt: str, user_prompt: str,
                                 images: 'Sequence[ImageAttachment]' = ()) -> tuple[str, int, int, int, int]:
        """Route to the configured provider. Returns (text, input_tokens, output_tokens, total_tokens, cached_tokens).

        ``images`` ride on the user turn as vision input (system messages can't carry them)."""
        if self.provider == 'gemini':
            return await self._call_gemini(system_prompt, user_prompt, images)
        elif self.provider == 'openai':
            return await self._call_openai(system_prompt, user_prompt, images)
        elif self.provider == 'ollama':
            return await self._call_ollama(system_prompt, user_prompt, images)
        elif self.provider in ('portkey', 'custom'):
            return await self._call_portkey(system_prompt, user_prompt, images)
        else:
            raise ValueError(f"Unknown AI provider: {self.provider}")

    async def _generate(self, system_prompt: str, user_prompt: str, label: str = 'generation',
                        images: 'Sequence[ImageAttachment]' = ()) -> GenerationResult:
        """Dispatch to the configured provider and wrap the result.

        Handles provider routing, token bookkeeping, and error translation
        so that callers only need to build prompts and consume a
        ``GenerationResult``.
        """
        try:
            text, input_tokens, output_tokens, total_tokens, cached_tokens = await self._dispatch_provider(
                system_prompt, user_prompt, images)
            return GenerationResult(
                text=text,
                success=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cached_tokens=cached_tokens,
            )
        except Exception as e:
            error_msg = self._parse_error(e)
            logger.error(f"AI {label} failed: {e}", exc_info=True)
            return GenerationResult(text="", success=False, error=error_msg)

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        """Strip wrapping markdown code fences from AI output."""
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.split('\n')
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        return stripped

    @staticmethod
    def _parse_json_response(text: str) -> str:
        """Strip markdown fences and validate JSON. Returns the cleaned text.

        Raises ``json.JSONDecodeError`` if the result isn't valid JSON.
        """
        import json as json_mod
        cleaned = AIService._strip_markdown_fences(text)
        json_mod.loads(cleaned)  # validate
        return cleaned

    @staticmethod
    def _format_file_section(f: dict, include_id: bool = True) -> str:
        """Format a file context dict into a prompt section.

        Handles notebooks (pre-formatted cells) vs code files (line-numbered
        + code-fenced).
        """
        if f.get('is_notebook'):
            label = f"### File: {f['name']}"
            if include_id:
                label += f" (ID: {f['id']})"
            return f"{label} [NOTEBOOK]\n{f['content']}"
        else:
            numbered = AIService._add_line_numbers(f['content'])
            label = f"### File: {f['name']}"
            if include_id:
                label += f" (ID: {f['id']})"
            return f"{label}\n```{f['extension'].lstrip('.')}\n{numbered}\n```"

    async def _resolve_and_format_prompt(
        self,
        prompt_type: str,
        format_kwargs: dict,
        variant_id_override: int | None = None,
    ) -> tuple[str, int | None]:
        """Resolve a prompt from the DB (or class constant fallback) and format it.

        When *variant_id_override* is provided, fetch that specific variant
        instead of the active one.  This is used during A/B experiments to
        generate output with each experiment variant independently.

        Returns ``(formatted_text, variant_id)``.
        """
        from asgiref.sync import sync_to_async

        prompt_text: str = ''
        variant_id: int | None = None

        if variant_id_override is not None:
            from core.models import SystemPromptVariant
            try:
                variant = await sync_to_async(
                    SystemPromptVariant.objects.get
                )(pk=variant_id_override)
                prompt_text = variant.text
                variant_id = variant.id
            except SystemPromptVariant.DoesNotExist:
                pass
        else:
            try:
                prompt_text, variant_id = await sync_to_async(self.resolve_prompt)(prompt_type)
            except Exception:
                # DB may be unavailable (e.g. in unit tests without django_db).
                pass

        if not prompt_text:
            from core.prompts.registry import prompt_registry
            prompt_text = prompt_registry.get_default_template(prompt_type)
        return prompt_text.format(**format_kwargs), variant_id

    @property
    def is_configured(self) -> bool:
        """Check if AI is properly configured for this course."""
        if self.provider in ('ollama', 'portkey'):
            # Ollama and Portkey work with a blank base_url — _call_ollama and
            # _call_portkey fall back to their default URLs — so the provider
            # alone is enough (matches test_connection and the settings
            # serializers' _provider_is_configured).
            return bool(self.provider)
        return bool(self.provider and self.api_key)

    @property
    def is_globally_disabled(self) -> bool:
        """Check if AI is globally disabled (course or org-level)."""
        if self.course.ai_use_own_settings:
            return bool(self.course.ai_disabled)
        org = self.course.organization
        if org and self._org_allows_course(org, self.course):
            return bool(org.ai_disabled)
        return bool(self.course.ai_disabled)

    def is_feature_enabled(self, feature_key: str) -> bool:
        """Check if a specific AI feature is enabled for this course.

        Resolution order:
        1. ``ai_disabled`` master switch → always ``False``
        2. Course ``ai_feature_config[key]`` if present → use it (full autonomy)
        3. Org ``ai_feature_config[key]`` if present → use it as default
        4. Registry default (``True``)

        Additionally, a feature is forced on if any feature that requires it
        is itself enabled (dependency enforcement).
        """
        if self.is_globally_disabled:
            return False
        if not self.is_configured:
            return False

        from core.ai_features.registry import ai_feature_registry

        resolved = self._resolve_feature_toggle(feature_key, ai_feature_registry)

        # If toggled off, check whether any dependent feature forces it on.
        if not resolved:
            for dep_key in ai_feature_registry.dependents_of(feature_key):
                if self._resolve_feature_toggle(dep_key, ai_feature_registry):
                    return True

        return resolved

    def _resolve_feature_toggle(self, feature_key: str, registry) -> bool:
        """Raw toggle resolution without dependency enforcement."""
        course_config = getattr(self.course, 'ai_feature_config', None) or {}
        if feature_key in course_config:
            return bool(course_config[feature_key])

        org = self.course.organization
        if org:
            org_config = getattr(org, 'ai_feature_config', None) or {}
            if feature_key in org_config:
                return bool(org_config[feature_key])

        return registry.get_default(feature_key)

    def get_feature_status(self) -> dict[str, bool]:
        """Return resolved enabled/disabled status for all registered features."""
        from core.ai_features.registry import ai_feature_registry
        return {
            entry.key: self.is_feature_enabled(entry.key)
            for entry in ai_feature_registry.all()
        }

    # ------------------------------------------------------------------
    # Cost estimation & usage recording
    # ------------------------------------------------------------------

    # TTL (in seconds) for Gemini explicit context caches
    GEMINI_CACHE_TTL = 900  # 15 minutes

    # Provider-specific discount on cached input tokens (fraction of full input rate)
    # Gemini: cached tokens billed at 25% of full input price (75% discount)
    # OpenAI: cached tokens billed at 50% of full input price (50% discount)
    CACHED_TOKEN_RATE: dict[str, float] = {
        'gemini': 0.25,
        'openai': 0.50,
    }

    # Rates: (input $/1M tokens, output $/1M tokens)
    TOKEN_RATES: dict[str, tuple[float, float]] = {
        # Gemini
        'gemini-3-pro-preview': (2.00, 12.00),
        'gemini-3-flash-preview': (0.50, 3),
        'gemini-2.5-flash': (0.15, 0.60),
        'gemini-2.5-pro': (1.25, 10.00),
        'gemini-2.0-flash': (0.10, 0.40),
        'gemini-1.5-flash': (0.075, 0.30),
        'gemini-1.5-pro': (1.25, 5.00),
        # OpenAI
        'gpt-4o': (2.50, 10.00),
        'gpt-4o-mini': (0.15, 0.60),
        'gpt-4.1': (2.00, 8.00),
        'gpt-4.1-mini': (0.40, 1.60),
        'gpt-4.1-nano': (0.10, 0.40),
        'o3-mini': (1.10, 4.40),
    }

    @staticmethod
    def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int,
                      custom_rates: dict | None = None,
                      cached_tokens: int = 0) -> float:
        """
        Estimate the cost of an AI API call in USD.

        Rate lookup order:
          1. ``custom_rates`` dict  (org/course overrides merged by caller)
          2. ``TOKEN_RATES``        (hardcoded defaults)
          3. Falls back to 0.0 for unknown models / self-hosted providers.

        ``custom_rates`` format: ``{"model-name": {"input": 0.15, "output": 0.60}, ...}``

        When ``cached_tokens`` > 0, the cached portion of input tokens is billed
        at the provider's discounted rate (see ``CACHED_TOKEN_RATE``).
        """
        # Gateway providers (e.g. Portkey) report slash-prefixed ids like
        # "provider-slug/gemini-3-flash-preview"; fall back to the basename
        # so bare-named rates still match. Exact match always wins.
        candidates = [model]
        if '/' in model:
            candidates.append(model.rsplit('/', 1)[-1])
        rates = None
        if custom_rates:
            for candidate in candidates:
                r = custom_rates.get(candidate)
                if isinstance(r, dict) and 'input' in r and 'output' in r:
                    rates = (float(r['input']), float(r['output']))
                    break
        if not rates:
            for candidate in candidates:
                rates = AIService.TOKEN_RATES.get(candidate)
                if rates:
                    break
        if not rates:
            return 0.0
        # Split input tokens into non-cached and cached portions
        effective_cached = min(cached_tokens, input_tokens)
        non_cached_input = input_tokens - effective_cached
        cache_discount = AIService.CACHED_TOKEN_RATE.get(provider, 1.0)
        input_cost = (non_cached_input / 1_000_000) * rates[0]
        cached_cost = (effective_cached / 1_000_000) * rates[0] * cache_discount
        output_cost = (output_tokens / 1_000_000) * rates[1]
        return float(Decimal(str(input_cost + cached_cost + output_cost)).quantize(Decimal('0.000001')))

    @staticmethod
    def merged_token_rates(organization=None, course=None) -> dict | None:
        """Merge custom token rates: course overrides org. The org defaults to
        course.organization when not given explicitly (course-less scopes like
        org connection tests pass it directly)."""
        rates: dict = {}
        org = organization or (course.organization if course else None)
        if org and org.ai_token_rates:
            rates.update(org.ai_token_rates)
        if course and course.ai_token_rates:
            rates.update(course.ai_token_rates)
        return rates or None

    def record_usage(
        self,
        result: 'GenerationResult',
        user: User,
        request_type: str = 'comment_generation',
        experiment: 'PromptExperiment | None' = None,
        organization=None,
    ) -> None:
        """
        Persist an AIUsageRecord for the generation that just completed.

        Parameters
        ----------
        result : GenerationResult
            The result returned by ``generate_comment`` or ``generate_test_script``.
        user : User
            The Django user who triggered the generation.
        request_type : str
            One of ``'comment_generation'``, ``'test_generation'``, ``'code_review'``, etc.
        experiment : PromptExperiment | None
            The active A/B experiment, if any.
        """
        try:
            from core.models import AIUsageRecord, SystemPromptVariant

            prompt_variant = None
            if result.variant_id is not None:
                prompt_variant = SystemPromptVariant.objects.filter(pk=result.variant_id).first()

            AIUsageRecord.objects.create(
                # Course-less instances (for_config, org-level tests) pass an
                # explicit organization instead.
                organization=self.course.organization if self.course else organization,
                course=self.course,
                assignment=self.assignment,
                user=user,
                provider=self.provider or '',
                model=self.model or '',
                request_type=request_type,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
                cached_tokens=result.cached_tokens,
                estimated_cost=self.estimate_cost(
                    self.provider or '', self.model or '',
                    result.input_tokens, result.output_tokens,
                    custom_rates=AIService.merged_token_rates(
                        organization=organization, course=self.course),
                    cached_tokens=result.cached_tokens,
                ),
                status='success' if result.success else 'error',
                error_message=result.error if not result.success else None,
                prompt_variant=prompt_variant,
                experiment=experiment,
            )
        except Exception as e:
            logger.warning(f"Failed to record AI usage: {e}")

    def get_system_prompt(self, context: GenerationContext) -> tuple[str, int | None, bool]:
        """Build the system prompt with context.

        Returns ``(formatted_prompt, variant_id, is_custom_context)``.
        ``is_custom_context`` is True when the assignment has a custom ``ai_system_prompt``.
        """
        is_custom = bool(self.assignment and self.assignment.ai_system_prompt)

        if is_custom:
            template = self.assignment.ai_system_prompt  # type: ignore[union-attr]
            variant_id: int | None = None
        else:
            template, variant_id = self.resolve_prompt('comment_generation')
            
        try:
            formatted = "\n\n".join([
                template.format(
                assignment_name=context.assignment_name,
                file_name=context.file_name,
                file_content=context.file_content,
                selected_content=context.selected_content,
                rubric_context=context.rubric_context,
                grader_draft=context.grader_draft,
                all_files=context.all_files_content,
            ),
            self.GLOBAL_SYSTEM_PROMPT
        ])
            return formatted, variant_id, is_custom
        except Exception as e:
            # Fallback if the user's template contains invalid placeholders or syntax
            logger.warning(f"Failed to format system prompt template: {e}")
            return "\n\n".join([
                template,
                self.GLOBAL_SYSTEM_PROMPT
            ]), variant_id, is_custom
    
    def build_user_prompt(self, context: GenerationContext, system_prompt_template: str = "") -> str:
        """
        Build the user prompt for generation.
        Checks system_prompt_template to avoid duplicating content already in system prompt.
        """
        parts = []
        
        # Selected code (only if not in system prompt)
        if '{selected_content}' not in system_prompt_template:
            parts.append(f"**Selected Code:**\n```\n{context.selected_content}\n```")
        
        # Grader's draft (only if not in system prompt)
        if '{grader_draft}' not in system_prompt_template:
            if context.grader_draft:
                parts.append(f"**Grader's Draft Comment:**\n{context.grader_draft}")
                parts.append("\nPlease improve and expand on this draft comment.")
            else:
                parts.append("\nPlease write a feedback comment for this code.")
        else:
            # If draft is in system prompt, we still need a "trigger" instruction in user prompt?
            # Or assume system prompt has instructions.
            # We'll add a generic instruction if the draft logic is handled in system prompt.
            if not parts: # If nothing else added yet
                 parts.append("\nPlease generate the feedback comment.")

        # Rubric context (only if not in system prompt)
        if '{rubric_context}' not in system_prompt_template and context.rubric_context:
            parts.append(f"\n**Rubric Context:**\n{context.rubric_context}")
            
        return "\n\n".join(parts)
    
    async def generate_comment(
        self,
        context: GenerationContext,
    ) -> GenerationResult:
        """
        Generate a comment using the configured AI provider.
        
        Args:
            context: GenerationContext with all relevant information
            
        Returns:
            GenerationResult with generated text or error
        """
        if not self.is_configured:
            return GenerationResult(
                text="",
                success=False,
                error="AI is not configured for this course"
            )
        
        try:
            # Resolve prompt (DB-backed with fallback to class constant)
            from asgiref.sync import sync_to_async
            system_prompt, variant_id, is_custom = await sync_to_async(self.get_system_prompt)(context)
            system_prompt_template = self.assignment.ai_system_prompt if self.assignment and self.assignment.ai_system_prompt else self.resolve_prompt('comment_generation')[0]

            user_prompt = self.build_user_prompt(context, system_prompt_template)

            # Call the appropriate provider
            logger.debug(f"Calling {self.provider} ({self.model}) for comment generation")
            text, input_tokens, output_tokens, total_tokens, cached_tokens = await self._dispatch_provider(system_prompt, user_prompt)

            return GenerationResult(
                text=text,
                success=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cached_tokens=cached_tokens,
            )

        except Exception as e:
            error_msg = self._parse_error(e)
            logger.error(f"AI generation failed: {e}", exc_info=True)
            return GenerationResult(
                text="",
                success=False,
                error=error_msg
            )

    LANGUAGE_EXAMPLES: dict[str, str] = {
        "python": """@test("Test Name", points=10, description="Test Description")
def test_name():
    assert func() == expected

@test("Test Partial", points=10, description="Partial credit example")
def test_partial():
    return 10 if func() == expected else 5

@test("Test Explanation", points=10, description="Score + explanation")
def test_explanation():
    score = 10 if func() == expected else 0
    return score, f"Computed score: {score}"
""",
        "java": """@Test(name="Test Name", points=10, description="Test Description")
public double testName() {
    assertEquals(expected, func());
    return 10.0;
}

@Test(name="Test Partial", points=10, description="Partial credit example")
public double testPartial() {
    return func() == expected ? 10.0 : 5.0;
}

@Test(name="Test Explanation", points=10, description="Score + explanation")
public Object[] testExplanation() {
    double score = (func() == expected) ? 10.0 : 0.0;
    return new Object[] { score, "This test returns score and explanation" };
}""",
        "cpp": """TEST_DESC(TestName, 10, "Test Description") {
    assertTrue(func() == expected, "Expected func() to equal expected");
}

TEST_DESC(TestPartial, 10, "Partial credit example") {
    double score = (func() == expected) ? 10.0 : 5.0;
    return score;
}

TEST_DESC_TIMEOUT(TestExplanation, 10, "Score + explanation", 30) {
    double score = (func() == expected) ? 10.0 : 0.0;
    return return_score(score, "This test returns score and explanation");
}""",
        "c": """TEST_DESC(TestName, 10, "Test Description") {
    assertTrue(func() == expected, "Expected func() to equal expected");
}

TEST_DESC(TestPartial, 10, "Partial credit example") {
    double score = (func() == expected) ? 10.0 : 5.0;
    return score;
}

TEST_DESC_TIMEOUT(TestExplanation, 10, "Score + explanation", 30) {
    double score = (func() == expected) ? 10.0 : 0.0;
    return return_score(score, "This test returns score and explanation");
}""",
        "javascript": """test("Test Name", 10, "Test Description", function() {
    if (func() !== expected) {
        throw new Error("Expected " + expected);
    }
});

test("Test Partial", 10, "Partial credit example", function() {
    return func() === expected ? 10 : 5;
});

test("Test Explanation", 10, "Score + explanation", function() {
    const score = func() === expected ? 10 : 0;
    return [score, "This test returns score and explanation"];
}, 30);
""",
        "node": """test("Test Name", 10, "Test Description", function() {
    if (func() !== expected) {
        throw new Error("Expected " + expected);
    }
});

test("Test Partial", 10, "Partial credit example", function() {
    return func() === expected ? 10 : 5;
});

test("Test Explanation", 10, "Score + explanation", function() {
    const score = func() === expected ? 10 : 0;
    return [score, "This test returns score and explanation"];
}, 30);
""",
        "php": """Tester::test("Test Name", 10.0, "Test Description", function() {
    if (func() !== expected) {
        throw new Exception("Expected " . expected);
    }
});

Tester::test("Test Partial", 10.0, "Partial credit example", function() {
    return (func() === expected) ? 10.0 : 5.0;
});

Tester::test("Test Explanation", 10.0, "Score + explanation", function() {
    $score = (func() === expected) ? 10.0 : 0.0;
    return [$score, "This test returns score and explanation"];
}, 30);
""",
        "r": """run_test("Test Name", 10, "Test Description", function() {
    if (func() != expected) {
        stop(paste("Expected", expected))
    }
})

run_test("Test Partial", 10, "Partial credit example", function() {
    return(ifelse(func() == expected, 10, 5))
})

run_test("Test Explanation", 10, "Score + explanation", function() {
    score <- ifelse(func() == expected, 10, 0)
    return(list(score, "This test returns score and explanation"))
}, 30)
""",
        "ruby": """run_test("Test Name", 10, "Test Description") do
    result = func()
    raise "Expected #{expected}" unless result == expected
end

run_test("Test Partial", 10, "Partial credit example") do
    (func() == expected) ? 10 : 5
end

run_test("Test Explanation", 10, "Score + explanation", 30) do
    score = (func() == expected) ? 10 : 0
    [score, "This test returns score and explanation"]
end"""
    }

    def _extract_java_test_methods(self, text: str) -> list[str]:
        """Extract @Test-annotated Java methods, preserving method bodies."""
        methods: list[str] = []

        for match in re.finditer(r'@Test\s*\([^)]*\)', text, flags=re.DOTALL):
            start = match.start()
            body_start = text.find('{', match.end())
            if body_start == -1:
                continue

            depth = 0
            end = -1
            for i in range(body_start, len(text)):
                ch = text[i]
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i
                        break

            if end != -1:
                methods.append(text[start:end + 1].strip())

        return methods

    def _normalize_generated_test_script(self, text: str, language: str) -> str:
        """Normalize model output to match runtime harness expectations."""
        normalized = self._strip_markdown_fences(text)

        # Java script harness (TestRunner.java) expects method bodies only.
        if language == 'java':
            methods = self._extract_java_test_methods(normalized)
            if methods:
                return "\n\n".join(methods)

            # Fallback cleanup if extraction fails.
            cleaned_lines = []
            for line in normalized.splitlines():
                stripped = line.strip()
                if stripped.startswith('package '):
                    continue
                if stripped.startswith('import '):
                    continue
                cleaned_lines.append(line)
            return "\n".join(cleaned_lines).strip()

        return normalized

    async def generate_test_script(
        self,
        context_file_content: str,
        context_filename: str,
        target_filename: str,
        target_code: str = "",
        language: str = "python",
        rubric_text: str = ""
    ) -> GenerationResult:
        """
        Generate a test script using the configured AI provider.
        """
        if not self.is_configured:
            return GenerationResult(
                text="",
                success=False,
                error="AI is not configured for this course"
            )

        try:
            # Select appropriate example or fallback to python
            lang_key = language.lower()
            if lang_key not in self.LANGUAGE_EXAMPLES:
                # Try to map common aliases
                if lang_key in ['py']:
                    lang_key = 'python'
                elif lang_key in ['js']:
                    lang_key = 'javascript'
                elif lang_key in ['nodejs']:
                    lang_key = 'node'
                elif lang_key in ['c++']:
                    lang_key = 'cpp'

            example = self.LANGUAGE_EXAMPLES.get(lang_key, self.LANGUAGE_EXAMPLES['python'])

            # Ensure target_code is never None
            safe_target_code = target_code if target_code else "(No content available)"

            # Format rubric section
            rubric_section = ""
            if rubric_text:
                rubric_section = f"Rubric Criterion (Test Goal):\n{rubric_text}\n"

            system_prompt, _variant_id = await self._resolve_and_format_prompt(
                'test_generation',
                dict(
                    context_filename=context_filename,
                    context_content=context_file_content,
                    target_filename=target_filename,
                    target_code=safe_target_code,
                    language=language,
                    language_example=example,
                ),
            )

            if rubric_section:
                system_prompt += f"\n\n{rubric_section}"

            user_prompt = f"Generate a {language} test script for {target_filename}."

            result = await self._generate(system_prompt, user_prompt, label='test generation')
            if result.success:
                result.text = self._normalize_generated_test_script(result.text, lang_key)
            return result

        except Exception as e:
            error_msg = self._parse_error(e)
            logger.error(f"AI test generation failed: {e}", exc_info=True)
            return GenerationResult(text="", success=False, error=error_msg)

    @property
    def api_key_looks_undecrypted(self) -> bool:
        """True when the resolved API key is still Fernet ciphertext.

        EncryptedCharField silently returns the raw ciphertext (``gAAAA…``) when
        this process's FIELD_ENCRYPTION_KEY doesn't match the one that saved the
        value (classically: a worker container missing the env var) — the
        provider then rejects it as an invalid API key.
        """
        return bool(self.api_key) and str(self.api_key).startswith('gAAAA')

    def _parse_error(self, e: Exception) -> str:
        """Parse exception into user-friendly error message."""
        error_str = str(e).lower()

        # API key errors
        if 'api_key' in error_str or 'api key' in error_str or 'invalid' in error_str and 'key' in error_str:
            if self.api_key_looks_undecrypted:
                return ("The stored API key could not be decrypted in this process — its "
                        "FIELD_ENCRYPTION_KEY does not match the one the key was saved with. "
                        "Check that the web and worker containers share the same "
                        "FIELD_ENCRYPTION_KEY environment variable.")
            return "Invalid API key. Please check your AI configuration in Course Settings."
        
        # Rate limiting
        if 'rate' in error_str and 'limit' in error_str:
            return "Rate limit exceeded. Please wait a moment and try again."
        
        if 'quota' in error_str or '429' in error_str:
            return "API quota exceeded. Please check your API usage limits."
        
        # Authentication errors
        if 'unauthorized' in error_str or '401' in error_str:
            return "Authentication failed. Please verify your API key."
        
        # Connection errors
        if 'connection' in error_str or 'timeout' in error_str:
            return "Failed to connect to AI service. Please try again later."
        
        # Model not found
        if 'model' in error_str and ('not found' in error_str or 'invalid' in error_str):
            return f"Model '{self.model}' not found. Please check your AI configuration."
        
        # Content policy
        if 'safety' in error_str or 'blocked' in error_str or 'content' in error_str and 'policy' in error_str:
            return "Content was blocked by AI safety filters. Please try a different selection."
        
        # Generic fallback with shortened message
        short_msg = str(e)[:200]
        if len(str(e)) > 200:
            short_msg += "..."
        return f"AI generation failed: {short_msg}"
    
    async def _get_or_create_gemini_cache(self, system_prompt: str) -> str | None:
        """
        Get or create a Gemini explicit context cache for the system prompt.
        Returns the cache name if successful, None otherwise (falls back to uncached).
        """
        from django.core.cache import cache as django_cache
        from google import genai
        from google.genai import types

        prompt_hash = hashlib.sha256(
            f"{self.model}\n{system_prompt}".encode()
        ).hexdigest()[:16]
        cache_key = f"gemini_ctx:{prompt_hash}"

        # Check Django cache for an existing Gemini cache reference
        cache_name = django_cache.get(cache_key)
        if cache_name:
            logger.debug(f"Gemini cache hit: {cache_name} for prompt hash {prompt_hash}")
            return cache_name

        # Create a new explicit cached content on Gemini's servers
        try:
            client = genai.Client(api_key=self.api_key)
            cached = await client.aio.caches.create(
                model=self.model,
                config=types.CreateCachedContentConfig(
                    system_instruction=system_prompt,
                    ttl=f"{self.GEMINI_CACHE_TTL}s",
                ),
            )
            # Store reference with a slightly shorter TTL to avoid stale refs
            django_cache.set(cache_key, cached.name, timeout=self.GEMINI_CACHE_TTL - 30)
            logger.debug(f"Created Gemini cache: {cached.name} for prompt hash {prompt_hash}")
            return cached.name
        except Exception as e:
            # Explicit caching can fail (e.g., prompt too short, unsupported model).
            # Fall back to implicit caching (Gemini 2.5+ does this automatically).
            logger.info(f"Gemini explicit cache creation skipped ({type(e).__name__}): {e}")
            return None

    @staticmethod
    def _openai_user_content(user_prompt: str, images: 'Sequence[ImageAttachment]'):
        """OpenAI chat 'content': a plain string, or text + image_url parts (data URIs are
        accepted as-is) when images are attached. Shared by OpenAI and Portkey/custom."""
        if not images:
            return user_prompt
        return [{"type": "text", "text": user_prompt}] + [
            {"type": "image_url", "image_url": {"url": f"data:{img.mime};base64,{img.base64_data}"}}
            for img in images
        ]

    async def _call_gemini(self, system_prompt: str, user_prompt: str,
                           images: 'Sequence[ImageAttachment]' = ()) -> tuple[str, int, int, int, int]:
        """Call Google Gemini API. Returns (text, input_tokens, output_tokens, total_tokens, cached_tokens)."""
        import base64
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self.api_key)

        # Try explicit context caching for the system prompt
        cache_name = await self._get_or_create_gemini_cache(system_prompt)

        if cache_name:
            config = types.GenerateContentConfig(
                cached_content=cache_name,
            )
        else:
            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
            )

        contents: Any = user_prompt
        if images:
            contents = [types.Part.from_text(text=user_prompt)] + [
                types.Part.from_bytes(data=base64.b64decode(img.base64_data), mime_type=img.mime)
                for img in images
            ]
        response = await client.aio.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )
        input_tokens = getattr(response.usage_metadata, 'prompt_token_count', 0) or 0
        output_tokens = getattr(response.usage_metadata, 'candidates_token_count', 0) or 0
        total_tokens = getattr(response.usage_metadata, 'total_token_count', 0) or (input_tokens + output_tokens)
        cached_tokens = getattr(response.usage_metadata, 'cached_content_token_count', 0) or 0
        self._last_provider_meta = {'model': getattr(response, 'model_version', None)}
        return response.text or "", input_tokens, output_tokens, total_tokens, cached_tokens
    
    async def _call_openai(self, system_prompt: str, user_prompt: str,
                           images: 'Sequence[ImageAttachment]' = ()) -> tuple[str, int, int, int, int]:
        """Call OpenAI API. Returns (text, input_tokens, output_tokens, total_tokens, cached_tokens)."""
        from openai import AsyncOpenAI
        
        client = AsyncOpenAI(api_key=self.api_key)
        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": self._openai_user_content(user_prompt, images)},
            ],
        )
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else (input_tokens + output_tokens)
        # OpenAI automatically caches prompts >1024 tokens; extract cached count
        cached_tokens = 0
        if usage and hasattr(usage, 'prompt_tokens_details') and usage.prompt_tokens_details:
            cached_tokens = getattr(usage.prompt_tokens_details, 'cached_tokens', 0) or 0
        self._last_provider_meta = {'model': getattr(response, 'model', None)}
        return response.choices[0].message.content or "", input_tokens, output_tokens, total_tokens, cached_tokens
    
    async def _call_ollama(self, system_prompt: str, user_prompt: str,
                           images: 'Sequence[ImageAttachment]' = ()) -> tuple[str, int, int, int, int]:
        """Call Ollama API (self-hosted). Returns (text, input_tokens, output_tokens, total_tokens, cached_tokens).

        Images go in the bare-base64 ``images`` list; only a vision model (llava, gemma3,
        qwen-vl, ...) will actually look at them — a text-only model silently ignores them."""
        import httpx
        
        base_url = self.base_url or DEFAULT_OLLAMA_URL
        payload: dict[str, Any] = {
            "model": self.model,
            "system": system_prompt,
            "prompt": user_prompt,
            "stream": False,
        }
        if images:
            payload["images"] = [img.base64_data for img in images]
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/api/generate",
                json=payload,
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()
            input_tokens = data.get('prompt_eval_count', 0) or 0
            output_tokens = data.get('eval_count', 0) or 0
            self._last_provider_meta = {'model': data.get('model')}
            return data["response"], input_tokens, output_tokens, input_tokens + output_tokens, 0
    
    async def _call_portkey(self, system_prompt: str, user_prompt: str,
                            images: 'Sequence[ImageAttachment]' = ()) -> tuple[str, int, int, int, int]:
        """Call Portkey AI gateway (self-hosted or cloud). Returns (text, input_tokens, output_tokens, total_tokens, cached_tokens).
        
        Portkey is an AI Gateway that proxies requests to underlying providers.
        When self-hosted, it typically only needs a base URL (API key is optional).
        Uses x-portkey-api-key header for gateway auth, not Authorization.
        The endpoint is OpenAI-compatible: POST /v1/chat/completions.
        """
        import httpx
        import json

        base_url = (self.base_url or DEFAULT_PORTKEY_URL).rstrip('/')
        # Ensure the URL doesn't already end with /v1 when we append the path
        if base_url.endswith('/v1'):
            url = f"{base_url}/chat/completions"
        else:
            url = f"{base_url}/v1/chat/completions"

        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        # API key is optional for self-hosted Portkey gateways
        if self.api_key:
            headers["x-portkey-api-key"] = self.api_key

        # Attach request context as Portkey observability metadata. Only emitted
        # when a caller set the context (signalled by _trace_id), so un-wired
        # paths keep the original bare request. Guarded to the portkey provider
        # so the shared 'custom' OpenAI-compatible path stays vendor-neutral.
        if self.provider == 'portkey' and self._trace_id is not None:
            metadata = self._build_portkey_metadata()
            if metadata:
                headers["x-portkey-metadata"] = json.dumps(metadata)
                headers["x-portkey-trace-id"] = self._trace_id

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": self._openai_user_content(user_prompt, images)},
                    ],
                },
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()
            usage = data.get('usage', {})
            input_tokens = usage.get('prompt_tokens', 0) or 0
            output_tokens = usage.get('completion_tokens', 0) or 0
            total_tokens = usage.get('total_tokens', 0) or (input_tokens + output_tokens)
            self._last_provider_meta = {'model': data.get('model')}
            return data["choices"][0]["message"]["content"], input_tokens, output_tokens, total_tokens, 0

    # ------------------------------------------------------------------
    # AI Grading Assistance: Suggested Comments, Summary, Description
    # ------------------------------------------------------------------

    @staticmethod
    def _add_line_numbers(content: str) -> str:
        """Prepend 0-indexed line numbers to each line of code.

        This makes line references unambiguous for the AI model, which otherwise
        has to count lines itself and tends to use 1-based indexing.
        """
        lines = content.split('\n')
        return '\n'.join(f'{i}: {line}' for i, line in enumerate(lines))

    @staticmethod
    def _collect_submission_context(submission: Submission) -> dict:
        """Collect all relevant context from a submission for AI processing.

        Returns a dict with keys: files, test_results, rubric_context, assignment_description.
        """
        from core.models import SubmissionFile, SubmissionTest, RubricCategory

        MAX_TOTAL_CHARS = 200_000  # Total character budget across all files

        assignment = submission.assignment

        # Collect student files (non-hidden, non-test-resource assignment files + extra uploads)
        assignment_file_names = set(
            assignment.files.filter(hidden=False, is_test_resource=False)
            .values_list('name', flat=True)
        )
        hidden_file_names = set(
            assignment.files.filter(hidden=True).values_list('name', flat=True)
        )
        submission_files = list(SubmissionFile.objects.filter(submission=submission))

        # Sort: student files first so they get priority in the budget
        def _is_student_file(sf):
            return sf.name in assignment_file_names or sf.name not in hidden_file_names

        submission_files.sort(key=lambda sf: (not _is_student_file(sf), sf.name))

        files_context = []
        total_chars = 0
        for sf in submission_files:
            # Include all submission files — the AI needs full picture
            content = sf.data
            is_notebook = sf.name.endswith('.ipynb')

            if is_notebook:
                # Parse notebook and present as enumerated cells so the AI model
                # returns 0-based cell indices instead of raw JSON line numbers.
                content = _format_notebook_as_cells(content)
            elif sf.name.lower().endswith('.pdf'):
                # Extract PDF text rather than dumping the raw base64 data URI.
                content = extract_pdf_text(content) or f"(could not extract text from PDF '{sf.name}')"
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
            elif len(content) > 50000:
                content = content[:50000] + "\n... (truncated)"

            # Enforce total character budget
            remaining = MAX_TOTAL_CHARS - total_chars
            if remaining <= 0:
                files_context.append({
                    'id': sf.id,
                    'name': sf.name,
                    'extension': sf.extension,
                    'content': '(omitted — total size budget exceeded)',
                    'is_notebook': is_notebook,
                    'is_student_file': _is_student_file(sf),
                })
                continue
            if len(content) > remaining:
                content = content[:remaining] + "\n... (truncated — total size budget)"

            total_chars += len(content)

            files_context.append({
                'id': sf.id,
                'name': sf.name,
                'extension': sf.extension,
                'content': content,
                # Raw stored data (pre-extraction) so prompt variables can dedup a file
                # that also appears as an unchanged assignment file.
                'data': sf.data,
                'is_notebook': is_notebook,
                'is_student_file': _is_student_file(sf),
            })

        # Collect test results
        test_results = []
        for st in SubmissionTest.objects.filter(submission=submission).select_related('testCase'):
            test_results.append({
                'name': st.testCase.description if hasattr(st.testCase, 'description') else str(st.testCase),
                'passed': st.passed,
                'is_error': st.isError,
                'logs': (st.logs or '')[:2000],
                'score': str(st.score) if st.score is not None else None,
                'max_score': str(st.maxScore) if st.maxScore is not None else None,
            })

        # Collect rubric
        rubric_parts = []
        for category in RubricCategory.objects.filter(assignment=assignment).prefetch_related('rubricComments'):
            cat_text = f"### {category.name} ({category.pointLimit} pts)\n"
            for rc in category.rubricComments.all():
                cat_text += f"  - [ID:{rc.id}] {rc.name or rc.text[:60]} ({rc.pointDelta:+g} pts)\n"
                if rc.explanation:
                    cat_text += f"    Explanation: {rc.explanation[:200]}\n"
            rubric_parts.append(cat_text)

        # Build formatted sections
        test_section = ""
        if test_results:
            passed = sum(1 for t in test_results if t['passed'])
            total = len(test_results)
            test_section = f"Test Results: {passed}/{total} passed\n"
            for t in test_results:
                status = "✓ PASS" if t['passed'] else ("✗ ERROR" if t['is_error'] else "✗ FAIL")
                test_section += f"  {status}: {t['name']}"
                if t['score'] is not None:
                    test_section += f" ({t['score']}/{t['max_score']})"
                test_section += "\n"
                if not t['passed'] and t['logs']:
                    test_section += f"    Logs: {t['logs'][:500]}\n"

        desc = assignment.ai_description or ''
        desc_section = f"Assignment Description:\n{desc}" if desc else ""
        desc_section += f"Assignment Instructor Explaination:\n{assignment.explanation}" if assignment.explanation else ""

        return {
            'files': files_context,
            'test_results': test_section,
            'rubric_context': "\n".join(rubric_parts),
            'assignment_description': desc_section,
        }

    async def generate_suggested_comments(self, submission) -> list[GenerationResult]:
        """
        Generate AI-suggested comments for an entire submission.
        Returns a list of GenerationResults — one per batch call. The text of each
        result is a JSON array of suggestion objects.
        """
        from asgiref.sync import sync_to_async

        ctx = await sync_to_async(self._collect_submission_context)(submission)
        assignment = submission.assignment

        # Pass test_results="" for backward compat with DB-stored prompt variants
        # that still reference {test_results}. Actual test results go in user prompt
        # so the system prompt stays stable across submissions (enables caching).
        system_prompt, _variant_id = await self._resolve_and_format_prompt(
            'suggested_comments',
            dict(
                assignment_name=assignment.name,
                assignment_description=ctx['assignment_description'],
                rubric_context=ctx['rubric_context'] or "No rubric defined.",
                test_results="",
            ),
        )

        # Build the user prompt with all file contents
        test_results_section = ctx['test_results'] or "No test results available."
        file_sections = []
        has_notebooks = any(f.get('is_notebook') for f in ctx['files'])
        for f in ctx['files']:
            file_sections.append(self._format_file_section(f))

        notebook_note = ""
        if has_notebooks:
            notebook_note = (
                "\n\nIMPORTANT for notebook (.ipynb) files: start_line and end_line must be "
                "1-based CELL NUMBERS (matching the \"CELL N\" headers above), NOT line numbers. "
                "For example, to comment on the first cell (CELL 1) use start_line=1, end_line=1."
            )

        user_prompt = f"""Analyze the following student submission files and generate feedback comments.
Each line of code is prefixed with its 0-indexed line number (e.g. "0: ...", "1: ...").
Use these exact line numbers in your response.

{test_results_section}

{chr(10).join(file_sections)}

Respond with ONLY a JSON array of comment objects. Each object must have:
- "file_id": integer (the file ID from above)
- "start_line": integer (the 0-indexed line number shown at the start of each line)
- "end_line": integer (the 0-indexed line number shown at the start of each line)
- "start_char": integer (0-indexed character offset within start_line where the issue begins; use 0 for the whole line)
- "end_char": integer (0-indexed character offset within end_line where the issue ends; use 0 for the whole line)
- "text": string (the feedback text, markdown supported)
- "rubric_comment_id": integer or null (ID of a matching rubric item, if applicable)
- "point_delta": number or null (suggested point deduction/bonus, negative for deduction)

When your feedback targets a specific expression, variable, or function call, set start_char/end_char
to highlight just that span. Character offsets are relative to the line content AFTER the line number
prefix (i.e. count from the actual code, not from the "N: " prefix). Use start_char=0, end_char=0
when the comment applies to the entire line or block. Do NOT use start_char/end_char for notebook files.
{notebook_note}
Generate only substantive comments. Return an empty array [] if no issues found.
"""

        result = await self._generate(system_prompt, user_prompt, label='suggested comments generation')
        if not result.success:
            return [result]
        try:
            result.text = self._parse_json_response(result.text)
            return [result]
        except Exception as e:
            error_msg = self._parse_error(e)
            logger.error(f"AI suggested comments generation failed: {e}", exc_info=True)
            return [GenerationResult(text="", success=False, error=error_msg)]

    async def generate_file_suggestions(self, submission, file_obj, variant_id_override: int | None = None) -> list[GenerationResult]:
        """
        Generate AI-suggested comments for a single file within a submission.
        Returns a list of GenerationResults (typically one). The text of each
        result is a JSON array of suggestion objects.

        When *variant_id_override* is given, use that specific prompt variant
        instead of the active one (used during A/B experiments).
        """
        from asgiref.sync import sync_to_async

        ctx = await sync_to_async(self._collect_submission_context)(submission)
        assignment = submission.assignment

        # Pass test_results="" for backward compat with DB-stored prompt variants.
        # Actual test results go in user prompt to keep system prompt cacheable.
        system_prompt, variant_id = await self._resolve_and_format_prompt(
            'suggested_comments',
            dict(
                assignment_name=assignment.name,
                assignment_description=ctx['assignment_description'],
                rubric_context=ctx['rubric_context'] or "No rubric defined.",
                test_results="",
            ),
            variant_id_override=variant_id_override,
        )

        test_results_section = ctx['test_results'] or "No test results available."

        # Build user prompt with just the target file, plus context from other files
        is_notebook = file_obj.name.endswith('.ipynb')
        if is_notebook:
            content = _format_notebook_as_cells(file_obj.data)
        else:
            content = file_obj.data
            if len(content) > 50000:
                content = content[:50000] + "\n... (truncated)"
            content = self._add_line_numbers(content)

        other_files = []
        for f in ctx['files']:
            if f['id'] != file_obj.id:
                other_files.append(f"- {f['name']}")

        context_section = ""
        if other_files:
            context_section = f"\n\nOther files in this submission (names only, for reference):\n{chr(10).join(other_files)}"

        notebook_note = ""
        if is_notebook:
            notebook_note = (
                "\n\nIMPORTANT: This is a notebook (.ipynb) file. start_line and end_line must be "
                "1-based CELL NUMBERS (matching the \"CELL N\" headers above), NOT line numbers within a cell. "
                "For example, to comment on the first cell (CELL 1) use start_line=1, end_line=1."
            )
            file_header = f"### File: {file_obj.name} (ID: {file_obj.id}) [NOTEBOOK]\n{content}"
        else:
            file_header = f"### File: {file_obj.name} (ID: {file_obj.id})\n```{file_obj.extension.lstrip('.')}\n{content}\n```"

        user_prompt = f"""Analyze the following file and generate feedback comments for it.
Each line of code is prefixed with its 0-indexed line number (e.g. "0: ...", "1: ...").
Use these exact line numbers in your response.
IMPORTANT: Only generate comments about code in the TARGET file below. The other files are provided as context only — do NOT generate suggestions for them.

{test_results_section}

{file_header}
{context_section}

Respond with ONLY a JSON array of comment objects. Each object must have:
- "file_id": {file_obj.id} (this file's ID)
- "start_line": integer (the 0-indexed line number shown at the start of each line)
- "end_line": integer (the 0-indexed line number shown at the start of each line)
- "start_char": integer (0-indexed character offset within start_line where the issue begins; use 0 for the whole line)
- "end_char": integer (0-indexed character offset within end_line where the issue ends; use 0 for the whole line)
- "text": string (the feedback text, markdown supported)
- "rubric_comment_id": integer or null (ID of a matching rubric item, if applicable)
- "point_delta": number or null (suggested point deduction/bonus, negative for deduction)

When your feedback targets a specific expression, variable, or function call, set start_char/end_char
to highlight just that span. Character offsets are relative to the line content AFTER the line number
prefix (i.e. count from the actual code, not from the "N: " prefix). Use start_char=0, end_char=0
when the comment applies to the entire line or block. Do NOT use start_char/end_char for notebook files.
{notebook_note}
Generate only substantive comments. Return an empty array [] if no issues found.
"""

        result = await self._generate(system_prompt, user_prompt, label='file suggestions generation')
        result.variant_id = variant_id
        if not result.success:
            return [result]
        try:
            result.text = self._parse_json_response(result.text)
            return [result]
        except Exception as e:
            error_msg = self._parse_error(e)
            logger.error(f"AI file suggestions generation failed: {e}", exc_info=True)
            return [GenerationResult(text="", success=False, error=error_msg)]

    async def generate_submission_summary(self, submission, target_file=None, variant_id_override: int | None = None) -> GenerationResult:
        """Generate a summary of a submission to help graders orient themselves.

        Args:
            submission: The Submission to summarize.
            target_file: Optional SubmissionFile to focus the summary on (the detected
                "main" file). When provided, the prompt is adjusted to present this file
                prominently and instruct the AI to focus its analysis on it. Other files
                are still included as context. When None, all files are treated equally
                (existing behavior).
            variant_id_override: When given, use this specific prompt variant instead
                of the active one (used during A/B experiments).
        """
        from asgiref.sync import sync_to_async

        ctx = await sync_to_async(self._collect_submission_context)(submission)
        assignment = submission.assignment

        has_description = bool(ctx['assignment_description'])
        description_comparison = (
            "- How the submission compares to the assignment requirements (based on the description above)"
            if has_description else ""
        )

        # Pass test_results="" for backward compat with DB-stored prompt variants.
        # Actual test results go in user prompt to keep system prompt cacheable.
        format_kwargs = dict(
            assignment_name=assignment.name,
            assignment_description=ctx['assignment_description'],
            test_results="",
            rubric=ctx['rubric_context'] or "No rubric defined.",
            description_comparison=description_comparison,
        )

        # A per-assignment override wins over the global/active variant (A/B experiments,
        # which force a specific variant_id_override, still take precedence over the override).
        assignment_override = (assignment.ai_summary_prompt or "").strip()
        if assignment_override and variant_id_override is None:
            try:
                system_prompt = assignment_override.format(**format_kwargs)
            except (KeyError, IndexError, ValueError):
                system_prompt = assignment_override
            variant_id = None
        else:
            system_prompt, variant_id = await self._resolve_and_format_prompt(
                'submission_summary', format_kwargs, variant_id_override=variant_id_override,
            )

        test_results_section = ctx['test_results'] or "No test results available."

        # Build user prompt with file contents, annotating student vs provided files
        target_file_id = target_file.id if target_file else None
        primary_sections = []
        context_sections = []
        for f in ctx['files']:
            label = f['name']
            if f.get('is_student_file'):
                label += ' (student implementation)'
            else:
                label += ' (provided template/test)'
            section = f"### File: {label}\n```{f['extension'].lstrip('.')}\n{f['content']}\n```"

            if target_file_id and f['id'] == target_file_id:
                primary_sections.append(section)
            else:
                context_sections.append(section)

        if target_file is not None and primary_sections:
            focus_note = (
                f"Focus your summary on the PRIMARY file below ({target_file.name}), "
                "which has been identified as the main submission file. "
                "The other files are included for context only.\n\n"
                "## PRIMARY FILE\n"
            )
            user_prompt = f"""Summarize this student submission for the grader:

{test_results_section}

{focus_note}{chr(10).join(primary_sections)}

## CONTEXT FILES
{chr(10).join(context_sections)}

Provide a concise markdown summary following the guidelines in your instructions.
"""
        else:
            file_sections = primary_sections + context_sections
            user_prompt = f"""Summarize this student submission for the grader:

{test_results_section}

{chr(10).join(file_sections)}

Provide a concise markdown summary following the guidelines in your instructions.
"""

        result = await self._generate(system_prompt, user_prompt, label='submission summary generation')
        result.variant_id = variant_id
        if result.success:
            result.text = result.text.strip()
        return result

    async def generate_assignment_description(self, assignment) -> GenerationResult:
        """Generate an AI description of what an assignment is asking students to do."""
        from asgiref.sync import sync_to_async
        from core.models import RubricCategory, SubmissionFile

        CONTEXT_BUDGET = 80000  # chars — stay well under model token limits
        PER_SUBMISSION_BUDGET = 10000

        def _collect_assignment_context():
            # Collect template/assignment files
            tpl_parts = []
            for af in assignment.files.filter(hidden=False, is_test_resource=False):
                content = af.data
                if len(content) > 20000:
                    content = content[:20000] + "\n... (truncated)"
                tpl_parts.append(f"### {af.name}\n```\n{content}\n```")

            # Collect test case definitions
            tst_parts = []
            for tc in assignment.testCategories.all():
                for test in tc.testCases.all():
                    desc = getattr(test, 'description', '') or ''
                    tst_parts.append(f"- {desc or test.text[:100]}")

            # Collect rubric
            rub_parts = []
            for category in RubricCategory.objects.filter(assignment=assignment).prefetch_related('rubricComments'):
                cat_text = f"### {category.name} ({category.pointLimit} pts)\n"
                for rc in category.rubricComments.all():
                    cat_text += f"  - {rc.name or rc.text[:60]} ({rc.pointDelta:+g} pts)\n"
                rub_parts.append(cat_text)

            # Calculate remaining budget for submission samples
            materials_size = (
                sum(len(p) for p in tpl_parts)
                + sum(len(p) for p in tst_parts)
                + sum(len(p) for p in rub_parts)
            )
            remaining = CONTEXT_BUDGET - materials_size
            max_samples = min(3, max(0, remaining // PER_SUBMISSION_BUDGET))

            # Sample student submissions (prefer finalized, fall back to any)
            sub_parts = []
            if max_samples > 0:
                student_file_names = set(
                    assignment.files.filter(hidden=False, is_test_resource=False)
                    .values_list('name', flat=True)
                )
                submissions = list(
                    assignment.submissions
                    .order_by('-isFinalized', '?')[:max_samples]
                )
                for sub in submissions:
                    files_text = []
                    chars_used = 0
                    for sf in SubmissionFile.objects.filter(submission=sub):
                        if sf.name not in student_file_names:
                            continue
                        content = sf.data
                        budget_left = PER_SUBMISSION_BUDGET - chars_used
                        if budget_left <= 0:
                            break
                        if len(content) > budget_left:
                            content = content[:budget_left] + "\n... (truncated)"
                        files_text.append(f"#### {sf.name}\n```\n{content}\n```")
                        chars_used += len(content)
                    if files_text:
                        sub_parts.append(f"### Submission Sample\n" + "\n".join(files_text))

            return tpl_parts, tst_parts, rub_parts, sub_parts

        template_parts, test_parts, rubric_parts, submission_parts = await sync_to_async(_collect_assignment_context)()

        submission_samples_text = (
            "Student Submission Samples (use these to understand the range of student work):\n"
            + "\n".join(submission_parts)
            if submission_parts
            else "No student submissions available yet."
        )

        system_prompt, _variant_id = await self._resolve_and_format_prompt(
            'assignment_description',
            dict(
                assignment_name=assignment.name,
                explanation=assignment.explanation or "(No student-facing instructions provided)",
                template_files="\n".join(template_parts) if template_parts else "No template files.",
                test_cases="\n".join(test_parts) if test_parts else "No test cases defined.",
                rubric="\n".join(rubric_parts) if rubric_parts else "No rubric defined.",
                submission_samples=submission_samples_text,
            ),
        )

        user_prompt = "Generate a concise assignment description based on the materials above."

        result = await self._generate(system_prompt, user_prompt, label='assignment description generation')
        if result.success:
            result.text = result.text.strip()
        return result

    async def generate_quiz_questions(
        self,
        assignment=None,
        num_questions: int = 5,
        question_types: Optional[list[str]] = None,
        source_question=None,
        instructions: str = '',
    ) -> GenerationResult:
        """Generate suggested quiz questions from an assignment and course material.

        Two modes:
        - Fresh generation (default): produce ``num_questions`` new questions from the
          assignment + course context.
        - Refresh (``source_question`` set): produce one improved variant of an existing
          question — the cross-semester "update this question" path.

        Returns a ``GenerationResult`` whose ``text`` is a JSON array of question objects.
        """
        from asgiref.sync import sync_to_async
        from core.models import RubricCategory

        assignment = assignment or self.assignment
        if question_types is None:
            question_types = ['multiple_choice', 'true_false', 'short_answer', 'essay', 'code']

        CONTEXT_BUDGET = 60000  # chars — stay well under model token limits

        def _collect_context():
            language = ''
            tpl_parts: list[str] = []
            tst_parts: list[str] = []
            rub_parts: list[str] = []
            if assignment is not None:
                for af in assignment.files.filter(hidden=False, is_test_resource=False):
                    content = af.data
                    if len(content) > 15000:
                        content = content[:15000] + "\n... (truncated)"
                    tpl_parts.append(f"### {af.name}\n```\n{content}\n```")
                for tc in assignment.testCategories.all():
                    for test in tc.testCases.all():
                        desc = getattr(test, 'description', '') or ''
                        tst_parts.append(f"- {desc or test.text[:100]}")
                for category in RubricCategory.objects.filter(assignment=assignment).prefetch_related('rubricComments'):
                    cat_text = f"### {category.name}\n"
                    for rc in category.rubricComments.all():
                        cat_text += f"  - {rc.name or rc.text[:60]}\n"
                    rub_parts.append(cat_text)
                try:
                    language = assignment.environment.language or ''
                except Exception:
                    language = ''

            # Course-level material (CourseFile), bounded by the remaining budget.
            mat_parts: list[str] = []
            used = sum(len(p) for p in tpl_parts)
            for cf in self.course.files.select_related('content'):
                if used >= CONTEXT_BUDGET:
                    break
                content = cf.content.data
                budget_left = CONTEXT_BUDGET - used
                if len(content) > budget_left:
                    content = content[:budget_left] + "\n... (truncated)"
                mat_parts.append(f"### {cf.name}\n```\n{content}\n```")
                used += len(content)

            # Serialize an existing question for refresh mode.
            existing = ''
            if source_question is not None:
                stype = source_question.questionType
                lines = [
                    "--- Existing question to refresh ---",
                    f"Type: {stype}",
                    f"Points: {source_question.points}",
                    f"Stem: {source_question.text}",
                ]
                if source_question.description:
                    lines.append(f"Description (Markdown, shown to the student beneath the stem):\n"
                                 f"{source_question.description}")
                choices = list(source_question.choices.all())
                if choices:
                    lines.append("Current choices:")
                    for ch in choices:
                        mark = " (correct)" if ch.isCorrect else ""
                        lines.append(f"  - {ch.text}{mark}")
                if source_question.referenceSolution:
                    lines.append(f"Reference solution:\n{source_question.referenceSolution}")
                if stype in ('multiple_choice', 'multiple_answers', 'true_false', 'short_answer', 'numerical'):
                    lines.append(
                        f"Keep the type `{stype}`. Your updated version MUST include a non-empty "
                        "`choices` array — regenerate the options to match the new stem and mark the "
                        "correct one(s). Do not return an empty choices array."
                    )
                existing = "\n".join(lines)
                if not language:
                    language = source_question.language or ''

            return tpl_parts, tst_parts, rub_parts, mat_parts, existing, language

        template_parts, test_parts, rubric_parts, material_parts, existing_question, language = \
            await sync_to_async(_collect_context)()

        system_prompt, variant_id = await self._resolve_and_format_prompt(
            'quiz_generation',
            dict(
                assignment_name=(assignment.name if assignment is not None else '(no assignment)'),
                explanation=(assignment.explanation if assignment is not None and assignment.explanation
                             else "(No student-facing instructions provided)"),
                template_files="\n".join(template_parts) if template_parts else "No template files.",
                test_cases="Test cases:\n" + "\n".join(test_parts) if test_parts else "No test cases defined.",
                rubric="Rubric:\n" + "\n".join(rubric_parts) if rubric_parts else "No rubric defined.",
                course_materials="Course materials:\n" + "\n".join(material_parts)
                if material_parts else "No additional course materials.",
                language=language or "(unspecified)",
                num_questions=num_questions,
                question_types=", ".join(question_types),
                existing_question=existing_question,
                instructions=instructions or "(none)",
            ),
        )

        # The user_prompt is always sent regardless of the (DB-overridable) system prompt,
        # so it's the reliable place to enforce the choices contract.
        user_prompt = (
            "Generate the quiz questions now as a JSON array, following the format exactly. "
            "CRITICAL: every multiple_choice, multiple_answers, true_false, short_answer, and numerical "
            'question MUST include a non-empty "choices" array (each item: {"text": ..., "is_correct": ...}). '
            "Only essay and code questions omit choices. Each question may include an optional "
            '"description" — it is shown to the student beneath the stem and rendered as Markdown, '
            "so use Markdown there for highlighting (fenced code blocks, bold, inline code)."
        )
        result = await self._generate(system_prompt, user_prompt, label='quiz question generation')
        if result.success:
            result.text = result.text.strip()
            result.variant_id = variant_id
        return result

    async def generate_personalized_quiz_questions(self, section, submission,
                                                   demo_files=None) -> GenerationResult:
        """Generate per-student quiz questions for one QuizGeneratedSection from the
        student's submission.

        The instructor's section prompt is resolved first ({variables} substituted per
        student — see core/prompts/variables.py), then embedded in the platform-level
        'personalized_quiz_generation' prompt, which owns the JSON output contract.
        The model sees ONLY what the prompt's variables reference — nothing is attached
        implicitly (instructors opt in via {submission_files}, {submission_test_results},
        etc.; the section editor's default template references them).
        Returns a ``GenerationResult`` whose ``text`` is a JSON array of question objects.
        """
        from asgiref.sync import sync_to_async
        from core.prompts.variables import VariableContext, resolve_template

        def _collect_context():
            # Submission is None on the eager path (submission-free prompts, including
            # standalone quizzes) — the service's own assignment (possibly None) provides
            # whatever context exists.
            assignment = submission.assignment if submission is not None else self.assignment
            ctx = VariableContext(course=self.course, assignment=assignment,
                                  submission=submission, section=section,
                                  demo_files=demo_files)
            resolved = resolve_template(section.systemPrompt, ctx)
            try:
                language = (assignment.environment.language or '') if assignment else ''
            except Exception:
                language = ''
            name = assignment.name if assignment is not None else section.quiz.title
            return resolved.text, resolved.images, name, language

        instructor_text, images, assignment_name, language = await sync_to_async(_collect_context)()

        question_types = section.questionTypes or [
            'multiple_choice', 'true_false', 'short_answer', 'essay', 'code']
        system_prompt, variant_id = await self._resolve_and_format_prompt(
            'personalized_quiz_generation',
            dict(
                instructor_prompt=instructor_text,
                assignment_name=assignment_name,
                num_questions=section.numQuestions,
                question_types=", ".join(question_types),
                language=language or "(unspecified)",
            ),
        )

        # The user_prompt is always sent regardless of the (DB-overridable) system prompt,
        # so it's the reliable place to enforce the choices contract.
        user_prompt = (
            f"Generate exactly {section.numQuestions} quiz questions now as a JSON array, "
            "following the format exactly. CRITICAL: every multiple_choice, multiple_answers, "
            "true_false, short_answer, and numerical question MUST include a non-empty "
            '"choices" array (each item: {"text": ..., "is_correct": ...}). '
            "Only essay and code questions omit choices. "
            'Use the optional "description" (Markdown, fenced code blocks) for any context '
            "that helps the student answer — their code, test output, examples — at your "
            "discretion; if the stem refers to something specific, show it there. "
            'CRITICAL: every question MUST include a non-empty "reference_solution" — a '
            "grader-only answer key (correct answer/working code, plus worked steps for "
            "hand-computation questions). It is never shown to the student."
        )
        # Images referenced by the instructor's variables ride on the user turn; the
        # "(image attached: …)" markers in instructor_text tell the model what they are.
        result = await self._generate(system_prompt, user_prompt, label='personalized quiz generation',
                                      images=images)
        # Recorded (success or not) so staff can inspect exactly what the model was given.
        result.resolved_prompt = instructor_text
        if result.success:
            result.text = result.text.strip()
            result.variant_id = variant_id
        return result


REGION_COMMENT_MARKER = 1_000_000


def _extract_pdf_selection(
    doc: pymupdf.Document,
    selected_pages: list[int],
    start_char: int | None,
    end_char: int | None,
) -> str:
    """Extract text from a PDF based on the comment selection type.
    
    Handles three modes:
    - Region selection (start_char >= 1,000,000): decodes bounding box percentages
      and extracts text from that rectangular area on the page.
    - Text selection (start_char/end_char provided): extracts the full page as
      markdown (character offsets from the browser text layer may not match pymupdf).
    - Page-level (no char info): extracts the full selected pages as markdown.
    """
    import pymupdf
    import pymupdf4llm as pdf_utils

    has_chars = start_char is not None and end_char is not None

    # Region-based selection: decode bounding box and extract text from that area
    if has_chars and start_char is not None and end_char is not None and start_char >= REGION_COMMENT_MARKER and end_char >= REGION_COMMENT_MARKER:
        s = start_char - REGION_COMMENT_MARKER
        e = end_char - REGION_COMMENT_MARKER
        left_pct = s // 101
        top_pct = s % 101
        right_pct = e // 101
        bottom_pct = e % 101

        parts = []
        for page_idx in selected_pages:
            if page_idx < 0 or page_idx >= len(doc):
                continue
            page = doc[page_idx]
            rect = page.rect
            clip = pymupdf.Rect(
                rect.x0 + (left_pct / 100) * rect.width,
                rect.y0 + (top_pct / 100) * rect.height,
                rect.x0 + (right_pct / 100) * rect.width,
                rect.y0 + (bottom_pct / 100) * rect.height,
            )
            text = page.get_textbox(clip).strip()
            if text:
                parts.append(text)

        if parts:
            return "\n\n".join(parts)
        # Fall through to full-page extraction if region extraction yielded nothing
        # (e.g. scanned PDF without OCR)

    # Text selection or page-level: extract full selected pages as markdown
    return str(pdf_utils.to_markdown(doc, pages=selected_pages))


def build_context_from_file(
    file: SubmissionFile,
    start_line: int,
    end_line: int,
    start_char: int | None = None,
    end_char: int | None = None,
) -> GenerationContext:
    """
    Build generation context from a submission file.
    
    Args:
        file: The SubmissionFile being commented on
        start_line: Start line of selection (0-indexed). For notebooks, this is the cell index.
                    For PDFs, this is the 1-based page number.
        end_line: End line of selection (0-indexed). For notebooks, this is the cell index.
                  For PDFs, this is the 1-based page number.
        start_char: Optional character offset or encoded region start (for PDFs).
        end_char: Optional character offset or encoded region end (for PDFs).
        
    Returns:
        GenerationContext with populated fields
    """
    submission = file.submission
    assignment = submission.assignment
    
    # Handle notebooks specially
    if file.name.endswith('.ipynb'):
        import json
        try:
            notebook = json.loads(file.data)
            cells = notebook.get('cells', [])
            
            # Get selected cells
            selected_parts = []
            # Bound the indices (treating inputs as 0-based indices)
            start_idx = max(0, min(start_line, len(cells) - 1)) if cells else 0
            end_idx = max(0, min(end_line, len(cells) - 1)) if cells else 0
            
            for i in range(start_idx, end_idx + 1):
                cell = cells[i]
                cell_type = cell.get('cell_type', 'code')
                content = _parse_notebook_cell(cell, include_outputs=True)
                selected_parts.append(f"[{cell_type.upper()} CELL]\n{content}")
            
            selected_content = "\n\n".join(selected_parts)
            
            # Helper for full file content
            def get_notebook_markdown():
                parts = []
                for i, cell in enumerate(cells):
                    cell_type = cell.get('cell_type', 'code')
                    content = _parse_notebook_cell(cell, include_outputs=True)
                    parts.append(f"## Cell {i} ({cell_type})\n{content}")
                return "\n\n".join(parts)
                
            file_content_markdown = get_notebook_markdown()
            
        except json.JSONDecodeError:
             # Fallback to treating as text if json parse fails
             selected_content = "Error: Could not parse notebook content."
             file_content_markdown = file.data
    elif file.name.endswith('.pdf'):
        import pymupdf
        import pymupdf4llm as pdf_utils
        
        prefix = "data:application/pdf;base64,"
        if file.data.startswith(prefix):
            try:
                base64_data = file.data[len(prefix):]
                pdf_bytes, _ = base64_decode(base64_data.encode())  # Validate base64
            except Exception:
                selected_content = "Error: Could not decode PDF content."
                pdf_bytes = b""
        else: 
            pdf_bytes = file.data
        
        # Frontend sends 1-based page numbers as start_line/end_line for PDFs.
        # pymupdf4llm.to_markdown accepts 0-based page numbers via the `pages` param.
        selected_pages = list(range(start_line - 1, end_line))  # convert 1-based to 0-based
        
        stream = pdf_bytes if isinstance(pdf_bytes, bytes) else pdf_bytes.encode()
        doc = pymupdf.open(stream=stream, filetype="pdf")
        
        # Try to extract precise selection based on start_char/end_char
        selected_content = _extract_pdf_selection(
            doc, selected_pages, start_char, end_char
        )
        doc.close()

        # Convert entire PDF to markdown for full file content (shared extraction path).
        file_content_markdown = extract_pdf_text(file.data) or "(could not extract PDF text)"
    else:
        # Standard text file handling
        lines = file.data.split('\n')
        # Bound the indices (treating inputs as 0-based indices)
        start_idx = max(0, min(start_line, len(lines) - 1)) if lines else 0
        end_idx = max(0, min(end_line, len(lines) - 1)) if lines else 0
        
        selected_lines = lines[start_idx:end_idx + 1]
        
        # Narrow to character offsets within the selected lines if provided.
        # start_char/end_char are 0-indexed offsets within their respective lines.
        if start_char is not None and selected_lines:
            selected_lines[0] = selected_lines[0][start_char:]
        if end_char is not None and selected_lines:
            selected_lines[-1] = selected_lines[-1][:end_char]
        
        selected_content = '\n'.join(selected_lines)
        file_content_markdown = file.data

    context = GenerationContext(
        selected_content=selected_content,
        assignment_name=assignment.name,
        file_name=file.name,
    )
    
    # Determine if we need full file content or all files content
    system_prompt_template = assignment.ai_system_prompt if assignment.ai_system_prompt else AIService.resolve_prompt('comment_generation')[0]
    needs_file_content = '{file_content}' in system_prompt_template
    needs_all_files = '{all_files}' in system_prompt_template
    
    # Always include file content if needed
    if needs_file_content:
        context.file_content = file_content_markdown
        
    # Always include all files content (lazy loaded effectively since we just do queries here)
    if needs_all_files:
        other_files = []
        
        # Include other submission files
        for sub_file in submission.files.exclude(id=file.id):
            # If other file is notebook, maybe just skipped or simplified?
            # For now treating as raw data to save complexity, unless requested
            content_snippet = sub_file.data[:10000] + "..." if len(sub_file.data) > 10000 else sub_file.data
            other_files.append(f"**{sub_file.name}:**\n```\n{content_snippet}\n```")
            
        # Include assignment files
        for assign_file in assignment.files.all():
            # Binary files (images, ...) are stored as data URIs — never inline base64.
            af_data = assign_file.data
            if af_data.startswith('data:'):
                af_data = f"(binary file '{assign_file.name}' not shown)"
            other_files.append(f"**[Assignment File] {assign_file.name}:**\n```\n{af_data}\n```")
            
        context.all_files_content = '\n\n'.join(other_files)
    
    return context


def extract_pdf_text(data: str) -> str | None:
    """Extract a PDF's full text as markdown from its stored ``File.data``.

    ``data`` is either a ``data:application/pdf;base64,...`` URI (how binary files are
    stored) or raw bytes-as-str. Returns the markdown, or ``None`` on any failure — callers
    substitute a placeholder so a bad PDF never leaks raw base64 into a prompt. This is the
    single extraction path shared by ``build_context_from_file`` and the prompt-variable
    resolvers (core/prompts/variables.py)."""
    try:
        import pymupdf
        import pymupdf4llm as pdf_utils

        prefix = "data:application/pdf;base64,"
        if data.startswith(prefix):
            pdf_bytes, _ = base64_decode(data[len(prefix):].encode())
        else:
            pdf_bytes = data.encode() if isinstance(data, str) else data

        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            return str(pdf_utils.to_markdown(doc))
        finally:
            doc.close()
    except Exception:
        logger.exception("Failed to extract PDF text")
        return None


def _format_notebook_as_cells(raw_json: str) -> str:
    """Convert raw .ipynb JSON into an enumerated cell representation.

    Uses 1-based cell numbers (CELL 1, CELL 2, ...) to match the UI display
    and avoid off-by-one errors from the AI model.
    """
    import json as json_mod
    try:
        notebook = json_mod.loads(raw_json)
    except (json_mod.JSONDecodeError, ValueError):
        # If parsing fails, fall back to raw content (truncated)
        return raw_json[:50000] + "\n... (truncated)" if len(raw_json) > 50000 else raw_json

    cells = notebook.get('cells', [])
    if not cells:
        return "(empty notebook)"

    parts = []
    for i, cell in enumerate(cells):
        cell_type = cell.get('cell_type', 'code').upper()
        content = _parse_notebook_cell(cell, include_outputs=True)
        parts.append(f"--- CELL {i + 1} [{cell_type}] ---\n{content}")

    result = "\n\n".join(parts)
    if len(result) > 50000:
        result = result[:50000] + "\n... (truncated)"
    return result


def _parse_notebook_cell(cell, include_outputs=False) -> str:
    """Parse a notebook cell to extract content and optionally outputs. These are nbformat v4 cells."""
    source = cell.get('source', [])
    content = ""
    if isinstance(source, list):
        content = "".join(source)
    else:
        content = str(source)
    
    if include_outputs and cell.get('cell_type') == 'code':
        outputs = cell.get('outputs', [])
        output_text = []
        for output in outputs:
            # Handle stream output (stdout/stderr)
            if output.get('output_type') == 'stream':
                text = output.get('text', [])
                if isinstance(text, list):
                    output_text.append("".join(text))
                else:
                    output_text.append(str(text))
            # Handle execute_result or display_data
            elif output.get('output_type') in ('execute_result', 'display_data'):
                data = output.get('data', {})
                # Prefer plain text representation
                if 'text/plain' in data:
                    text = data['text/plain']
                    if isinstance(text, list):
                        output_text.append("".join(text))
                    else:
                        output_text.append(str(text))
                elif 'text/html' in data:
                    output_text.append("[HTML Output]")
                elif 'image/png' in data:
                    output_text.append("[Image Output]")
            # Handle errors
            elif output.get('output_type') == 'error':
                ename = output.get('ename', '')
                evalue = output.get('evalue', '')
                output_text.append(f"Error: {ename}: {evalue}")

        if output_text:
            content += "\n\n[OUTPUT]:\n" + "\n".join(output_text)
    
    return content
