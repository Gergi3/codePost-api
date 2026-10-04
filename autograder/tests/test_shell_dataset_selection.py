# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Which datasets an instructor shell session mounts.

Before this, build_shell_context mounted every active dataset — including a whole
per-student variant pool (all sharing one mount path, so the binds collide) and test-category
fixtures. Now the default is the shared set a normal run gets, and the instructor can pick
exactly which datasets (any variant, any fixture) to mount via the `datasetIds` option.
"""
import factory
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.signals import post_save

from autograder.consumers import EnvironmentShellConsumer
from autograder.services.executors.shell import select_shell_datasets
from core.models import AssignmentDataSet


@pytest.fixture
def shell_datasets(db):
    from core.tests.factories import AssignmentFactory, CourseFactory

    with factory.django.mute_signals(post_save):
        course = CourseFactory()
        assignment = course.assignments.first()
        other_assignment = AssignmentFactory(name='Other', course=course)

    def ds(assignment_, name, **kw):
        return AssignmentDataSet.objects.create(
            assignment=assignment_, name=name, file=SimpleUploadedFile(name, b'x'), **kw)

    return {
        'assignment': assignment,
        'shared': ds(assignment, 'shared.csv'),
        'inactive': ds(assignment, 'retired.csv', is_active=False),
        'v1': ds(assignment, 'v1.csv', is_student_variant=True, mount_path='shared/data.csv'),
        'v2': ds(assignment, 'v2.csv', is_student_variant=True, mount_path='shared/data.csv'),
        'fixture': ds(assignment, 'fixture.csv', is_test_resource=True),
        'foreign': ds(other_assignment, 'foreign.csv'),
    }


class TestSelectShellDatasets:
    def test_default_is_the_shared_set_only(self, shell_datasets):
        """No selection → what a normal run mounts: active shared datasets, no variants (the
        pool shares one mount path), no test fixtures."""
        picked = select_shell_datasets(shell_datasets['assignment'], True, None)
        assert picked == [shell_datasets['shared']]

    def test_explicit_ids_can_add_a_variant_and_a_fixture(self, shell_datasets):
        ids = [shell_datasets['shared'].id, shell_datasets['v1'].id, shell_datasets['fixture'].id]
        picked = select_shell_datasets(shell_datasets['assignment'], True, ids)
        assert {d.id for d in picked} == set(ids)

    def test_explicit_ids_drop_inactive_and_foreign_datasets(self, shell_datasets):
        """The assignment-scoped queryset is the ownership check: another assignment's id is
        silently ignored, as is a retired (inactive) dataset."""
        ids = [shell_datasets['shared'].id, shell_datasets['inactive'].id, shell_datasets['foreign'].id]
        picked = select_shell_datasets(shell_datasets['assignment'], True, ids)
        assert picked == [shell_datasets['shared']]

    def test_empty_selection_mounts_nothing(self, shell_datasets):
        assert select_shell_datasets(shell_datasets['assignment'], True, []) == []

    def test_include_datasets_false_mounts_nothing(self, shell_datasets):
        assert select_shell_datasets(shell_datasets['assignment'], False, None) == []
        assert select_shell_datasets(shell_datasets['assignment'], False, [shell_datasets['shared'].id]) == []

    def test_no_assignment_mounts_nothing(self):
        assert select_shell_datasets(None, True, None) == []


class TestFormatMounts:
    def test_work_dir_mounts_are_tagged_as_datasets(self):
        from autograder.services.shell_session_utils import format_mounts

        mounts = format_mounts({
            '/host/a': {'bind': '/shared/a.csv', 'mode': 'ro'},
            '/host/b': {'bind': '/work/b.csv', 'mode': 'ro'},
            '/host/c': {'bind': '/tmp/pip-cache', 'mode': 'rw'},
        })
        assert [m['type'] for m in mounts] == ['dataset', 'dataset', 'other']


class TestParseIdList:
    """`datasetIds` query param: absent means "use the default", present means "exactly these"."""

    def test_absent_is_none(self):
        assert EnvironmentShellConsumer._parse_id_list(None) is None

    def test_empty_string_is_an_empty_selection(self):
        assert EnvironmentShellConsumer._parse_id_list("") == []

    def test_comma_list(self):
        assert EnvironmentShellConsumer._parse_id_list("1,2, 3") == [1, 2, 3]

    def test_garbage_entries_are_dropped_not_fatal(self):
        assert EnvironmentShellConsumer._parse_id_list("1,x,,-2,3") == [1, 3]
