# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Image assignment files in prompt variables: {assignment_files} stays text-only,
{assignment_files_with_images} / {assignment_file:x.png} / {course_file:x.png} attach the
image for the provider's vision input, and each provider puts it in the right place
(user turn, never inline base64 in the prompt text).
"""
import base64
from types import SimpleNamespace
from typing import cast
from unittest import mock

import factory
import pytest
from asgiref.sync import async_to_sync
from django.db.models.signals import post_save

from core.models import Course
from core.prompts.variables import (
    MAX_PROMPT_IMAGES,
    ImageAttachment,
    VariableContext,
    resolve_template,
    substitute_variables,
)
from core.services.ai_service import AIService

PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00' * 16
PNG_B64 = base64.b64encode(PNG_BYTES).decode()
PNG_URI = 'data:image/png;base64,' + PNG_B64


def _course_with_files(name, files):
    from core.tests.factories import CourseFactory, AssignmentFileFactory
    with factory.django.mute_signals(post_save):
        course = CourseFactory(name=name, period="s2026", organization__name="Rutgers")
        assignment = course.assignments.first()
        for fname, data in files:
            AssignmentFileFactory(assignment=assignment, name=fname, data=data,
                                  extension='.' + fname.rsplit('.', 1)[-1])
    return course, assignment


class TestAssignmentFileImages:
    def test_assignment_files_skips_images_with_a_pointer(self, db):
        course, assignment = _course_with_files('img201', [('main.py', 'x = 1\n'), ('plot.png', PNG_URI)])
        resolved = resolve_template('{assignment_files}', VariableContext(course=course, assignment=assignment))
        assert 'x = 1' in resolved.text
        assert "(image 'plot.png' omitted — use {assignment_files_with_images} to attach it)" in resolved.text
        assert resolved.images == ()
        assert PNG_B64 not in resolved.text

    def test_assignment_files_with_images_attaches(self, db):
        course, assignment = _course_with_files('img202', [('main.py', 'x = 1\n'), ('plot.png', PNG_URI)])
        resolved = resolve_template('{assignment_files_with_images}',
                                    VariableContext(course=course, assignment=assignment))
        assert 'x = 1' in resolved.text
        assert '(image attached: plot.png)' in resolved.text
        assert resolved.images == (ImageAttachment(name='plot.png', mime='image/png', base64_data=PNG_B64),)
        assert PNG_B64 not in resolved.text

    def test_named_assignment_file_attaches_its_image(self, db):
        course, assignment = _course_with_files('img203', [('plot.png', PNG_URI)])
        resolved = resolve_template('Look at {assignment_file:plot.png}.',
                                    VariableContext(course=course, assignment=assignment))
        assert resolved.text == 'Look at (image attached: plot.png).'
        assert [i.name for i in resolved.images] == ['plot.png']

    def test_named_course_file_attaches_its_image(self, db):
        from core.models import CourseFile
        from core.tests.factories import CourseFactory
        with factory.django.mute_signals(post_save):
            course = CourseFactory(name='img204', period='s2026', organization__name='Rutgers')
            CourseFile.objects.create(course=course, name='diagram.png', data=PNG_URI, extension='.png')
        resolved = resolve_template('{course_file:diagram.png}', VariableContext(course=course))
        assert resolved.text == '(image attached: diagram.png)'
        assert [i.mime for i in resolved.images] == ['image/png']

    def test_same_image_referenced_twice_is_attached_once(self, db):
        course, assignment = _course_with_files('img205', [('plot.png', PNG_URI)])
        resolved = resolve_template('{assignment_file:plot.png} {assignment_files_with_images}',
                                    VariableContext(course=course, assignment=assignment))
        assert len(resolved.images) == 1
        assert "(image 'plot.png' is identical to 'plot.png', already attached above)" in resolved.text

    def test_identical_image_under_another_name_is_attached_once(self, db):
        course, assignment = _course_with_files('img210', [('a.png', PNG_URI), ('copy.png', PNG_URI)])
        resolved = resolve_template('{assignment_files_with_images}',
                                    VariableContext(course=course, assignment=assignment))
        assert [i.name for i in resolved.images] == ['a.png']
        assert "(image 'copy.png' is identical to 'a.png', already attached above)" in resolved.text

    def test_svg_and_pdf_are_not_attached(self, db):
        svg_uri = 'data:image/svg+xml;base64,' + base64.b64encode(b'<svg/>').decode()
        course, assignment = _course_with_files('img206', [('icon.svg', svg_uri), ('spec.pdf', 'not a pdf')])
        resolved = resolve_template('{assignment_files_with_images}',
                                    VariableContext(course=course, assignment=assignment))
        assert resolved.images == ()
        assert "(binary file 'icon.svg' not shown)" in resolved.text
        assert "could not extract text from PDF 'spec.pdf'" in resolved.text

    def test_image_cap(self, db):
        # Distinct bytes per image — identical content would be deduplicated instead.
        files = [(f'p{i}.png', 'data:image/png;base64,' + base64.b64encode(PNG_BYTES + bytes([i])).decode())
                 for i in range(MAX_PROMPT_IMAGES + 2)]
        course, assignment = _course_with_files('img207', files)
        resolved = resolve_template('{assignment_files_with_images}',
                                    VariableContext(course=course, assignment=assignment))
        assert len(resolved.images) == MAX_PROMPT_IMAGES
        assert f"at most {MAX_PROMPT_IMAGES} images per prompt" in resolved.text

    def test_substitute_variables_is_the_text_view(self, db):
        course, assignment = _course_with_files('img208', [('plot.png', PNG_URI)])
        text, used = substitute_variables('{assignment_file:plot.png}',
                                          VariableContext(course=course, assignment=assignment))
        assert text == '(image attached: plot.png)'
        assert used == {'assignment_file'}


IMAGES = (ImageAttachment(name='plot.png', mime='image/png', base64_data=PNG_B64),)


def _usage_response(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2, prompt_tokens_details=None),
        model='m')


class TestProviderImagePayloads:
    def test_openai_puts_images_on_the_user_turn(self):
        svc = AIService.for_config('openai', api_key='k', model='gpt-x')
        create = mock.AsyncMock(return_value=_usage_response('ok'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch('openai.AsyncOpenAI', return_value=client):
            text, *_ = async_to_sync(svc._call_openai)('SYS', 'USER', IMAGES)
        assert text == 'ok'
        messages = create.call_args.kwargs['messages']
        assert messages[0] == {"role": "system", "content": "SYS"}
        assert messages[1]['content'] == [
            {"type": "text", "text": "USER"},
            {"type": "image_url", "image_url": {"url": PNG_URI}},
        ]

    def test_openai_without_images_keeps_plain_string_content(self):
        svc = AIService.for_config('openai', api_key='k', model='gpt-x')
        create = mock.AsyncMock(return_value=_usage_response('ok'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch('openai.AsyncOpenAI', return_value=client):
            async_to_sync(svc._call_openai)('SYS', 'USER')
        assert create.call_args.kwargs['messages'][1] == {"role": "user", "content": "USER"}

    def test_gemini_sends_decoded_bytes_parts(self):
        from google.genai import types
        svc = AIService.for_config('gemini', api_key='k', model='gemini-x')
        response = SimpleNamespace(text='ok', usage_metadata=SimpleNamespace(), model_version='m')
        generate = mock.AsyncMock(return_value=response)
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        with mock.patch('google.genai.Client', return_value=client), \
                mock.patch.object(AIService, '_get_or_create_gemini_cache', mock.AsyncMock(return_value=None)):
            async_to_sync(svc._call_gemini)('SYS', 'USER', IMAGES)
        contents = generate.call_args.kwargs['contents']
        assert isinstance(contents, list) and len(contents) == 2
        assert contents[0] == types.Part.from_text(text='USER')
        assert contents[1].inline_data.mime_type == 'image/png'
        assert contents[1].inline_data.data == PNG_BYTES

    def test_ollama_sends_bare_base64_images(self):
        svc = AIService.for_config('ollama', api_key='', base_url='http://ollama', model='llava')
        post = mock.AsyncMock(return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {'response': 'ok', 'prompt_eval_count': 1, 'eval_count': 1, 'model': 'llava'}))
        client = mock.MagicMock()
        client.__aenter__.return_value = SimpleNamespace(post=post)
        with mock.patch('httpx.AsyncClient', return_value=client):
            async_to_sync(svc._call_ollama)('SYS', 'USER', IMAGES)
        assert post.call_args.kwargs['json']['images'] == [PNG_B64]

    def test_portkey_uses_openai_content_parts(self):
        svc = AIService.for_config('portkey', api_key='k', base_url='http://pk', model='m')
        post = mock.AsyncMock(return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {'choices': [{'message': {'content': 'ok'}}],
                          'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}))
        client = mock.MagicMock()
        client.__aenter__.return_value = SimpleNamespace(post=post)
        with mock.patch('httpx.AsyncClient', return_value=client):
            async_to_sync(svc._call_portkey)('SYS', 'USER', IMAGES)
        user = post.call_args.kwargs['json']['messages'][1]
        assert user['content'][1] == {"type": "image_url", "image_url": {"url": PNG_URI}}


@pytest.mark.django_db
def test_personalized_quiz_generation_forwards_images():
    from core.models import Quiz, QuizGeneratedSection
    course, assignment = _course_with_files('img209', [('plot.png', PNG_URI)])
    with factory.django.mute_signals(post_save):
        quiz = Quiz.objects.create(course=course, assignment=assignment, title='q')
        section = QuizGeneratedSection.objects.create(
            quiz=quiz, systemPrompt='Ask about {assignment_file:plot.png}', numQuestions=1)
    captured = {}

    async def fake_dispatch(self, system_prompt, user_prompt, images=(), response_schema=None):
        captured['images'] = images
        captured['system_prompt'] = system_prompt
        return ('[]', 1, 1, 2, 0)

    # No submission (eager path) — the service's own assignment supplies the files.
    svc = AIService(cast(Course, course), assignment)
    with mock.patch('core.services.ai_service.AIService._dispatch_provider', new=fake_dispatch):
        result = async_to_sync(svc.generate_personalized_quiz_questions)(section, None)
    assert result.success
    assert [i.name for i in captured['images']] == ['plot.png']
    assert '(image attached: plot.png)' in captured['system_prompt']
    assert PNG_B64 not in captured['system_prompt']
    assert result.resolved_prompt == 'Ask about (image attached: plot.png)'


STARTER = "def helper(x):\n    return x * 2  # starter code the student must not edit\n"


def _submission_with_files(course, assignment, files):
    from core.models import Submission, SubmissionFile
    from core.tests.factories import UserFactory
    with factory.django.mute_signals(post_save):
        submission = Submission.objects.create(assignment=assignment)
        submission.students.add(UserFactory())
        for fname, data in files:
            SubmissionFile.objects.create(submission=submission, name=fname, data=data,
                                          extension='.' + fname.rsplit('.', 1)[-1])
    return submission


class TestUnchangedFilesNotRepeated:
    """A file the student submitted unchanged from the assignment is sent once."""

    def test_unchanged_starter_in_submission_files_points_back(self, db):
        course, assignment = _course_with_files('dd301', [('helper.py', STARTER)])
        submission = _submission_with_files(course, assignment,
                                            [('helper.py', STARTER), ('main.py', 'import helper\nprint(helper.helper(2))\n')])
        resolved = resolve_template('{assignment_files}\n---\n{submission_files}',
                                    VariableContext(course=course, assignment=assignment, submission=submission))
        assert resolved.text.count(STARTER.strip()) == 1
        assert "### helper.py\n(identical to 'helper.py' above — not repeated)" in resolved.text
        assert 'print(helper.helper(2))' in resolved.text

    def test_changed_file_is_sent_in_full(self, db):
        course, assignment = _course_with_files('dd302', [('helper.py', STARTER)])
        edited = STARTER.replace('x * 2', 'x * 3')
        submission = _submission_with_files(course, assignment, [('helper.py', edited)])
        resolved = resolve_template('{assignment_files}\n{submission_file:helper.py}',
                                    VariableContext(course=course, assignment=assignment, submission=submission))
        assert 'x * 2' in resolved.text and 'x * 3' in resolved.text
        assert 'not repeated' not in resolved.text

    def test_first_occurrence_wins_regardless_of_order(self, db):
        course, assignment = _course_with_files('dd303', [('helper.py', STARTER)])
        submission = _submission_with_files(course, assignment, [('helper.py', STARTER)])
        resolved = resolve_template('{submission_files}\n{assignment_file:helper.py}',
                                    VariableContext(course=course, assignment=assignment, submission=submission))
        assert resolved.text.count(STARTER.strip()) == 1
        assert resolved.text.index('```') < resolved.text.index('not repeated')

    def test_unchanged_pdf_is_extracted_once(self, db):
        import pymupdf
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), 'Compute the covariance matrix of the dataset.')
        pdf_uri = 'data:application/pdf;base64,' + base64.b64encode(doc.tobytes()).decode()
        doc.close()
        course, assignment = _course_with_files('dd304', [('spec.pdf', pdf_uri)])
        submission = _submission_with_files(course, assignment, [('spec.pdf', pdf_uri)])
        resolved = resolve_template('{assignment_file:spec.pdf}\n{submission_files}',
                                    VariableContext(course=course, assignment=assignment, submission=submission))
        assert resolved.text.count('Compute the covariance matrix') == 1
        assert "### spec.pdf\n(identical to 'spec.pdf' above — not repeated)" in resolved.text

    def test_demo_files_dedup_in_previews(self, db):
        course, assignment = _course_with_files('dd305', [('helper.py', STARTER)])
        ctx = VariableContext(course=course, assignment=assignment,
                              demo_files=({'name': 'helper.py', 'content': STARTER},))
        resolved = resolve_template('{assignment_files}\n{submission_files}', ctx)
        assert resolved.text.count(STARTER.strip()) == 1

    def test_separate_resolutions_do_not_share_memory(self, db):
        course, assignment = _course_with_files('dd306', [('helper.py', STARTER)])
        ctx = VariableContext(course=course, assignment=assignment)
        first = resolve_template('{assignment_files}', ctx)
        second = resolve_template('{assignment_files}', ctx)
        assert first.text == second.text
        assert 'not repeated' not in second.text
