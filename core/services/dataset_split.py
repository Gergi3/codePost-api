# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Split one master dataset file into a per-student variant pool.

Chunks are disjoint (every row goes to exactly one chunk) and sized by rows-per-chunk
rather than current enrollment, so the pool stays stable as students enroll or drop —
core.services.dataset_assignment.get_or_assign() balances students across whatever pool
exists, doubling up on a chunk only if enrollment ever exceeds the chunk count.
"""
import math

from django.core.files.base import ContentFile

from core.models import AssignmentDataSet

# A bad rows_per_chunk value (e.g. 1) shouldn't be able to explode a large file into
# thousands of tiny per-student rows.
MAX_SPLIT_CHUNKS = 200


class DatasetSplitError(Exception):
    """Raised for any reason a master dataset can't be split as requested — always a
    user-facing message, never an internal detail."""


def _chunk_name(stem: str, ext: str, index: int) -> str:
    return f"{stem}_variant_{index}{ext}"


def split_master_dataset(master: AssignmentDataSet, rows_per_chunk: int,
                         has_header: bool = True,
                         replace: bool = False) -> list[AssignmentDataSet]:
    """Split ``master``'s file into disjoint row-chunks, each becoming its own
    ``is_student_variant=True`` dataset sharing one mount_path (enforced by
    AssignmentDataSet.save()). The header row (if any) is repeated in every chunk.
    ``master`` itself is deactivated and hidden from students (not mounted, not in their
    download) and excluded from the pool — it's the whole file, not a per-student slice —
    but its data is kept, not deleted. Returns the created chunks.

    ``replace=True`` regenerates in place: this master's prior auto-generated variants
    (``<stem>_variant_*``) are deleted first instead of raising on the name collision. That
    delete cascades to StudentDataSetAssignment and SubmissionVariantRun, so every student's
    variant assignment is reset and they are reassigned on next access — callers must warn."""
    if rows_per_chunk < 1:
        raise DatasetSplitError("Rows per chunk must be at least 1.")
    if not master.file:
        raise DatasetSplitError("This dataset has no file to split.")

    with master.file.open('rb') as f:
        raw = f.read()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise DatasetSplitError("Only text files (e.g. CSV) can be split into variants.")

    lines = text.splitlines(keepends=True)
    if not lines:
        raise DatasetSplitError("The file is empty.")

    header = lines[0] if has_header else ''
    data_lines = lines[1:] if has_header else lines
    if not data_lines:
        raise DatasetSplitError("There are no data rows to split (only a header).")

    chunk_count = math.ceil(len(data_lines) / rows_per_chunk)
    if chunk_count > MAX_SPLIT_CHUNKS:
        raise DatasetSplitError(
            f"That would create {chunk_count} variants (max {MAX_SPLIT_CHUNKS}) — "
            "use a larger rows-per-chunk value.")

    dot = master.name.rfind('.')
    stem, ext = (master.name[:dot], master.name[dot:]) if dot > 0 else (master.name, '')

    if replace:
        # Regenerating in place: drop this master's prior auto-generated variants (any count,
        # not just the ones the new split would collide with, so a smaller split doesn't
        # leave stale higher-numbered variants). Cascades to student assignments + variant
        # runs — students are reassigned on next access.
        AssignmentDataSet.objects.filter(
            assignment=master.assignment, is_student_variant=True,
            name__startswith=f"{stem}_variant_").delete()
    else:
        existing_names = set(AssignmentDataSet.objects.filter(
            assignment=master.assignment,
            name__in=[_chunk_name(stem, ext, i + 1) for i in range(chunk_count)],
        ).values_list('name', flat=True))
        if existing_names:
            raise DatasetSplitError(
                "This assignment already has datasets named like the ones this split would "
                f"create ({', '.join(sorted(existing_names))}) — delete the previous split's "
                "variants first, or re-run with 'replace existing variants'.")

    created = []
    for i in range(chunk_count):
        chunk_lines = data_lines[i * rows_per_chunk:(i + 1) * rows_per_chunk]
        content = header + ''.join(chunk_lines)
        chunk_name = _chunk_name(stem, ext, i + 1)
        chunk = AssignmentDataSet(
            assignment=master.assignment,
            name=chunk_name,
            description=f"Auto-generated variant {i + 1}/{chunk_count} of '{master.name}'.",
            is_active=True,
            is_student_variant=True,
        )
        chunk.file.save(chunk_name, ContentFile(content.encode('utf-8')), save=False)
        chunk.save()
        created.append(chunk)

    # The master isn't itself a per-student slice — pull it out of circulation (but keep
    # its data, don't delete) so it doesn't also get handed out as a "variant": not mounted,
    # and hidden so students don't get the whole file in their download either.
    master.is_active = False
    master.hidden = True
    master.save(update_fields=['is_active', 'hidden', 'modified'])

    return created
