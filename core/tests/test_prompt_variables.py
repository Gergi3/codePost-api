# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Tests for the prompt variable framework (core/prompts/variables.py): regex
substitution semantics, strict save-time validation, and the autocomplete payload."""
import factory
import pytest
from django.db.models.signals import post_save

from core.prompts.variables import (
    VariableContext, describe_available_variables, substitute_variables, validate_template,
)


@pytest.fixture
def assignment_setup(db):
    from core.tests.factories import CourseFactory, AssignmentFileFactory

    with factory.django.mute_signals(post_save):
        course = CourseFactory(name="cos126", period="s2026", organization__name="Princeton")
        assignment = course.assignments.first()
        AssignmentFileFactory(assignment=assignment, name='starter.py', data='print("hi")')
        AssignmentFileFactory(assignment=assignment, name='secret.py', data='key', hidden=True)
    return {'course': course, 'assignment': assignment}


@pytest.fixture
def course_file_setup(db):
    from core.models import CourseFile
    from core.tests.factories import CourseFactory

    with factory.django.mute_signals(post_save):
        course = CourseFactory(name="cos226", period="s2026", organization__name="Princeton")
        CourseFile.objects.create(course=course, name='style.md', data='Use camelCase.',
                                  extension='.md')
    return {'course': course}


class TestSubstitution:
    def test_literal_braces_pass_through(self):
        ctx = VariableContext(course=None)
        text, used = substitute_variables('Return JSON like {"a": 1} or {}.', ctx)
        assert text == 'Return JSON like {"a": 1} or {}.'
        assert used == set()

    def test_unknown_variable_passes_through(self):
        ctx = VariableContext(course=None)
        text, used = substitute_variables('keep {not_a_real_variable} as-is', ctx)
        assert text == 'keep {not_a_real_variable} as-is'
        assert used == set()

    def test_unresolvable_variable_becomes_marker(self):
        # assignment_name is registered but there's no assignment in context.
        ctx = VariableContext(course=None)
        text, used = substitute_variables('for {assignment_name}!', ctx)
        assert text == 'for (unavailable: {assignment_name})!'
        assert used == {'assignment_name'}

    def test_static_and_parameterized_resolution(self, assignment_setup):
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'])
        text, used = substitute_variables(
            'A: {assignment_name}\n{assignment_file:starter.py}', ctx)
        assert 'A: ' + assignment_setup['assignment'].name in text
        assert 'print("hi")' in text
        assert used == {'assignment_name', 'assignment_file'}

    def test_missing_file_argument_becomes_marker(self, assignment_setup):
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'])
        text, _ = substitute_variables('{assignment_file:nope.py}', ctx)
        assert text == '(unavailable: {assignment_file:nope.py})'

    def test_hidden_files_never_resolve(self, assignment_setup):
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'])
        text, _ = substitute_variables('{assignment_file:secret.py}', ctx)
        assert 'key' not in text
        assert '(unavailable' in text

    def test_argument_on_static_variable_becomes_marker(self):
        ctx = VariableContext(course=None)
        text, _ = substitute_variables('{num_questions:5}', ctx)
        assert text == '(unavailable: {num_questions:5})'

    def test_course_file_resolves(self, course_file_setup):
        # Course files resolve from ctx.course alone — no assignment/submission needed.
        ctx = VariableContext(course=course_file_setup['course'])
        text, used = substitute_variables('Guide:\n{course_file:style.md}', ctx)
        assert 'Use camelCase.' in text
        assert used == {'course_file'}

    def test_missing_course_file_becomes_marker(self, course_file_setup):
        ctx = VariableContext(course=course_file_setup['course'])
        text, _ = substitute_variables('{course_file:nope.md}', ctx)
        assert text == '(unavailable: {course_file:nope.md})'

    def test_section_variables(self, assignment_setup):
        from core.models import Quiz, QuizGeneratedSection
        quiz = Quiz.objects.create(course=assignment_setup['course'], title='Q',
                                   assignment=assignment_setup['assignment'])
        section = QuizGeneratedSection.objects.create(
            quiz=quiz, systemPrompt='x', numQuestions=4, questionTypes=['essay'])
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'], section=section)
        text, _ = substitute_variables('{num_questions} of {question_types}', ctx)
        assert text == '4 of essay'


class TestFileContentConversion:
    """PDF and notebook file variables resolve to readable text/cells, never raw base64."""

    def _pdf_data_uri(self, text):
        import base64
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), text)
        pdf_bytes = doc.tobytes()
        doc.close()
        return 'data:application/pdf;base64,' + base64.b64encode(pdf_bytes).decode()

    def test_assignment_pdf_resolves_to_text_not_base64(self, db):
        from core.tests.factories import CourseFactory, AssignmentFileFactory
        with factory.django.mute_signals(post_save):
            course = CourseFactory(name="pdf101", period="s2026", organization__name="Rutgers")
            assignment = course.assignments.first()
            AssignmentFileFactory(assignment=assignment, name='spec.pdf',
                                  data=self._pdf_data_uri('Compute the z-score.'), extension='.pdf')
        ctx = VariableContext(course=course, assignment=assignment)
        text, used = substitute_variables('Spec:\n{assignment_file:spec.pdf}', ctx)
        assert 'assignment_file' in used
        assert 'Compute the z-score.' in text
        assert 'base64' not in text
        assert 'data:application/pdf' not in text

    def test_bad_pdf_becomes_placeholder_not_base64(self, db):
        from core.tests.factories import CourseFactory, AssignmentFileFactory
        with factory.django.mute_signals(post_save):
            course = CourseFactory(name="pdf102", period="s2026", organization__name="Rutgers")
            assignment = course.assignments.first()
            # A file named .pdf whose bytes aren't a real PDF — extraction fails gracefully.
            AssignmentFileFactory(assignment=assignment, name='broken.pdf',
                                  data='not actually a pdf', extension='.pdf')
        ctx = VariableContext(course=course, assignment=assignment)
        text, _ = substitute_variables('{assignment_file:broken.pdf}', ctx)
        assert "could not extract text from PDF 'broken.pdf'" in text
        assert 'not actually a pdf' not in text or 'could not extract' in text

    def test_assignment_binary_becomes_placeholder_not_base64(self, db):
        # A non-image binary (no vision input possible) — see test_prompt_images.py for images.
        import base64
        from core.tests.factories import CourseFactory, AssignmentFileFactory
        jar = 'data:application/java-archive;base64,' + base64.b64encode(b'PK\x03\x04' + b'\x00' * 16).decode()
        with factory.django.mute_signals(post_save):
            course = CourseFactory(name="img101", period="s2026", organization__name="Rutgers")
            assignment = course.assignments.first()
            AssignmentFileFactory(assignment=assignment, name='lib.jar', data=jar, extension='.jar')
        ctx = VariableContext(course=course, assignment=assignment)
        text, _ = substitute_variables('{assignment_file:lib.jar}', ctx)
        assert "binary file 'lib.jar' not shown" in text
        assert 'base64' not in text

    def test_course_pdf_resolves_to_text(self, db):
        from core.models import CourseFile
        from core.tests.factories import CourseFactory
        with factory.django.mute_signals(post_save):
            course = CourseFactory(name="pdf103", period="s2026", organization__name="Rutgers")
            CourseFile.objects.create(course=course, name='rubric.pdf',
                                      data=self._pdf_data_uri('Grade on clarity.'), extension='.pdf')
        ctx = VariableContext(course=course)
        text, _ = substitute_variables('{course_file:rubric.pdf}', ctx)
        assert 'Grade on clarity.' in text
        assert 'base64' not in text

    def test_notebook_assignment_file_resolves_to_cells(self, db):
        import json
        from core.tests.factories import CourseFactory, AssignmentFileFactory
        notebook = json.dumps({
            'cells': [
                {'cell_type': 'markdown', 'source': ['# Analysis']},
                {'cell_type': 'code', 'source': ['mean(shop$price)'], 'outputs': []},
            ],
            'metadata': {'kernelspec': {'language': 'R', 'name': 'ir'}},
            'nbformat': 4, 'nbformat_minor': 5,
        })
        with factory.django.mute_signals(post_save):
            course = CourseFactory(name="nb101", period="s2026", organization__name="Rutgers")
            assignment = course.assignments.first()
            AssignmentFileFactory(assignment=assignment, name='hw.ipynb', data=notebook,
                                  extension='.ipynb')
        ctx = VariableContext(course=course, assignment=assignment)
        text, _ = substitute_variables('{assignment_file:hw.ipynb}', ctx)
        assert '--- CELL 1 [MARKDOWN] ---' in text
        assert '--- CELL 2 [CODE] ---' in text
        assert 'mean(shop$price)' in text


class TestValidation:
    def test_valid_template(self, assignment_setup):
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'])
        errors = validate_template(
            'Ask about {assignment_file:starter.py} and {submission_files}. JSON: {"a": 1}', ctx)
        assert errors == []

    def test_unknown_variable_is_error(self, assignment_setup):
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'])
        errors = validate_template('{bogus_variable}', ctx)
        assert errors == ["Unknown variable '{bogus_variable}'."]

    def test_missing_argument_is_error(self, assignment_setup):
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'])
        errors = validate_template('{assignment_file}', ctx)
        assert len(errors) == 1 and 'needs an argument' in errors[0]

    def test_argument_on_static_variable_is_error(self, assignment_setup):
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'])
        errors = validate_template('{assignment_name:foo}', ctx)
        assert len(errors) == 1 and 'does not take an argument' in errors[0]

    def test_unknown_assignment_file_is_error(self, assignment_setup):
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'])
        errors = validate_template('{assignment_file:missing.py}', ctx)
        assert len(errors) == 1 and 'no file named' in errors[0]

    def test_assignment_variable_without_assignment_is_error(self):
        ctx = VariableContext(course=None)
        errors = validate_template('{submission_files}', ctx)
        assert len(errors) == 1 and 'attached to an assignment' in errors[0]

    def test_duplicate_tokens_error_once(self, assignment_setup):
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'])
        errors = validate_template('{bogus} then {bogus}', ctx)
        assert len(errors) == 1

    def test_unknown_course_file_is_error(self, course_file_setup):
        ctx = VariableContext(course=course_file_setup['course'])
        errors = validate_template('{course_file:missing.md}', ctx)
        assert len(errors) == 1 and 'no file named' in errors[0]

    def test_course_file_valid_without_assignment(self, course_file_setup):
        # A known course file validates even with no attached assignment — the whole point
        # is that course files work on standalone quizzes.
        ctx = VariableContext(course=course_file_setup['course'], assignment=None)
        assert validate_template('Refer to {course_file:style.md}.', ctx) == []


class TestDescribeAvailableVariables:
    def test_expands_assignment_files(self, assignment_setup):
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'])
        entries = describe_available_variables(ctx)
        tokens = {e['token'] for e in entries}
        assert '{assignment_name}' in tokens
        assert '{submission_files}' in tokens
        assert '{assignment_file:starter.py}' in tokens
        assert '{submission_file:starter.py}' in tokens
        # Hidden files are never offered.
        assert '{assignment_file:secret.py}' not in tokens
        kinds = {e['token']: e['kind'] for e in entries}
        assert kinds['{assignment_file:starter.py}'] == 'file'
        assert kinds['{assignment_name}'] == 'static'

    def test_without_assignment_only_section_variables(self):
        entries = describe_available_variables(VariableContext(course=None))
        tokens = {e['token'] for e in entries}
        assert tokens == {'{num_questions}', '{question_types}'}

    def test_expands_course_files(self, course_file_setup):
        # Course files expand per-file (kind 'file') and appear with no attached assignment.
        entries = describe_available_variables(VariableContext(course=course_file_setup['course']))
        by_token = {e['token']: e['kind'] for e in entries}
        assert by_token.get('{course_file:style.md}') == 'file'
        assert '{assignment_file:starter.py}' not in by_token


class TestTemplateRequirements:
    def test_classification_drives_generation_timing(self):
        from core.prompts.variables import template_requirements, template_requires_submission
        # Section-only / plain prompts need nothing → eager, standalone-capable.
        assert template_requirements('Ask {num_questions} things about recursion.') == set()
        assert template_requires_submission('Plain prompt, no variables.') is False
        # Assignment data needs attachment but not a submission.
        assert template_requirements('Use {assignment_name} and {assignment_files}.') == {'assignment'}
        assert template_requires_submission('{assignment_file:main.py}') is False
        # Per-student submission data needs both.
        assert template_requirements('Read {submission_files}.') == {'assignment', 'submission'}
        assert template_requires_submission('{submission_file:main.py}') is True
        assert template_requires_submission('{submission_test_results}') is True
        # Course files require nothing → keep prompts on the eager, standalone-capable path.
        assert template_requirements('Use {course_file:style.md}.') == set()
        assert template_requires_submission('{course_file:style.md}') is False


class TestDemoFileContext:
    """Instructor demo files stand in for a real submission in prompt test previews."""

    DEMO = ({'name': 'main.py', 'content': 'def solve():\n    return 42\n'},
            {'name': 'notes.txt', 'content': 'my approach'},)

    def _ctx(self, assignment_setup, **kwargs):
        return VariableContext(course=assignment_setup['course'],
                               assignment=assignment_setup['assignment'],
                               demo_files=self.DEMO, **kwargs)

    def test_submission_files_resolve_from_demo_files(self, assignment_setup):
        text, _ = substitute_variables('{submission_files}', self._ctx(assignment_setup))
        assert '### main.py' in text and 'return 42' in text
        assert '### notes.txt' in text and 'my approach' in text

    def test_submission_file_matches_by_name(self, assignment_setup):
        ctx = self._ctx(assignment_setup)
        text, _ = substitute_variables('{submission_file:main.py}', ctx)
        assert '### main.py' in text and 'return 42' in text
        text, _ = substitute_variables('{submission_file:absent.py}', ctx)
        assert text == "(no file named 'absent.py' in this submission)"

    def test_test_results_placeholder(self, assignment_setup):
        text, _ = substitute_variables('{submission_test_results}', self._ctx(assignment_setup))
        assert text == '(test results are not available in test previews)'

    def test_student_dataset_picks_variant_without_persisting(self, assignment_setup):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from core.models import AssignmentDataSet, StudentDataSetAssignment
        AssignmentDataSet.objects.create(
            assignment=assignment_setup['assignment'], name='shopping_a.csv',
            is_student_variant=True,
            file=SimpleUploadedFile('shopping_a.csv', b'a,b\n1,2\n'))
        text, _ = substitute_variables('{student_dataset}', self._ctx(assignment_setup))
        assert 'a,b' in text
        # The read-only invariant: previews must never assign a variant to anyone.
        assert StudentDataSetAssignment.objects.count() == 0

    def test_without_demo_files_behavior_is_unchanged(self, assignment_setup):
        ctx = VariableContext(course=assignment_setup['course'],
                              assignment=assignment_setup['assignment'])
        text, _ = substitute_variables('{submission_files}', ctx)
        assert text == '(unavailable: {submission_files})'
