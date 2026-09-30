# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Prompt Variable Registry — instructor-facing {variables} for prompt templates.

Instructor-authored prompt text (e.g. QuizGeneratedSection.systemPrompt) may contain
tokens like ``{assignment_name}``, ``{assignment_file:main.py}`` or ``{submission_files}``
that are resolved server-side at generation time. This module is the single source of
truth for those variables:

* ``substitute_variables()`` — resolve tokens in a template (generation time; graceful:
  an unresolvable token becomes a visible ``(unavailable: …)`` marker, an unknown token
  passes through untouched so literal braces are safe).
* ``validate_template()`` — strict checking at save time (unknown variable, missing or
  bad argument, variable that needs an attached assignment) so instructors get a 400
  with a helpful message instead of silent degradation.
* ``describe_available_variables()`` — the payload behind autocomplete editors
  (e.g. GET /quizzes/{id}/promptVariables/): parameterized variables expand into one
  entry per concrete argument (one per assignment file), token pre-built.

Substitution is regex-based, NOT ``str.format`` — instructor text and resolved file
contents may contain arbitrary braces.

NOTE: resolvers import models/services lazily to avoid circular imports (this package
is imported during ``core.models`` load via the prompt registry).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from core.models import Course, Assignment, Submission, QuizGeneratedSection

logger = logging.getLogger(__name__)

# {name} or {name:argument} — name is a lowercase identifier; the argument may not
# contain braces or newlines (so literal JSON like {"a": 1} never matches).
TOKEN_RE = re.compile(r'\{([a-z][a-z0-9_]*)(?::([^{}\n]+))?\}')

# Per-file / total character caps, matching the existing AI context collection.
ASSIGNMENT_FILE_CHAR_CAP = 15000     # as in AIService.generate_quiz_questions
SUBMISSION_FILE_CHAR_CAP = 50000     # as in AIService._collect_submission_context
COURSE_FILE_CHAR_CAP = 15000         # course-level reference files (mirrors assignment files)
# The assigned dataset variant IS the core content of a retasking prompt, so it gets a
# larger cap than a generic assignment file — a 15k cap silently clipped most real CSVs.
STUDENT_DATASET_CHAR_CAP = 40000


@dataclass(frozen=True)
class VariableContext:
    """What a variable may draw on. At authoring/validation time only ``course`` and
    ``assignment`` are set; at generation time ``submission`` and ``section`` are too."""
    course: 'Course'
    assignment: 'Optional[Assignment]' = None
    submission: 'Optional[Submission]' = None
    section: 'Optional[QuizGeneratedSection]' = None
    # Instructor-supplied stand-in for a real submission in prompt test previews: a
    # tuple of {'name', 'content'} dicts, or None outside previews. Only consulted by
    # the submission-dependent resolvers when ``submission`` is None.
    demo_files: 'Optional[tuple]' = None


@dataclass(frozen=True)
class PromptVariable:
    """A registered template variable.

    ``resolver(context, argument)`` returns the replacement text, or ``None`` when the
    variable can't be resolved in this context (rendered as an ``(unavailable: …)``
    marker). ``list_arguments(context)`` powers autocomplete for parameterized
    variables; ``validate_argument(context, argument)`` returns an error message for a
    bad argument at save time (or ``None``)."""
    name: str
    label: str
    description: str
    resolver: Callable[[VariableContext, Optional[str]], Optional[str]]
    takes_argument: bool = False
    list_arguments: Optional[Callable[[VariableContext], list[dict]]] = None
    validate_argument: Optional[Callable[[VariableContext, str], Optional[str]]] = None
    requires: frozenset[str] = field(default_factory=frozenset)  # e.g. {'assignment'}


class PromptVariableRegistry:
    """Global registry of prompt template variables."""

    def __init__(self) -> None:
        self._entries: dict[str, PromptVariable] = {}

    def register(self, variable: PromptVariable) -> None:
        if variable.name in self._entries:
            raise ValueError(f"Prompt variable '{variable.name}' is already registered.")
        self._entries[variable.name] = variable

    def get(self, name: str) -> Optional[PromptVariable]:
        return self._entries.get(name)

    def all(self) -> list[PromptVariable]:
        return list(self._entries.values())


# Module-level singleton
prompt_variable_registry = PromptVariableRegistry()


def substitute_variables(template: str, context: VariableContext) -> tuple[str, set[str]]:
    """Resolve all registered {variable} tokens in ``template``.

    Returns ``(text, used_names)`` where ``used_names`` is the set of registered
    variable names that appeared (resolved or not). Unknown tokens pass through
    untouched; a registered token that can't resolve becomes ``(unavailable: <token>)``.
    """
    used: set[str] = set()

    def _sub(m: re.Match) -> str:
        name, argument = m.group(1), m.group(2)
        variable = prompt_variable_registry.get(name)
        if variable is None:
            return m.group(0)
        used.add(name)
        value = None
        if variable.takes_argument == bool(argument):
            try:
                value = variable.resolver(context, argument)
            except Exception:
                logger.exception("Prompt variable '%s' failed to resolve", m.group(0))
        return value if value is not None else f"(unavailable: {m.group(0)})"

    return TOKEN_RE.sub(_sub, template), used


def validate_template(template: str, context: VariableContext) -> list[str]:
    """Strictly validate a template at save time. Returns a list of error messages
    (empty when valid). Unlike substitution, unknown variables are errors here."""
    errors: list[str] = []
    seen: set[str] = set()
    for m in TOKEN_RE.finditer(template):
        token = m.group(0)
        if token in seen:
            continue
        seen.add(token)
        name, argument = m.group(1), m.group(2)
        variable = prompt_variable_registry.get(name)
        if variable is None:
            errors.append(f"Unknown variable '{token}'.")
            continue
        if 'assignment' in variable.requires and context.assignment is None:
            errors.append(f"'{token}' requires the quiz to be attached to an assignment.")
            continue
        if variable.takes_argument and not argument:
            errors.append(f"'{token}' needs an argument, e.g. {{{name}:filename}}.")
            continue
        if not variable.takes_argument and argument:
            errors.append(f"'{name}' does not take an argument (got '{token}').")
            continue
        if argument and variable.validate_argument is not None:
            error = variable.validate_argument(context, argument)
            if error:
                errors.append(error)
    return errors


def template_requirements(template: str) -> set[str]:
    """The union of ``requires`` over the registered variables used in ``template``
    (e.g. {'assignment', 'submission'}). Drives generation timing: a prompt with no
    'submission' requirement can generate before (or without) any submission, and one
    with no 'assignment' requirement is usable on standalone quizzes."""
    requirements: set[str] = set()
    for m in TOKEN_RE.finditer(template):
        variable = prompt_variable_registry.get(m.group(1))
        if variable is not None:
            requirements |= variable.requires
    return requirements


def template_requires_submission(template: str) -> bool:
    """Whether any variable in ``template`` draws on a student's submission."""
    return 'submission' in template_requirements(template)


def referenced_course_files(template: str) -> set[str]:
    """The set of course-file names a template references via ``{course_file:name}``.
    Used by cross-course cloning to carry along only the course files a prompt needs."""
    names: set[str] = set()
    for m in TOKEN_RE.finditer(template):
        if m.group(1) == 'course_file' and m.group(2):
            names.add(m.group(2))
    return names


def describe_available_variables(context: VariableContext) -> list[dict]:
    """The autocomplete payload: one entry per usable token in this context.

    Static variables yield one entry; parameterized variables expand via
    ``list_arguments`` (e.g. one entry per assignment file, token pre-built).
    """
    entries: list[dict] = []
    for variable in prompt_variable_registry.all():
        if 'assignment' in variable.requires and context.assignment is None:
            continue
        if not variable.takes_argument:
            entries.append({
                'token': f'{{{variable.name}}}',
                'name': variable.name,
                'argument': None,
                'label': variable.label,
                'description': variable.description,
                'kind': 'static',
            })
        elif variable.list_arguments is not None:
            for arg in variable.list_arguments(context):
                entries.append({
                    'token': f'{{{variable.name}:{arg["argument"]}}}',
                    'name': variable.name,
                    'argument': arg['argument'],
                    'label': f'{variable.label}: {arg.get("label", arg["argument"])}',
                    'description': variable.description,
                    'kind': 'file',
                })
    return entries


# --------------------------------------------------------------------------- #
# Built-in variables
# --------------------------------------------------------------------------- #

def _visible_assignment_files(assignment):
    return assignment.files.filter(hidden=False, is_test_resource=False)


def _file_content_for_prompt(name: str, data: str) -> str:
    """Turn a stored File.data into readable prompt text based on its type: PDFs are
    extracted to markdown (never emitted as raw base64), notebooks to enumerated cells,
    everything else passed through. Caps are applied afterward by _format_file_block."""
    lower = name.lower()
    if lower.endswith('.pdf'):
        from core.services.ai_service import extract_pdf_text
        return extract_pdf_text(data) or f"(could not extract text from PDF '{name}')"
    if lower.endswith('.ipynb'):
        from core.services.ai_service import _format_notebook_as_cells
        return _format_notebook_as_cells(data)
    if data.startswith('data:'):
        # Other binary files (images, ...) — never emit raw base64 into a prompt.
        return f"(binary file '{name}' not shown)"
    return data


def _format_file_block(name: str, content: str, cap: int) -> str:
    content = _file_content_for_prompt(name, content)
    if len(content) > cap:
        content = content[:cap] + "\n... (truncated)"
    return f"### {name}\n```\n{content}\n```"


def _resolve_assignment_name(ctx, argument):
    return ctx.assignment.name if ctx.assignment is not None else None


def _resolve_assignment_description(ctx, argument):
    if ctx.assignment is None:
        return None
    parts = []
    if ctx.assignment.ai_description:
        parts.append(ctx.assignment.ai_description)
    if ctx.assignment.explanation:
        parts.append(ctx.assignment.explanation)
    return "\n\n".join(parts) if parts else "(no assignment description)"


def _resolve_assignment_files(ctx, argument):
    if ctx.assignment is None:
        return None
    blocks = [_format_file_block(af.name, af.data, ASSIGNMENT_FILE_CHAR_CAP)
              for af in _visible_assignment_files(ctx.assignment)]
    return "\n\n".join(blocks) if blocks else "(no assignment files)"


def _resolve_assignment_file(ctx, argument):
    if ctx.assignment is None:
        return None
    af = _visible_assignment_files(ctx.assignment).filter(name=argument).first()
    if af is None:
        return None
    return _format_file_block(af.name, af.data, ASSIGNMENT_FILE_CHAR_CAP)


def _list_assignment_file_arguments(ctx):
    if ctx.assignment is None:
        return []
    return [{'argument': name, 'label': name}
            for name in _visible_assignment_files(ctx.assignment).values_list('name', flat=True)]


def _validate_assignment_file_argument(ctx, argument):
    if ctx.assignment is None:
        return None
    if not _visible_assignment_files(ctx.assignment).filter(name=argument).exists():
        return (f"'{{assignment_file:{argument}}}': the assignment has no file named "
                f"'{argument}'.")
    return None


# Course files are course-level reference material (CourseFile via course.files). Unlike
# AssignmentFile, CourseFile has no hidden/is_test_resource fields, so no filtering — and
# no 'assignment' requirement, so these resolve on any quiz (attached or standalone).
def _resolve_course_file(ctx, argument):
    if ctx.course is None:
        return None
    cf = ctx.course.files.filter(name=argument).select_related('content').first()
    if cf is None:
        return None
    return _format_file_block(cf.name, cf.content.data, COURSE_FILE_CHAR_CAP)


def _list_course_file_arguments(ctx):
    if ctx.course is None:
        return []
    return [{'argument': name, 'label': name}
            for name in ctx.course.files.values_list('name', flat=True)]


def _validate_course_file_argument(ctx, argument):
    if ctx.course is None:
        return None
    if not ctx.course.files.filter(name=argument).exists():
        return f"'{{course_file:{argument}}}': the course has no file named '{argument}'."
    return None


def _resolve_test_cases(ctx, argument):
    if ctx.assignment is None:
        return None
    parts = []
    for tc in ctx.assignment.testCategories.all():
        for test in tc.testCases.all():
            desc = getattr(test, 'description', '') or ''
            parts.append(f"- {desc or test.text[:100]}")
    return "Test cases:\n" + "\n".join(parts) if parts else "(no test cases defined)"


def _resolve_rubric(ctx, argument):
    if ctx.assignment is None:
        return None
    from core.models import RubricCategory
    parts = []
    for category in RubricCategory.objects.filter(assignment=ctx.assignment).prefetch_related('rubricComments'):
        cat_text = f"### {category.name}\n"
        for rc in category.rubricComments.all():
            cat_text += f"  - {rc.name or rc.text[:60]}\n"
        parts.append(cat_text)
    return "Rubric:\n" + "\n".join(parts) if parts else "(no rubric defined)"


def _submission_context(ctx):
    from core.services.ai_service import AIService
    return AIService._collect_submission_context(ctx.submission)


def _resolve_submission_files(ctx, argument):
    if ctx.submission is None:
        if ctx.demo_files is not None:
            return "\n\n".join(_format_file_block(f['name'], f['content'], SUBMISSION_FILE_CHAR_CAP)
                               for f in ctx.demo_files) or "(the submission has no files)"
        return None
    blocks = [f"### {f['name']}\n```\n{f['content']}\n```" for f in _submission_context(ctx)['files']]
    return "\n\n".join(blocks) if blocks else "(the submission has no files)"


def _resolve_submission_file(ctx, argument):
    if ctx.submission is None:
        if ctx.demo_files is not None:
            df = next((f for f in ctx.demo_files if f['name'] == argument), None)
            if df is None:
                return f"(no file named '{argument}' in this submission)"
            return _format_file_block(df['name'], df['content'], SUBMISSION_FILE_CHAR_CAP)
        return None
    sf = ctx.submission.files.filter(name=argument).first()
    if sf is None:
        return f"(no file named '{argument}' in this submission)"
    return _format_file_block(sf.name, sf.data, SUBMISSION_FILE_CHAR_CAP)


def _list_submission_file_arguments(ctx):
    # Student file names vary — offer the assignment's expected file names, required first.
    if ctx.assignment is None:
        return []
    files = _visible_assignment_files(ctx.assignment).order_by('-required', 'name')
    return [{'argument': name, 'label': name} for name in files.values_list('name', flat=True)]


def _resolve_submission_test_results(ctx, argument):
    if ctx.submission is None:
        if ctx.demo_files is not None:
            return "(test results are not available in test previews)"
        return None
    return _submission_context(ctx)['test_results'] or "(no test results)"


def _resolve_student_dataset(ctx, argument):
    # The submitting student's assigned dataset variant (per-student pool) — resolved per
    # student at generation time, like {submission_files}. Datasets use a real FileField
    # (binary storage), so this decodes as text rather than reusing _format_file_block's
    # File.data path.
    if ctx.assignment is None:
        return None
    if ctx.submission is not None:
        from core.services.dataset_assignment import get_or_assign_for_submission
        dataset = get_or_assign_for_submission(ctx.assignment, ctx.submission)
    elif ctx.demo_files is not None:
        # Test previews have no submission to key an assignment row on — pick a random
        # variant read-only so the example still shows a realistic dataset.
        from core.services.dataset_assignment import random_variant
        dataset = random_variant(ctx.assignment)
    else:
        return None
    if dataset is None or not dataset.file:
        return None
    try:
        with dataset.file.open('rb') as f:
            content = f.read().decode('utf-8', errors='replace')
    except Exception:
        return "(could not read the assigned dataset file)"
    filename = dataset.file.name.split('/')[-1]
    if len(content) > STUDENT_DATASET_CHAR_CAP:
        # Truncation is silent to the model (just "... (truncated)"); log so a variant that's
        # too big to fully fit the prompt is diagnosable, and surfaced at authoring time via
        # QuizGeneratedSectionSerializer.datasetTruncationWarning.
        logger.warning(
            "[student_dataset] Variant '%s' (dataset %s, %d chars) exceeds the %d-char cap "
            "and will be truncated in the generation prompt.",
            filename, dataset.id, len(content), STUDENT_DATASET_CHAR_CAP)
    return _format_file_block(filename, content, STUDENT_DATASET_CHAR_CAP)


def _resolve_num_questions(ctx, argument):
    return str(ctx.section.numQuestions) if ctx.section is not None else None


def _resolve_question_types(ctx, argument):
    if ctx.section is None:
        return None
    return ", ".join(ctx.section.questionTypes) if ctx.section.questionTypes else "(any type)"


for _variable in [
    PromptVariable(
        name='assignment_name', label='Assignment name',
        description='The name of the attached assignment.',
        resolver=_resolve_assignment_name, requires=frozenset({'assignment'})),
    PromptVariable(
        name='assignment_description', label='Assignment description',
        description="The assignment's description and student-facing instructions.",
        resolver=_resolve_assignment_description, requires=frozenset({'assignment'})),
    PromptVariable(
        name='assignment_files', label='All assignment files',
        description='The contents of every student-visible assignment file.',
        resolver=_resolve_assignment_files, requires=frozenset({'assignment'})),
    PromptVariable(
        name='assignment_file', label='Assignment file',
        description='The contents of one named assignment file.',
        resolver=_resolve_assignment_file, takes_argument=True,
        list_arguments=_list_assignment_file_arguments,
        validate_argument=_validate_assignment_file_argument,
        requires=frozenset({'assignment'})),
    PromptVariable(
        name='course_file', label='Course file',
        description='The contents of one course-level file (usable on any quiz, attached or not).',
        resolver=_resolve_course_file, takes_argument=True,
        list_arguments=_list_course_file_arguments,
        validate_argument=_validate_course_file_argument),
    PromptVariable(
        name='test_cases', label='Test cases',
        description="Descriptions of the assignment's test cases.",
        resolver=_resolve_test_cases, requires=frozenset({'assignment'})),
    PromptVariable(
        name='rubric', label='Rubric',
        description="The assignment's grading rubric.",
        resolver=_resolve_rubric, requires=frozenset({'assignment'})),
    PromptVariable(
        name='submission_files', label="All the student's submitted files",
        description="The contents of every file in the student's submission "
                    '(resolved per student at generation time).',
        resolver=_resolve_submission_files, requires=frozenset({'assignment', 'submission'})),
    PromptVariable(
        name='submission_file', label='Submitted file',
        description="One named file from the student's submission "
                    '(resolved per student at generation time).',
        resolver=_resolve_submission_file, takes_argument=True,
        list_arguments=_list_submission_file_arguments,
        requires=frozenset({'assignment', 'submission'})),
    PromptVariable(
        name='submission_test_results', label="The student's test results",
        description="The student's autograder test results "
                    '(resolved per student at generation time).',
        resolver=_resolve_submission_test_results, requires=frozenset({'assignment', 'submission'})),
    PromptVariable(
        name='student_dataset', label="The student's assigned dataset",
        description="The contents of the dataset variant assigned to this student, for "
                    'assignments with a per-student dataset pool '
                    '(resolved per student at generation time).',
        resolver=_resolve_student_dataset, requires=frozenset({'assignment', 'submission'})),
    PromptVariable(
        name='num_questions', label='Number of questions',
        description="This section's configured question count.",
        resolver=_resolve_num_questions),
    PromptVariable(
        name='question_types', label='Question types',
        description="This section's configured question types.",
        resolver=_resolve_question_types),
]:
    prompt_variable_registry.register(_variable)
