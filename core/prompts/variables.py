# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Prompt Variable Registry — instructor-facing {variables} for prompt templates.

Instructor-authored prompt text (e.g. QuizGeneratedSection.systemPrompt) may contain
tokens like ``{assignment_name}``, ``{assignment_file:main.py}`` or ``{submission_files}``
that are resolved server-side at generation time. This module is the single source of
truth for those variables:

* ``resolve_template()`` — resolve tokens in a template (generation time; graceful:
  an unresolvable token becomes a visible ``(unavailable: …)`` marker, an unknown token
  passes through untouched so literal braces are safe). Image files referenced by a
  variable become ``ImageAttachment``s for the model's vision input, with a short
  ``(image attached: …)`` marker left in the text. ``substitute_variables()`` is the
  text-only view of the same result.
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

import hashlib
import logging
import re
from dataclasses import dataclass, field, replace
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
# Image types every supported provider accepts as vision input (SVG is not one of them).
PROMPT_IMAGE_MIMES = frozenset({'image/png', 'image/jpeg', 'image/gif', 'image/webp'})
# Hard cap on attached images per prompt — each one costs hundreds to >1k input tokens.
MAX_PROMPT_IMAGES = 10


@dataclass(frozen=True)
class ImageAttachment:
    """An image a variable pulled out of a stored ``File.data`` data URI, for the
    provider's vision input (``AIService._generate(images=...)``)."""
    name: str
    mime: str
    base64_data: str


@dataclass(frozen=True)
class Resolved:
    """A resolver result that carries images alongside its text."""
    text: str
    images: tuple[ImageAttachment, ...] = ()


@dataclass(frozen=True)
class ResolvedTemplate:
    text: str
    used: set[str]
    images: tuple[ImageAttachment, ...]


class _ContentDedup:
    """Per-resolution memory of file contents already emitted, so a file that appears
    twice in one prompt (typically an unchanged starter file showing up in both
    {assignment_files} and {submission_files}) is sent once and pointed to after."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def first_name_for(self, name: str, data: str) -> Optional[str]:
        """The name this content was first emitted under, or None (and record it now).
        Trivially small content is never deduplicated."""
        if len(data.strip()) < 40:
            return None
        digest = hashlib.sha256(data.encode('utf-8')).hexdigest()
        if digest in self._seen:
            return self._seen[digest]
        self._seen[digest] = name
        return None


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
    # Set by resolve_template for the duration of one resolution (see _ContentDedup).
    dedup: Optional[_ContentDedup] = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class PromptVariable:
    """A registered template variable.

    ``resolver(context, argument)`` returns the replacement text (or a ``Resolved``
    carrying image attachments), or ``None`` when the variable can't be resolved in this
    context (rendered as an ``(unavailable: …)`` marker). ``list_arguments(context)`` powers autocomplete for parameterized
    variables; ``validate_argument(context, argument)`` returns an error message for a
    bad argument at save time (or ``None``)."""
    name: str
    label: str
    description: str
    resolver: Callable[[VariableContext, Optional[str]], 'Optional[str | Resolved]']
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


def resolve_template(template: str, context: VariableContext) -> ResolvedTemplate:
    """Resolve all registered {variable} tokens in ``template``.

    ``used`` is the set of registered variable names that appeared (resolved or not).
    Unknown tokens pass through untouched; a registered token that can't resolve becomes
    ``(unavailable: <token>)``. ``images`` are the attachments the variables produced,
    capped at MAX_PROMPT_IMAGES (extras are noted in the text). Identical file contents
    (by hash) are emitted or attached once per resolution; later occurrences become a
    short pointer to the first.
    """
    used: set[str] = set()
    images: list[ImageAttachment] = []
    context = replace(context, dedup=_ContentDedup())

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
        if value is None:
            return f"(unavailable: {m.group(0)})"
        if isinstance(value, str):
            return value
        text = value.text
        for image in value.images:
            if len(images) >= MAX_PROMPT_IMAGES:
                text += f"\n(image '{image.name}' omitted — at most {MAX_PROMPT_IMAGES} images per prompt)"
                continue
            images.append(image)
        return text

    text = TOKEN_RE.sub(_sub, template)
    return ResolvedTemplate(text=text, used=used, images=tuple(images))


def substitute_variables(template: str, context: VariableContext) -> tuple[str, set[str]]:
    """Text-only view of ``resolve_template``: ``(text, used_names)``."""
    resolved = resolve_template(template, context)
    return resolved.text, resolved.used


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


def _image_attachment(name: str, data: str) -> Optional[ImageAttachment]:
    """The ImageAttachment for a File.data that is a data URI of a provider-accepted
    image type, else None (text, PDFs, SVG, other binaries)."""
    if not data.startswith('data:'):
        return None
    header, sep, encoded = data.partition(',')
    if not sep or ';base64' not in header:
        return None
    mime = header[5:].split(';', 1)[0].strip().lower()
    if mime not in PROMPT_IMAGE_MIMES:
        return None
    return ImageAttachment(name=name, mime=mime, base64_data=encoded)


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
        # Other binary files — never emit raw base64 into a prompt.
        return f"(binary file '{name}' not shown)"
    return data


def _already_sent_as(ctx: Optional[VariableContext], name: str, data: str) -> Optional[str]:
    """The name this exact content was already emitted under earlier in the prompt, else
    None. No-op outside resolve_template (ctx None or no dedup)."""
    if ctx is None or ctx.dedup is None:
        return None
    return ctx.dedup.first_name_for(name, data)


def _format_file_block(name: str, content: str, cap: int, ctx: Optional[VariableContext] = None) -> str:
    first = _already_sent_as(ctx, name, content)
    if first is not None:
        return f"### {name}\n(identical to '{first}' above — not repeated)"
    content = _file_content_for_prompt(name, content)
    if len(content) > cap:
        content = content[:cap] + "\n... (truncated)"
    return f"### {name}\n```\n{content}\n```"


def _format_file(ctx: VariableContext, name: str, data: str, cap: int, attach_images: bool) -> Resolved:
    """A file as prompt content: an image becomes an attachment plus a marker when
    ``attach_images`` is set (and a pointer to the *_with_images variable when not);
    anything else is a text block."""
    image = _image_attachment(name, data)
    if image is None:
        return Resolved(_format_file_block(name, data, cap, ctx))
    if not attach_images:
        return Resolved(f"(image '{name}' omitted — use {{assignment_files_with_images}} to attach it)")
    first = _already_sent_as(ctx, name, data)
    if first is not None:
        return Resolved(f"(image '{name}' is identical to '{first}', already attached above)")
    return Resolved(f"(image attached: {name})", (image,))


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


def _resolve_assignment_files_impl(ctx, attach_images: bool):
    if ctx.assignment is None:
        return None
    parts = [_format_file(ctx, af.name, af.data, ASSIGNMENT_FILE_CHAR_CAP, attach_images)
             for af in _visible_assignment_files(ctx.assignment)]
    if not parts:
        return "(no assignment files)"
    images = tuple(img for part in parts for img in part.images)
    return Resolved("\n\n".join(part.text for part in parts), images)


def _resolve_assignment_files(ctx, argument):
    return _resolve_assignment_files_impl(ctx, attach_images=False)


def _resolve_assignment_files_with_images(ctx, argument):
    return _resolve_assignment_files_impl(ctx, attach_images=True)


def _resolve_assignment_file(ctx, argument):
    if ctx.assignment is None:
        return None
    af = _visible_assignment_files(ctx.assignment).filter(name=argument).first()
    if af is None:
        return None
    # Naming a file is opt-in, so an image named here is attached.
    return _format_file(ctx, af.name, af.data, ASSIGNMENT_FILE_CHAR_CAP, attach_images=True)


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
    return _format_file(ctx, cf.name, cf.content.data, COURSE_FILE_CHAR_CAP, attach_images=True)


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
            return "\n\n".join(_format_file_block(f['name'], f['content'], SUBMISSION_FILE_CHAR_CAP, ctx)
                               for f in ctx.demo_files) or "(the submission has no files)"
        return None
    blocks = []
    for f in _submission_context(ctx)['files']:
        # Content here is already extracted/capped; dedup on the raw data it came from.
        first = _already_sent_as(ctx, f['name'], f['data'])
        if first is not None:
            blocks.append(f"### {f['name']}\n(identical to '{first}' above — not repeated)")
        else:
            blocks.append(f"### {f['name']}\n```\n{f['content']}\n```")
    return "\n\n".join(blocks) if blocks else "(the submission has no files)"


def _resolve_submission_file(ctx, argument):
    if ctx.submission is None:
        if ctx.demo_files is not None:
            df = next((f for f in ctx.demo_files if f['name'] == argument), None)
            if df is None:
                return f"(no file named '{argument}' in this submission)"
            return _format_file_block(df['name'], df['content'], SUBMISSION_FILE_CHAR_CAP, ctx)
        return None
    sf = ctx.submission.files.filter(name=argument).first()
    if sf is None:
        return f"(no file named '{argument}' in this submission)"
    return _format_file_block(sf.name, sf.data, SUBMISSION_FILE_CHAR_CAP, ctx)


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
    return _format_file_block(filename, content, STUDENT_DATASET_CHAR_CAP, ctx)


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
        description='The contents of every student-visible assignment file (text only; '
                    'images are skipped).',
        resolver=_resolve_assignment_files, requires=frozenset({'assignment'})),
    PromptVariable(
        name='assignment_files_with_images', label='All assignment files, with images',
        description='The contents of every student-visible assignment file, with images '
                    '(PNG, JPEG, GIF, WebP) attached for the model to see.',
        resolver=_resolve_assignment_files_with_images, requires=frozenset({'assignment'})),
    PromptVariable(
        name='assignment_file', label='Assignment file',
        description='The contents of one named assignment file (an image is attached for '
                    'the model to see).',
        resolver=_resolve_assignment_file, takes_argument=True,
        list_arguments=_list_assignment_file_arguments,
        validate_argument=_validate_assignment_file_argument,
        requires=frozenset({'assignment'})),
    PromptVariable(
        name='course_file', label='Course file',
        description='The contents of one course-level file (usable on any quiz, attached or '
                    'not; an image is attached for the model to see).',
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
