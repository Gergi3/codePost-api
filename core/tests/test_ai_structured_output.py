# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Quiz generation asks every provider for structured output: the reply is valid JSON of
the quiz-question shape, instead of free text we hope parses. These tests pin the
per-provider request field each one needs, and the re-ask feedback."""
from types import SimpleNamespace
from typing import cast
from unittest import mock

import jsonschema
import pytest
from asgiref.sync import async_to_sync

from core.models import Course
from core.services.ai_json import parse_json_questions, quiz_questions_schema
from core.services.ai_service import AIService

SCHEMA = {'type': 'object', 'properties': {'questions': {'type': 'array'}},
          'required': ['questions'], 'additionalProperties': False}


class TestQuizQuestionsSchema:
    def test_is_valid_strict_json_schema(self):
        """Every object lists all properties as required with additionalProperties false —
        what OpenAI strict mode demands — and the type enum tracks QUESTION_TYPE_CHOICES."""
        from core.models import QUESTION_TYPE_CHOICES
        schema = quiz_questions_schema()
        jsonschema.Draft202012Validator.check_schema(schema)

        def check(obj):
            if obj.get('type') == 'object':
                assert obj['additionalProperties'] is False
                assert set(obj['required']) == set(obj['properties'])
                for prop in obj['properties'].values():
                    check(prop)
            elif obj.get('type') == 'array':
                check(obj['items'])
        check(schema)
        question = schema['properties']['questions']['items']
        assert question['properties']['type']['enum'] == [k for k, _ in QUESTION_TYPE_CHOICES]

    def test_structured_reply_round_trips_through_the_parser(self):
        reply = {'questions': [{
            'type': 'code', 'text': 'Fix the regex.', 'description': None, 'points': 10,
            'choices': [], 'starter_code': "re.compile(r'\\s*')", 'reference_solution': None}]}
        jsonschema.validate(reply, quiz_questions_schema())
        import json
        assert parse_json_questions(json.dumps(reply)) == reply['questions']


def _usage_response(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2, prompt_tokens_details=None),
        model='m')


class _Client:
    """httpx.AsyncClient stand-in that answers each post with the next response."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(dict(json))  # snapshot: the fallback mutates the body
        return self.responses.pop(0)


def _http_response(status_code, payload):
    return SimpleNamespace(status_code=status_code, text='', json=lambda: payload,
                           raise_for_status=lambda: None)


class TestProviderStructuredOutput:
    def test_openai_sends_strict_json_schema(self):
        svc = AIService.for_config('openai', api_key='k', model='gpt-x')
        create = mock.AsyncMock(return_value=_usage_response('{"questions": []}'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch('openai.AsyncOpenAI', return_value=client):
            async_to_sync(svc._call_openai)('SYS', 'USER', (), SCHEMA)
        assert create.call_args.kwargs['response_format'] == {
            'type': 'json_schema',
            'json_schema': {'name': 'response', 'schema': SCHEMA, 'strict': True}}

    def test_openai_without_schema_sends_no_response_format(self):
        svc = AIService.for_config('openai', api_key='k', model='gpt-x')
        create = mock.AsyncMock(return_value=_usage_response('ok'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch('openai.AsyncOpenAI', return_value=client):
            async_to_sync(svc._call_openai)('SYS', 'USER')
        assert 'response_format' not in create.call_args.kwargs

    def test_gemini_sets_json_mime_and_schema(self):
        svc = AIService.for_config('gemini', api_key='k', model='gemini-x')
        response = SimpleNamespace(text='{"questions": []}', usage_metadata=SimpleNamespace(), model_version='m')
        generate = mock.AsyncMock(return_value=response)
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        with mock.patch('google.genai.Client', return_value=client), \
                mock.patch.object(AIService, '_get_or_create_gemini_cache', mock.AsyncMock(return_value=None)):
            async_to_sync(svc._call_gemini)('SYS', 'USER', (), SCHEMA)
        config = generate.call_args.kwargs['config']
        assert config.response_mime_type == 'application/json'
        assert config.response_json_schema == SCHEMA

    def test_ollama_sends_schema_as_format(self):
        svc = AIService.for_config('ollama', api_key='', base_url='http://ollama', model='llama')
        client = _Client([_http_response(200, {'response': '{"questions": []}', 'model': 'llama'})])
        with mock.patch('httpx.AsyncClient', return_value=client):
            async_to_sync(svc._call_ollama)('SYS', 'USER', (), SCHEMA)
        assert client.posts[0]['format'] == SCHEMA

    def test_portkey_sends_openai_response_format(self):
        svc = AIService.for_config('portkey', api_key='k', base_url='http://pk', model='m')
        client = _Client([_http_response(200, {'choices': [{'message': {'content': '{"questions": []}'}}]})])
        with mock.patch('httpx.AsyncClient', return_value=client):
            text, *_ = async_to_sync(svc._call_portkey)('SYS', 'USER', (), SCHEMA)
        assert text == '{"questions": []}'
        assert client.posts[0]['response_format']['json_schema']['schema'] == SCHEMA

    def test_portkey_falls_back_to_free_text_when_response_format_is_rejected(self):
        """A custom OpenAI-compatible endpoint that 400s on response_format is asked
        again without it; the lenient parser handles that reply."""
        svc = AIService.for_config('custom', api_key='k', base_url='http://custom', model='m')
        client = _Client([
            _http_response(400, {'error': 'response_format unsupported'}),
            _http_response(200, {'choices': [{'message': {'content': '[]'}}]}),
        ])
        with mock.patch('httpx.AsyncClient', return_value=client):
            text, *_ = async_to_sync(svc._call_portkey)('SYS', 'USER', (), SCHEMA)
        assert text == '[]'
        assert len(client.posts) == 2
        assert 'response_format' in client.posts[0]
        assert 'response_format' not in client.posts[1]


@pytest.mark.django_db
class TestQuizGenerationRequests:
    """Both quiz generators pass the schema to the provider and quote a prior error back."""

    def _capture(self):
        captured = {}

        async def fake_dispatch(self, system_prompt, user_prompt, images=(), response_schema=None):
            captured['user_prompt'] = user_prompt
            captured['response_schema'] = response_schema
            return ('{"questions": []}', 1, 1, 2, 0)
        return captured, fake_dispatch

    def test_suggestions_use_schema_and_feedback(self):
        from core.tests.factories import AssignmentFactory
        assignment = AssignmentFactory()
        svc = AIService(cast(Course, assignment.course), assignment)
        captured, fake = self._capture()
        with mock.patch('core.services.ai_service.AIService._dispatch_provider', new=fake):
            async_to_sync(svc.generate_quiz_questions)(assignment=assignment)
            assert captured['response_schema'] == quiz_questions_schema()
            assert 'previous reply' not in captured['user_prompt']
            async_to_sync(svc.generate_quiz_questions)(
                assignment=assignment, prior_error='it was not valid JSON (Extra data).')
        assert captured['user_prompt'].endswith(
            'Your previous reply could not be used: it was not valid JSON (Extra data). '
            'Reply again with only the corrected JSON.')

    def test_personalized_uses_schema_and_feedback(self):
        from django.db.models.signals import post_save
        import factory
        from core.models import Quiz, QuizGeneratedSection
        from core.tests.factories import AssignmentFactory
        assignment = AssignmentFactory()
        with factory.django.mute_signals(post_save):
            quiz = Quiz.objects.create(course=assignment.course, assignment=assignment, title='q')
            section = QuizGeneratedSection.objects.create(quiz=quiz, systemPrompt='Ask one.', numQuestions=1)
        svc = AIService(cast(Course, assignment.course), assignment)
        captured, fake = self._capture()
        with mock.patch('core.services.ai_service.AIService._dispatch_provider', new=fake):
            async_to_sync(svc.generate_personalized_quiz_questions)(
                section, None, prior_error='Could not parse the model output as questions.')
        assert captured['response_schema'] == quiz_questions_schema()
        assert 'Your previous reply could not be used: Could not parse' in captured['user_prompt']
