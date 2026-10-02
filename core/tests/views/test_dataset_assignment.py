# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Tests for per-student dataset variant assignment: deterministic auto-assign, group
sharing, the hidden/variant access fix on AssignmentDataSetViewSet (list/retrieve/download/
by_assignment), datasets bundled into the assignment zip download, the staff override API,
executor dataset staging, and the autograder variant-robustness rerun dispatch."""
import factory
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.signals import post_save
from rest_framework import status

from core.models import AssignmentDataSet, StudentDataSetAssignment
from core.services.dataset_assignment import get_or_assign, get_or_assign_for_submission


@pytest.fixture
def variant_setup(db):
    from core.tests.factories import CourseFactory, SubmissionFactory

    with factory.django.mute_signals(post_save):
        course = CourseFactory(name="ds101", period="s2026", organization__name="Rutgers")
        assignment = course.assignments.first()
        students = list(course.students.all())
        submission = SubmissionFactory(assignment=assignment)
        submission.students.add(students[0])
        submission.isFinalized = True
        submission.save()

    v1 = AssignmentDataSet.objects.create(
        assignment=assignment, name='shopping_a.csv', is_student_variant=True,
        mount_path='shared/shopping.csv', file=SimpleUploadedFile('shopping_a.csv', b'a,b\n1,2\n'))
    v2 = AssignmentDataSet.objects.create(
        assignment=assignment, name='shopping_b.csv', is_student_variant=True,
        mount_path='shared/shopping.csv', file=SimpleUploadedFile('shopping_b.csv', b'a,b\n3,4\n'))
    shared = AssignmentDataSet.objects.create(
        assignment=assignment, name='readme.txt',
        file=SimpleUploadedFile('readme.txt', b'shared instructions'))
    hidden = AssignmentDataSet.objects.create(
        assignment=assignment, name='answer_key.csv', hidden=True,
        file=SimpleUploadedFile('answer_key.csv', b'secret'))

    return {
        'course': course,
        'assignment': assignment,
        'submission': submission,
        'admin': course.courseAdmins.first(),
        'grader': course.graders.first(),
        'students': students,
        'v1': v1,
        'v2': v2,
        'shared': shared,
        'hidden': hidden,
    }


# --------------------------------------------------------------------------- #
# Pool integrity: all variants share one mount_path
# --------------------------------------------------------------------------- #

class TestMountPathConsistency:
    def test_new_variant_aligns_to_pool_mount_path(self, variant_setup):
        assert variant_setup['v1'].mount_path == variant_setup['v2'].mount_path
        v3 = AssignmentDataSet.objects.create(
            assignment=variant_setup['assignment'], name='shopping_c.csv', is_student_variant=True,
            mount_path='some/other/path.csv', file=SimpleUploadedFile('shopping_c.csv', b'a,b\n5,6\n'))
        assert v3.mount_path == variant_setup['v1'].mount_path

    def test_shared_datasets_unaffected(self, variant_setup):
        assert variant_setup['shared'].mount_path != variant_setup['v1'].mount_path


# --------------------------------------------------------------------------- #
# Assignment service: deterministic auto-assign + group sharing
# --------------------------------------------------------------------------- #

class TestGetOrAssign:
    def test_auto_assigns_from_pool(self, variant_setup):
        student = variant_setup['students'][0]
        dataset = get_or_assign(variant_setup['assignment'], student)
        assert dataset is not None
        assert dataset.is_student_variant is True

    def test_idempotent_on_repeat_calls(self, variant_setup):
        student = variant_setup['students'][0]
        first = get_or_assign(variant_setup['assignment'], student)
        second = get_or_assign(variant_setup['assignment'], student)
        assert first.id == second.id
        assert StudentDataSetAssignment.objects.filter(
            assignment=variant_setup['assignment'], student=student).count() == 1

    def test_balances_across_pool(self, variant_setup):
        assignment = variant_setup['assignment']
        students = variant_setup['students']
        for student in students:
            get_or_assign(assignment, student)
        counts = {}
        for row in StudentDataSetAssignment.objects.filter(assignment=assignment):
            counts[row.dataset_id] = counts.get(row.dataset_id, 0) + 1
        # 4 students over a 2-variant pool: no variant should be starved.
        assert set(counts.keys()) == {variant_setup['v1'].id, variant_setup['v2'].id}
        assert max(counts.values()) - min(counts.values()) <= 1

    def test_no_pool_returns_none(self, variant_setup):
        variant_setup['v1'].delete()
        variant_setup['v2'].delete()
        student = variant_setup['students'][0]
        assert get_or_assign(variant_setup['assignment'], student) is None

    def test_group_submission_shares_one_variant(self, variant_setup):
        assignment = variant_setup['assignment']
        submission = variant_setup['submission']
        a, b = variant_setup['students'][0], variant_setup['students'][1]
        submission.students.add(b)

        dataset_a = get_or_assign(assignment, a)
        dataset_b = get_or_assign(assignment, b)
        assert dataset_a.id == dataset_b.id

    def test_group_share_with_divergent_variants_picks_earliest(self, variant_setup):
        """If group members somehow hold different variants (a staff override / regroup), a
        new member deterministically inherits the earliest-assigned one, not an arbitrary
        pick."""
        assignment = variant_setup['assignment']
        submission = variant_setup['submission']  # already has students[0]
        from core.tests.factories import StudentFactory
        with factory.django.mute_signals(post_save):
            c = StudentFactory(course=variant_setup['course'].name,
                               organization=variant_setup['course'].organization, count=98)
        variant_setup['course'].students.add(c)
        b = variant_setup['students'][1]
        submission.students.add(b, c)
        # b's assignment is created first, so it's the earliest.
        StudentDataSetAssignment.objects.create(
            assignment=assignment, student=b, dataset=variant_setup['v2'])
        StudentDataSetAssignment.objects.create(
            assignment=assignment, student=c, dataset=variant_setup['v1'])

        chosen = get_or_assign(assignment, variant_setup['students'][0])
        assert chosen.id == variant_setup['v2'].id

    def test_get_or_assign_for_submission(self, variant_setup):
        dataset = get_or_assign_for_submission(variant_setup['assignment'], variant_setup['submission'])
        assert dataset is not None
        assert dataset.is_student_variant is True

    def test_get_or_assign_for_submission_no_students(self, variant_setup):
        from core.tests.factories import SubmissionFactory
        with factory.django.mute_signals(post_save):
            empty_submission = SubmissionFactory(assignment=variant_setup['assignment'])
        assert get_or_assign_for_submission(variant_setup['assignment'], empty_submission) is None


# --------------------------------------------------------------------------- #
# Access control: hidden datasets + variant ownership
# --------------------------------------------------------------------------- #

class TestDatasetAccess:
    def test_staff_sees_everything_including_hidden(self, api_client, variant_setup):
        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.get(
            f"/assignmentDataSets/by_assignment/?assignment_id={variant_setup['assignment'].id}")
        assert resp.status_code == status.HTTP_200_OK
        names = {d['name'] for d in resp.data}
        assert names == {'shopping_a.csv', 'shopping_b.csv', 'readme.txt', 'answer_key.csv'}

    def test_student_never_sees_hidden_dataset(self, api_client, variant_setup):
        student = variant_setup['students'][0]
        api_client.force_authenticate(user=student)
        resp = api_client.get(
            f"/assignmentDataSets/by_assignment/?assignment_id={variant_setup['assignment'].id}")
        assert resp.status_code == status.HTTP_200_OK
        names = {d['name'] for d in resp.data}
        assert 'answer_key.csv' not in names
        assert 'readme.txt' in names

    def test_student_sees_only_own_variant(self, api_client, variant_setup):
        student = variant_setup['students'][0]
        assigned = get_or_assign(variant_setup['assignment'], student)
        api_client.force_authenticate(user=student)
        resp = api_client.get(
            f"/assignmentDataSets/by_assignment/?assignment_id={variant_setup['assignment'].id}")
        variant_names_seen = {d['name'] for d in resp.data} & {'shopping_a.csv', 'shopping_b.csv'}
        assert variant_names_seen == {assigned.name}

    def test_by_assignment_auto_assigns_on_first_access(self, api_client, variant_setup):
        student = variant_setup['students'][0]
        assert not StudentDataSetAssignment.objects.filter(
            assignment=variant_setup['assignment'], student=student).exists()
        api_client.force_authenticate(user=student)
        api_client.get(f"/assignmentDataSets/by_assignment/?assignment_id={variant_setup['assignment'].id}")
        assert StudentDataSetAssignment.objects.filter(
            assignment=variant_setup['assignment'], student=student).exists()

    def test_student_cannot_download_hidden_dataset(self, api_client, variant_setup):
        student = variant_setup['students'][0]
        api_client.force_authenticate(user=student)
        resp = api_client.get(f"/assignmentDataSets/{variant_setup['hidden'].id}/download/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_student_cannot_download_classmates_variant(self, api_client, variant_setup):
        student = variant_setup['students'][0]
        assigned = get_or_assign(variant_setup['assignment'], student)
        other = variant_setup['v1'] if assigned.id == variant_setup['v2'].id else variant_setup['v2']

        api_client.force_authenticate(user=student)
        own = api_client.get(f"/assignmentDataSets/{assigned.id}/download/")
        assert own.status_code == status.HTTP_200_OK
        denied = api_client.get(f"/assignmentDataSets/{other.id}/download/")
        assert denied.status_code == status.HTTP_404_NOT_FOUND

    def test_student_can_download_shared_dataset(self, api_client, variant_setup):
        student = variant_setup['students'][0]
        api_client.force_authenticate(user=student)
        resp = api_client.get(f"/assignmentDataSets/{variant_setup['shared'].id}/download/")
        assert resp.status_code == status.HTTP_200_OK

    def test_staff_can_download_hidden_dataset(self, api_client, variant_setup):
        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.get(f"/assignmentDataSets/{variant_setup['hidden'].id}/download/")
        assert resp.status_code == status.HTTP_200_OK

    def test_assignment_datasets_action_filters_for_student(self, api_client, variant_setup):
        student = variant_setup['students'][0]
        api_client.force_authenticate(user=student)
        resp = api_client.get(f"/assignments/{variant_setup['assignment'].id}/datasets/")
        assert resp.status_code == status.HTTP_200_OK
        names = {d['name'] for d in resp.data}
        assert 'answer_key.csv' not in names


# --------------------------------------------------------------------------- #
# Assignment zip download: datasets bundled in
# --------------------------------------------------------------------------- #

class TestAssignmentZipDownload:
    def test_student_zip_contains_shared_and_own_variant_only(self, api_client, variant_setup):
        import base64
        import zipfile
        import io

        student = variant_setup['students'][0]
        assigned = get_or_assign(variant_setup['assignment'], student)
        api_client.force_authenticate(user=student)
        resp = api_client.get(f"/assignments/{variant_setup['assignment'].id}/download/")
        assert resp.status_code == status.HTTP_200_OK
        zip_bytes = base64.b64decode(resp.data['zip'])
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert f'data/{assigned.file.name.split("/")[-1]}' in names
            other = variant_setup['v1'] if assigned.id == variant_setup['v2'].id else variant_setup['v2']
            assert f'data/{other.file.name.split("/")[-1]}' not in names
            assert f'data/{variant_setup["shared"].file.name.split("/")[-1]}' in names
            assert 'data/answer_key.csv' not in names
            # Round-trip the bytes for the student's own variant.
            content = zf.read(f'data/{assigned.file.name.split("/")[-1]}')
            assert content in (b'a,b\n1,2\n', b'a,b\n3,4\n')

    def test_staff_zip_excludes_variants(self, api_client, variant_setup):
        import base64
        import zipfile
        import io

        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.get(f"/assignments/{variant_setup['assignment'].id}/download/")
        assert resp.status_code == status.HTTP_200_OK
        zip_bytes = base64.b64decode(resp.data['zip'])
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert f'data/{variant_setup["v1"].file.name.split("/")[-1]}' not in names
            assert f'data/{variant_setup["v2"].file.name.split("/")[-1]}' not in names
            assert f'data/{variant_setup["shared"].file.name.split("/")[-1]}' in names

    def test_include_datasets_false_returns_files_without_datasets(self, api_client, variant_setup):
        """Callers that mount datasets themselves (JupyterHub) get the assignment files
        alone — no data/ entries at all."""
        import base64
        import zipfile
        import io
        from core.tests.factories import AssignmentFileFactory

        with factory.django.mute_signals(post_save):
            AssignmentFileFactory(assignment=variant_setup['assignment'], name='starter.py')

        student = variant_setup['students'][0]
        api_client.force_authenticate(user=student)
        resp = api_client.get(
            f"/assignments/{variant_setup['assignment'].id}/download/?includeDatasets=false")
        assert resp.status_code == status.HTTP_200_OK
        zip_bytes = base64.b64decode(resp.data['zip'])
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert 'starter.py' in names                          # assignment files still ship
            assert not any(n.startswith('data/') for n in names)  # no datasets bundled

    def test_include_datasets_false_does_not_auto_assign_a_variant(self, api_client, variant_setup):
        """The opt-out must also skip get_or_assign's side effect — a JupyterHub file sync
        shouldn't silently claim a variant for the student."""
        from core.tests.factories import AssignmentFileFactory

        with factory.django.mute_signals(post_save):
            AssignmentFileFactory(assignment=variant_setup['assignment'], name='starter.py')

        student = variant_setup['students'][0]
        api_client.force_authenticate(user=student)
        api_client.get(
            f"/assignments/{variant_setup['assignment'].id}/download/?includeDatasets=false")
        assert not StudentDataSetAssignment.objects.filter(
            assignment=variant_setup['assignment'], student=student).exists()

    def test_datasets_included_by_default(self, api_client, variant_setup):
        """Omitting the param keeps the existing behaviour (the student download path)."""
        import base64
        import zipfile
        import io

        student = variant_setup['students'][0]
        api_client.force_authenticate(user=student)
        resp = api_client.get(f"/assignments/{variant_setup['assignment'].id}/download/")
        assert resp.status_code == status.HTTP_200_OK
        zip_bytes = base64.b64decode(resp.data['zip'])
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert any(n.startswith('data/') for n in zf.namelist())


# --------------------------------------------------------------------------- #
# Staff override API
# --------------------------------------------------------------------------- #

class TestStudentDataSetAssignmentAPI:
    def test_admin_lists_assignments_for_assignment(self, api_client, variant_setup):
        student = variant_setup['students'][0]
        get_or_assign(variant_setup['assignment'], student)
        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.get(f"/studentDataSetAssignments/?assignment={variant_setup['assignment'].id}")
        assert resp.status_code == status.HTTP_200_OK
        assert len(resp.data) == 1
        assert resp.data[0]['studentEmail'] == student.email

    def test_list_requires_assignment_param(self, api_client, variant_setup):
        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.get("/studentDataSetAssignments/")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_student_cannot_list(self, api_client, variant_setup):
        api_client.force_authenticate(user=variant_setup['students'][0])
        resp = api_client.get(f"/studentDataSetAssignments/?assignment={variant_setup['assignment'].id}")
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_grader_without_role_cannot_override(self, api_client, variant_setup):
        student = variant_setup['students'][0]
        row_dataset = get_or_assign(variant_setup['assignment'], student)
        row = StudentDataSetAssignment.objects.get(assignment=variant_setup['assignment'], student=student)
        other = variant_setup['v1'] if row_dataset.id == variant_setup['v2'].id else variant_setup['v2']

        api_client.force_authenticate(user=variant_setup['grader'])
        resp = api_client.patch(f"/studentDataSetAssignments/{row.id}/", {'dataset': other.id}, format='json')
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_admin_overrides_assignment(self, api_client, variant_setup):
        student = variant_setup['students'][0]
        assigned = get_or_assign(variant_setup['assignment'], student)
        row = StudentDataSetAssignment.objects.get(assignment=variant_setup['assignment'], student=student)
        other = variant_setup['v1'] if assigned.id == variant_setup['v2'].id else variant_setup['v2']

        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.patch(f"/studentDataSetAssignments/{row.id}/", {'dataset': other.id}, format='json')
        assert resp.status_code == status.HTTP_200_OK
        row.refresh_from_db()
        assert row.dataset_id == other.id
        assert row.assignedBy_id == variant_setup['admin'].id

    def test_override_rejects_dataset_from_other_assignment(self, api_client, variant_setup):
        from core.tests.factories import AssignmentFactory
        student = variant_setup['students'][0]
        get_or_assign(variant_setup['assignment'], student)
        row = StudentDataSetAssignment.objects.get(assignment=variant_setup['assignment'], student=student)

        with factory.django.mute_signals(post_save):
            other_assignment = AssignmentFactory(name='Other', course=variant_setup['course'])
        foreign_dataset = AssignmentDataSet.objects.create(
            assignment=other_assignment, name='foreign.csv', is_student_variant=True,
            file=SimpleUploadedFile('foreign.csv', b'x'))

        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.patch(f"/studentDataSetAssignments/{row.id}/",
                                {'dataset': foreign_dataset.id}, format='json')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


# --------------------------------------------------------------------------- #
# Executor dataset staging (unit-level, no Docker)
# --------------------------------------------------------------------------- #

class TestExecutorStaging:
    def _mock_file(self, submission, assignment, course):
        class MockFile:
            id = -1
            name = 'main.py'
            extension = 'py'
            data = 'print(1)'
            path = ''

            def get_file_info(self):
                return (submission, assignment, course)

        return MockFile()

    def test_stages_shared_and_own_variant(self, variant_setup):
        from autograder.services.executors.python import PythonExecutor

        student = variant_setup['students'][0]
        submission = variant_setup['submission']
        assignment = variant_setup['assignment']
        assigned = get_or_assign(assignment, student)

        executor = PythonExecutor(self._mock_file(submission, assignment, assignment.course))
        staged_ids = {d.id for d in executor.datasets}
        assert variant_setup['shared'].id in staged_ids
        assert assigned.id in staged_ids
        other = variant_setup['v1'] if assigned.id == variant_setup['v2'].id else variant_setup['v2']
        assert other.id not in staged_ids
        # hidden only controls student-facing visibility (downloads/zip), not execution
        # staging — a hidden shared dataset (e.g. a grader-only reference file) still mounts.
        assert variant_setup['hidden'].id in staged_ids

    def test_no_submission_context_stages_shared_only(self, variant_setup):
        from autograder.services.executors.python import PythonExecutor

        assignment = variant_setup['assignment']
        executor = PythonExecutor(self._mock_file(None, assignment, assignment.course))
        staged_ids = {d.id for d in executor.datasets}
        assert staged_ids == {variant_setup['shared'].id, variant_setup['hidden'].id}

    def test_datasets_kwarg_overrides_everything(self, variant_setup):
        from autograder.services.executors.python import PythonExecutor

        assignment = variant_setup['assignment']
        executor = PythonExecutor(
            self._mock_file(variant_setup['submission'], assignment, assignment.course),
            datasets=[variant_setup['v1']])
        assert [d.id for d in executor.datasets] == [variant_setup['v1'].id]

    def test_test_resource_dataset_never_stages_in_normal_runs(self, variant_setup):
        """Test-resource datasets (grading fixtures/answer keys) must not mount into a
        student's own execution — they only reach containers via the 'resources' kwarg
        during a test category's runs."""
        from autograder.services.executors.python import PythonExecutor

        assignment = variant_setup['assignment']
        test_resource = AssignmentDataSet.objects.create(
            assignment=assignment, name='grading_key.csv', is_test_resource=True,
            file=SimpleUploadedFile('grading_key.csv', b'secret,key'))
        assert test_resource.hidden is True  # forced by save()

        executor = PythonExecutor(
            self._mock_file(variant_setup['submission'], assignment, assignment.course))
        staged_ids = {d.id for d in executor.datasets}
        assert test_resource.id not in staged_ids
        assert variant_setup['shared'].id in staged_ids


# --------------------------------------------------------------------------- #
# Autograder variant-robustness rerun dispatch
# --------------------------------------------------------------------------- #

class TestVariantRobustnessDispatch:
    def test_flag_off_queues_nothing(self, variant_setup, monkeypatch):
        from autograder.run import queue_variant_robustness_reruns

        calls = []
        monkeypatch.setattr('autograder.run.RunSubmissionVariant.delay',
                            lambda *a, **kw: calls.append((a, kw)))
        assert queue_variant_robustness_reruns(variant_setup['submission']) == 0
        assert calls == []

    def test_not_finalized_queues_nothing(self, variant_setup, monkeypatch):
        from autograder.run import queue_variant_robustness_reruns

        variant_setup['v1'].autogradeAllVariants = True
        variant_setup['v1'].save()
        variant_setup['v2'].autogradeAllVariants = True
        variant_setup['v2'].save()
        variant_setup['submission'].isFinalized = False
        variant_setup['submission'].save()

        calls = []
        monkeypatch.setattr('autograder.run.RunSubmissionVariant.delay',
                            lambda *a, **kw: calls.append((a, kw)))
        assert queue_variant_robustness_reruns(variant_setup['submission']) == 0
        assert calls == []

    def test_flag_on_queues_one_rerun_per_other_variant(self, variant_setup, monkeypatch):
        from autograder.run import queue_variant_robustness_reruns

        variant_setup['v1'].autogradeAllVariants = True
        variant_setup['v1'].save()
        variant_setup['v2'].autogradeAllVariants = True
        variant_setup['v2'].save()

        student = variant_setup['students'][0]
        assigned = get_or_assign(variant_setup['assignment'], student)

        calls = []
        monkeypatch.setattr('autograder.run.RunSubmissionVariant.delay',
                            lambda *a, **kw: calls.append(a))
        queued = queue_variant_robustness_reruns(variant_setup['submission'])
        assert queued == 1
        (sub_id, dataset_id), = calls
        assert sub_id == variant_setup['submission'].id
        other = variant_setup['v1'] if assigned.id == variant_setup['v2'].id else variant_setup['v2']
        assert dataset_id == other.id

    def test_flag_on_samples_capped_number_of_variants(self, variant_setup, monkeypatch):
        """A large pool is not rerun in full — at most DATASET_VARIANT_RERUN_SAMPLE_SIZE
        other variants are sampled, so compute doesn't scale with pool size."""
        from autograder.run import queue_variant_robustness_reruns
        from core.constants import DATASET_VARIANT_RERUN_SAMPLE_SIZE

        assignment = variant_setup['assignment']
        variant_setup['v1'].autogradeAllVariants = True
        variant_setup['v1'].save()
        variant_setup['v2'].autogradeAllVariants = True
        variant_setup['v2'].save()
        # Grow the flagged pool well beyond the sample size.
        for i in range(DATASET_VARIANT_RERUN_SAMPLE_SIZE + 3):
            AssignmentDataSet.objects.create(
                assignment=assignment, name=f'extra_{i}.csv', is_student_variant=True,
                autogradeAllVariants=True, mount_path='shared/shopping.csv',
                file=SimpleUploadedFile(f'extra_{i}.csv', b'a,b\n9,9\n'))

        calls = []
        monkeypatch.setattr('autograder.run.RunSubmissionVariant.delay',
                            lambda *a, **kw: calls.append(a))
        queued = queue_variant_robustness_reruns(variant_setup['submission'])
        assert queued == DATASET_VARIANT_RERUN_SAMPLE_SIZE
        # Distinct variants, none sampled twice.
        assert len({dataset_id for _, dataset_id in calls}) == DATASET_VARIANT_RERUN_SAMPLE_SIZE

    def test_run_submission_variant_writes_result(self, variant_setup, monkeypatch):
        from autograder.run import RunSubmissionVariant
        from core.models import SubmissionVariantRun

        class FakeResult:
            success = True
            stdout = 'ok'
            stderr = ''
            err = None
            output_data = {'images': []}

        class FakeExecutor:
            def execute(self):
                return FakeResult()

        monkeypatch.setattr('autograder.run.Executor.factory', lambda *a, **kw: FakeExecutor())
        RunSubmissionVariant(variant_setup['submission'].id, variant_setup['v1'].id)

        run = SubmissionVariantRun.objects.get(
            submission=variant_setup['submission'], dataset=variant_setup['v1'])
        assert run.result['status'] == 'success'
        assert run.result['stdout'] == 'ok'

    def test_run_submission_variant_records_error_on_exception(self, variant_setup, monkeypatch):
        from autograder.run import RunSubmissionVariant
        from core.models import SubmissionVariantRun

        def boom(*a, **kw):
            raise RuntimeError("sandbox exploded")

        monkeypatch.setattr('autograder.run.Executor.factory', boom)
        RunSubmissionVariant(variant_setup['submission'].id, variant_setup['v1'].id)

        run = SubmissionVariantRun.objects.get(
            submission=variant_setup['submission'], dataset=variant_setup['v1'])
        assert run.result['status'] == 'error'
        assert 'sandbox exploded' in run.result['error']

    def test_run_submission_variant_no_executable_file(self, variant_setup, monkeypatch):
        from autograder.run import RunSubmissionVariant
        from core.models import SubmissionVariantRun

        monkeypatch.setattr('autograder.run.Executor.factory', lambda *a, **kw: None)
        RunSubmissionVariant(variant_setup['submission'].id, variant_setup['v1'].id)

        run = SubmissionVariantRun.objects.get(
            submission=variant_setup['submission'], dataset=variant_setup['v1'])
        assert run.result['status'] == 'error'


# --------------------------------------------------------------------------- #
# Submission variantRuns endpoint: staff-only
# --------------------------------------------------------------------------- #

class TestVariantRunsEndpoint:
    def test_staff_sees_variant_runs(self, api_client, variant_setup):
        from core.models import SubmissionVariantRun
        SubmissionVariantRun.objects.create(
            submission=variant_setup['submission'], dataset=variant_setup['v1'],
            result={'status': 'success', 'stdout': 'ok'})
        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.get(f"/submissions/{variant_setup['submission'].id}/variantRuns/")
        assert resp.status_code == status.HTTP_200_OK
        assert len(resp.data) == 1
        assert resp.data[0]['datasetName'] == variant_setup['v1'].name

    def test_student_forbidden(self, api_client, variant_setup):
        api_client.force_authenticate(user=variant_setup['students'][0])
        resp = api_client.get(f"/submissions/{variant_setup['submission'].id}/variantRuns/")
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# Split a master dataset into a per-student variant pool
# --------------------------------------------------------------------------- #

class TestSplitMasterDataset:
    def _master(self, variant_setup, rows=10, name='master.csv'):
        content = 'a,b\n' + ''.join(f'{i},{i * 2}\n' for i in range(rows))
        return AssignmentDataSet.objects.create(
            assignment=variant_setup['assignment'], name=name,
            file=SimpleUploadedFile(name, content.encode()))

    def test_service_splits_into_disjoint_chunks(self, variant_setup):
        from core.services.dataset_split import split_master_dataset

        master = self._master(variant_setup, rows=10)
        chunks = split_master_dataset(master, rows_per_chunk=4)
        assert len(chunks) == 3  # ceil(10/4)
        for c in chunks:
            assert c.is_student_variant is True
            assert c.mount_path == chunks[0].mount_path

        # Header repeated in every chunk; data rows partitioned with none dropped or duplicated.
        all_data_rows = []
        for c in chunks:
            with c.file.open('rb') as f:
                text = f.read().decode()
            lines = text.splitlines()
            assert lines[0] == 'a,b'
            all_data_rows.extend(lines[1:])
        assert len(all_data_rows) == 10
        assert len(set(all_data_rows)) == 10  # no duplicates

        master.refresh_from_db()
        assert master.is_active is False
        assert master.hidden is True  # students must not get the whole file in their download
        assert master.is_student_variant is False

    def test_split_master_not_delivered_to_students(self, api_client, variant_setup):
        """After a split, the student's listing and download zip carry their own variant
        chunk but never the master — the master is the whole dataset, handing it out would
        defeat per-student variants."""
        import base64
        import io
        import zipfile
        from core.services.dataset_split import split_master_dataset

        # Start from a clean pool so the student's variant is one of the split chunks.
        AssignmentDataSet.objects.filter(
            assignment=variant_setup['assignment'], is_student_variant=True).delete()
        master = self._master(variant_setup, rows=4)
        chunks = split_master_dataset(master, rows_per_chunk=2)
        chunk_filenames = {c.file.name.split('/')[-1] for c in chunks}
        master_filename = master.file.name.split('/')[-1]

        student = variant_setup['students'][0]
        api_client.force_authenticate(user=student)

        resp = api_client.get(
            f"/assignmentDataSets/by_assignment/?assignment_id={variant_setup['assignment'].id}")
        assert resp.status_code == status.HTTP_200_OK
        listed = {d['id'] for d in resp.data}
        assert master.id not in listed
        assert len(listed & {c.id for c in chunks}) == 1

        resp = api_client.get(f"/assignments/{variant_setup['assignment'].id}/download/")
        assert resp.status_code == status.HTTP_200_OK
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(resp.data['zip']))) as zf:
            data_names = {n[len('data/'):] for n in zf.namelist() if n.startswith('data/')}
        assert master_filename not in data_names
        assert len(data_names & chunk_filenames) == 1

    def test_service_respects_max_chunks(self, variant_setup, monkeypatch):
        from core.services import dataset_split

        monkeypatch.setattr(dataset_split, 'MAX_SPLIT_CHUNKS', 2)
        master = self._master(variant_setup, rows=10)
        with pytest.raises(dataset_split.DatasetSplitError):
            dataset_split.split_master_dataset(master, rows_per_chunk=4)  # would need 3 > cap of 2

    def test_service_rejects_resplit_without_cleanup(self, variant_setup):
        from core.services.dataset_split import DatasetSplitError, split_master_dataset

        master = self._master(variant_setup, rows=6)
        split_master_dataset(master, rows_per_chunk=3)

        # Re-splitting the same (now-inactive) master again — without deleting the
        # previous chunks first — must fail clearly instead of silently colliding.
        with pytest.raises(DatasetSplitError):
            split_master_dataset(master, rows_per_chunk=2)

    def test_service_replace_regenerates_in_place(self, variant_setup):
        """replace=True drops this master's prior variants (cascading to student assignments)
        and recreates from the retained master, instead of erroring on the name collision."""
        from core.services.dataset_split import split_master_dataset
        master = self._master(variant_setup, rows=6)
        first = split_master_dataset(master, rows_per_chunk=3)  # 2 chunks
        assert len(first) == 2

        # Pin a student to one of the first-split chunks (explicit, so the shared fixture's
        # shopping_* pool doesn't decide which variant they get).
        student = variant_setup['students'][0]
        StudentDataSetAssignment.objects.create(
            assignment=variant_setup['assignment'], student=student, dataset=first[0])

        master.refresh_from_db()
        second = split_master_dataset(master, rows_per_chunk=2, replace=True)  # 3 chunks
        assert len(second) == 3

        names = set(AssignmentDataSet.objects.filter(
            assignment=variant_setup['assignment'], is_student_variant=True,
            name__startswith='master_variant_').values_list('name', flat=True))
        assert names == {'master_variant_1.csv', 'master_variant_2.csv', 'master_variant_3.csv'}
        # The stale assignment to a now-deleted chunk was cascade-removed (student reassigns
        # on next access). Other assignments' variants (shopping_*) are untouched.
        assert not StudentDataSetAssignment.objects.filter(
            assignment=variant_setup['assignment'], student=student).exists()

    def test_service_no_header_mode(self, variant_setup):
        from core.services.dataset_split import split_master_dataset

        content = ''.join(f'{i},{i * 2}\n' for i in range(6))
        master = AssignmentDataSet.objects.create(
            assignment=variant_setup['assignment'], name='noheader.csv',
            file=SimpleUploadedFile('noheader.csv', content.encode()))
        chunks = split_master_dataset(master, rows_per_chunk=3, has_header=False)
        assert len(chunks) == 2
        with chunks[0].file.open('rb') as f:
            assert len(f.read().decode().splitlines()) == 3

    def test_api_split_endpoint(self, api_client, variant_setup):
        master = self._master(variant_setup, rows=8)
        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.post(f"/assignmentDataSets/{master.id}/splitIntoVariants/",
                               {'rowsPerChunk': 3, 'hasHeader': True}, format='json')
        assert resp.status_code == status.HTTP_201_CREATED
        assert len(resp.data) == 3  # ceil(8/3)
        assert all(d['isStudentVariant'] for d in resp.data)

    def test_api_split_requires_manage_datasets(self, api_client, variant_setup):
        master = self._master(variant_setup, rows=8)
        api_client.force_authenticate(user=variant_setup['grader'])
        resp = api_client.post(f"/assignmentDataSets/{master.id}/splitIntoVariants/",
                               {'rowsPerChunk': 3}, format='json')
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_api_split_bad_rows_per_chunk(self, api_client, variant_setup):
        master = self._master(variant_setup, rows=8)
        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.post(f"/assignmentDataSets/{master.id}/splitIntoVariants/",
                               {'rowsPerChunk': 'not-a-number'}, format='json')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_api_split_replace(self, api_client, variant_setup):
        master = self._master(variant_setup, rows=6)
        api_client.force_authenticate(user=variant_setup['admin'])
        r1 = api_client.post(f"/assignmentDataSets/{master.id}/splitIntoVariants/",
                             {'rowsPerChunk': 3}, format='json')
        assert r1.status_code == status.HTTP_201_CREATED and len(r1.data) == 2
        # Re-splitting without replace collides and 400s.
        r2 = api_client.post(f"/assignmentDataSets/{master.id}/splitIntoVariants/",
                             {'rowsPerChunk': 2}, format='json')
        assert r2.status_code == status.HTTP_400_BAD_REQUEST
        # With replace it regenerates in place.
        r3 = api_client.post(f"/assignmentDataSets/{master.id}/splitIntoVariants/",
                             {'rowsPerChunk': 2, 'replace': True}, format='json')
        assert r3.status_code == status.HTTP_201_CREATED and len(r3.data) == 3

    def test_generated_pool_stays_stable_as_enrollment_changes(self, variant_setup):
        """The core scenario the split feature exists for: enrollment can change mid-
        semester without needing to re-split (chunk count is fixed by rows_per_chunk).
        New students just balance across the existing pool; only once enrollment exceeds
        the pool size does a chunk get shared, gracefully."""
        from core.services.dataset_split import split_master_dataset
        from core.tests.factories import StudentFactory

        master = self._master(variant_setup, rows=10)
        chunks = split_master_dataset(master, rows_per_chunk=4)  # 3 chunks
        assert len(chunks) == 3

        students = variant_setup['students'][:2]
        for s in students:
            get_or_assign(variant_setup['assignment'], s)
        # A late-enrolling student still gets a distinct, never-before-used chunk.
        with factory.django.mute_signals(post_save):
            late_student = StudentFactory(
                course=variant_setup['course'].name, organization=variant_setup['course'].organization, count=99)
        variant_setup['course'].students.add(late_student)
        third = get_or_assign(variant_setup['assignment'], late_student)
        used_ids = {get_or_assign(variant_setup['assignment'], s).id for s in students}
        assert third.id not in used_ids  # each of the first 3 students got a unique chunk


# --------------------------------------------------------------------------- #
# Cloning: dataset flags copy, per-student rows never do
# --------------------------------------------------------------------------- #

class TestCloning:
    def test_copy_assignment_copies_variant_flags(self, variant_setup):
        from core.tests.factories import CourseFactory
        from core.utils import copy_assignment

        with factory.django.mute_signals(post_save):
            dest_course = CourseFactory(name="ds102", period="s2026", organization__name="Rutgers")

        new_assignment = copy_assignment(variant_setup['assignment'], dest_course)
        assert new_assignment is not None
        cloned = list(new_assignment.dataSets.all())
        assert any(d.name == 'shopping_a.csv' and d.is_student_variant for d in cloned)
        # Per-student assignment rows are never cloned — the clone's pool starts unassigned.
        assert not StudentDataSetAssignment.objects.filter(assignment=new_assignment).exists()

    def test_clone_reports_failed_datasets(self, variant_setup):
        """A dataset whose file can't be read is skipped (the rest still clone) but surfaced
        on the returned assignment so the clone isn't silently missing data."""
        import os
        from core.tests.factories import CourseFactory
        from core.utils import copy_assignment

        # Make one source dataset's file unreadable.
        os.remove(variant_setup['v1'].file.path)
        with factory.django.mute_signals(post_save):
            dest_course = CourseFactory(name="ds104", period="s2026", organization__name="Rutgers")

        new_assignment = copy_assignment(variant_setup['assignment'], dest_course)
        assert new_assignment is not None
        assert 'shopping_a.csv' in new_assignment._datasets_failed_to_copy
        cloned_names = {d.name for d in new_assignment.dataSets.all()}
        assert 'shopping_b.csv' in cloned_names   # the readable ones still cloned
        assert 'shopping_a.csv' not in cloned_names


# --------------------------------------------------------------------------- #
# Quiz code-answer execution mounts the acting student's variant (A1)
# --------------------------------------------------------------------------- #

class TestQuizResponseCodeVariant:
    def test_fixed_code_question_stages_acting_students_variant(self, variant_setup, monkeypatch):
        """A fixed (non-AI) code question has no seeding submission, so the executor can't
        resolve the student's dataset variant on its own — RunQuizResponseCode must resolve
        it from the attempt and stage it explicitly (regression: previously only AI-generated
        questions got a variant)."""
        from core.models import Quiz, QuizAttempt, QuizResponse, Question, QuestionBank
        from core.services.dataset_assignment import get_or_assign

        assignment = variant_setup['assignment']
        course = variant_setup['course']
        student = variant_setup['students'][0]
        with factory.django.mute_signals(post_save):
            quiz = Quiz.objects.create(course=course, assignment=assignment,
                                       title='Q', isPublished=True)
            bank = QuestionBank.objects.create(course=course, name='B')
            code_q = Question.objects.create(course=course, bank=bank, questionType='code',
                                             text='print the mean', language='python')
            attempt = QuizAttempt.objects.create(quiz=quiz, student=student)
            response = QuizResponse.objects.create(
                attempt=attempt, question=code_q,
                questionSnapshot={'language': 'python'}, answerText='print(1)')

        captured = {}

        class FakeResult:
            success = True
            stdout = 'ok'
            stderr = ''
            err = None
            output_data = {'images': []}

        class FakeExecutor:
            EXECUTABLE_EXTENSIONS = ['.py']

            def __init__(self, *a, **kw):
                captured['datasets'] = kw.get('datasets')

            def execute(self):
                return FakeResult()

        monkeypatch.setattr('autograder.services.executors.get_executor_class',
                            lambda lang: FakeExecutor)
        from autograder.run import RunQuizResponseCode
        RunQuizResponseCode(response.id)

        assigned = get_or_assign(assignment, student)
        staged_ids = {d.id for d in (captured['datasets'] or [])}
        assert assigned.id in staged_ids                 # the student's own variant mounts
        assert variant_setup['shared'].id in staged_ids  # shared datasets still mount
        other = variant_setup['v1'] if assigned.id == variant_setup['v2'].id else variant_setup['v2']
        assert other.id not in staged_ids                # never another student's variant


# --------------------------------------------------------------------------- #
# Authoring-time truncation warning for {student_dataset} (D1)
# --------------------------------------------------------------------------- #

class TestDatasetTruncationWarning:
    def _section(self, variant_setup, system_prompt):
        from core.models import Quiz, QuizGeneratedSection
        with factory.django.mute_signals(post_save):
            quiz = Quiz.objects.create(course=variant_setup['course'],
                                       assignment=variant_setup['assignment'],
                                       title='Q', isPublished=True)
            return QuizGeneratedSection.objects.create(
                quiz=quiz, name='S', systemPrompt=system_prompt)

    def test_warns_when_variant_exceeds_cap(self, variant_setup):
        from core.prompts.variables import STUDENT_DATASET_CHAR_CAP
        from core.serializers.generatedQuiz import QuizGeneratedSectionSerializer

        AssignmentDataSet.objects.create(
            assignment=variant_setup['assignment'], name='big_variant.csv',
            is_student_variant=True, mount_path='shared/shopping.csv',
            file=SimpleUploadedFile('big_variant.csv', b'x' * (STUDENT_DATASET_CHAR_CAP + 10)))
        section = self._section(variant_setup, 'Use {student_dataset} to write questions.')
        data = QuizGeneratedSectionSerializer(section).data
        assert data['datasetTruncationWarning'] is not None
        assert 'truncat' in data['datasetTruncationWarning'].lower()

    def test_no_warning_when_prompt_omits_dataset(self, variant_setup):
        from core.prompts.variables import STUDENT_DATASET_CHAR_CAP
        from core.serializers.generatedQuiz import QuizGeneratedSectionSerializer

        AssignmentDataSet.objects.create(
            assignment=variant_setup['assignment'], name='big_variant.csv',
            is_student_variant=True, mount_path='shared/shopping.csv',
            file=SimpleUploadedFile('big_variant.csv', b'x' * (STUDENT_DATASET_CHAR_CAP + 10)))
        section = self._section(variant_setup, 'Write questions about {assignment_name}.')
        data = QuizGeneratedSectionSerializer(section).data
        assert data['datasetTruncationWarning'] is None

    def test_no_warning_when_variants_fit(self, variant_setup):
        from core.serializers.generatedQuiz import QuizGeneratedSectionSerializer

        # variant_setup's variants (v1/v2) are tiny and well under the cap.
        section = self._section(variant_setup, 'Use {student_dataset} to write questions.')
        data = QuizGeneratedSectionSerializer(section).data
        assert data['datasetTruncationWarning'] is None


# --------------------------------------------------------------------------- #
# Dataset update API (PATCH /assignmentDataSets/{id}/) — `hidden` is editable
# --------------------------------------------------------------------------- #

class TestDatasetUpdateAPI:
    def test_admin_can_hide_a_shared_dataset(self, api_client, variant_setup):
        """Unticking "include in students' download" after upload must be possible — before,
        `hidden` was upload-time only and a mistake meant re-uploading."""
        shared = variant_setup['shared']
        student = variant_setup['students'][0]

        api_client.force_authenticate(user=student)
        resp = api_client.get(f"/assignmentDataSets/{shared.id}/")
        assert resp.status_code == status.HTTP_200_OK

        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.patch(f"/assignmentDataSets/{shared.id}/", {'hidden': True}, format='json')
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data['hidden'] is True
        shared.refresh_from_db()
        assert shared.hidden is True

        api_client.force_authenticate(user=student)
        resp = api_client.get(f"/assignmentDataSets/{shared.id}/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_admin_can_unhide_a_dataset(self, api_client, variant_setup):
        hidden = variant_setup['hidden']
        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.patch(f"/assignmentDataSets/{hidden.id}/", {'hidden': False}, format='json')
        assert resp.status_code == status.HTTP_200_OK
        hidden.refresh_from_db()
        assert hidden.hidden is False

    def test_test_resource_stays_hidden(self, api_client, variant_setup):
        """A test-category fixture is never student-visible; the model forces hidden=True on
        save, so a PATCH trying to un-hide it is a no-op rather than a leak."""
        fixture = AssignmentDataSet.objects.create(
            assignment=variant_setup['assignment'], name='fixture.csv', is_test_resource=True,
            file=SimpleUploadedFile('fixture.csv', b'k,v\n'))
        assert fixture.hidden is True

        api_client.force_authenticate(user=variant_setup['admin'])
        resp = api_client.patch(f"/assignmentDataSets/{fixture.id}/", {'hidden': False}, format='json')
        assert resp.status_code == status.HTTP_200_OK
        fixture.refresh_from_db()
        assert fixture.hidden is True

    def test_grader_cannot_update(self, api_client, variant_setup):
        api_client.force_authenticate(user=variant_setup['grader'])
        resp = api_client.patch(
            f"/assignmentDataSets/{variant_setup['shared'].id}/", {'hidden': True}, format='json')
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# Upload validation (POST /assignmentDataSets/) — content must match the extension
# --------------------------------------------------------------------------- #

class TestDatasetUploadValidation:
    def _post(self, api_client, setup, filename, content, **extra):
        api_client.force_authenticate(user=setup['admin'])
        return api_client.post('/assignmentDataSets/', {
            'assignment': setup['assignment'].id,
            'name': filename,
            'file': SimpleUploadedFile(filename, content),
            **extra,
        }, format='multipart')

    @staticmethod
    def _real_zip():
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('a.csv', 'x,y\n1,2\n')
        return buf.getvalue()

    def test_fake_zip_is_rejected_with_a_file_error(self, api_client, variant_setup):
        """The reported case: a .zip whose content isn't a zip (e.g. an HTML page, or a CSV
        someone renamed) must fail at upload, not in the student's code."""
        resp = self._post(api_client, variant_setup, 'data.zip', b'<html>Not Found</html>')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'file' in resp.data
        assert 'not a valid ZIP archive' in str(resp.data['file'])
        assert not AssignmentDataSet.objects.filter(name='data.zip').exists()

    def test_real_zip_is_accepted_and_still_readable(self, api_client, variant_setup):
        import io
        import zipfile
        resp = self._post(api_client, variant_setup, 'data.zip', self._real_zip())
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        ds = AssignmentDataSet.objects.get(id=resp.data['id'])
        with ds.file.open('rb') as f:
            assert zipfile.ZipFile(io.BytesIO(f.read())).namelist() == ['a.csv']

    def test_truncated_zip_is_rejected(self, api_client, variant_setup):
        """Starts with the right magic bytes but has no central directory."""
        resp = self._post(api_client, variant_setup, 'cut.zip', self._real_zip()[:12])
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'file' in resp.data

    def test_zip_based_container_is_checked_too(self, api_client, variant_setup):
        resp = self._post(api_client, variant_setup, 'grades.xlsx', b'name,grade\nA,1\n')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'Excel workbook' in str(resp.data['file'])

    @pytest.mark.parametrize('filename,content', [
        ('data.tar.gz', b'name,grade\n'),
        ('weights.npy', b'not numpy'),
        ('report.pdf', b'<html></html>'),
    ])
    def test_signature_mismatch_is_rejected(self, api_client, variant_setup, filename, content):
        resp = self._post(api_client, variant_setup, filename, content)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'file' in resp.data

    def test_matching_signature_is_accepted(self, api_client, variant_setup):
        import gzip
        resp = self._post(api_client, variant_setup, 'data.csv.gz', gzip.compress(b'x,y\n1,2\n'))
        assert resp.status_code == status.HTTP_201_CREATED, resp.data

    def test_empty_file_is_rejected(self, api_client, variant_setup):
        resp = self._post(api_client, variant_setup, 'empty.csv', b'')
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert 'empty' in str(resp.data['file'])

    def test_text_and_unknown_extensions_are_not_inspected(self, api_client, variant_setup):
        """A UTF-16 CSV (NUL bytes everywhere) and a custom extension both pass — only
        formats with a definitive signature are checked."""
        resp = self._post(api_client, variant_setup, 'utf16.csv', 'x,y\n1,2\n'.encode('utf-16'))
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        resp = self._post(api_client, variant_setup, 'model.bin', b'\x00\x01\x02')
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
