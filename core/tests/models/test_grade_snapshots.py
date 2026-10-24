# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Tests for the SubmissionGradeSnapshot model and its snapshot_submission_grade signal."""
from decimal import Decimal
from django.test import TestCase

from core.models import Submission, SubmissionGradeSnapshot
from core.management.commands.backfill_grade_snapshots import Command as BackfillCommand
from core.tests.utils import setUpClient, setUpSubmission


class TestSnapshotOnCreate(TestCase):
  def setUp(self):
    setUpClient(self)

  def test_create_writes_one_snapshot(self):
    sub = setUpSubmission(self)
    self.assertEqual(sub.gradeSnapshots.count(), 1)
    snap = sub.gradeSnapshots.first()
    self.assertEqual(snap.grade, sub.grade)
    self.assertFalse(snap.seed)

  def test_no_snapshot_from_factory_muted_signal(self):
    """SubmissionFactory mutes post_save — factories must produce zero snapshots."""
    from core.tests.factories import SubmissionFactory
    sub = SubmissionFactory()
    self.assertEqual(sub.gradeSnapshots.count(), 0)


class TestSnapshotOnGradeChange(TestCase):
  def setUp(self):
    setUpClient(self)

  def test_grade_change_writes_second_snapshot(self):
    sub = setUpSubmission(self)
    initial_count = sub.gradeSnapshots.count()
    original_grade = sub.grade

    # Freeze the grade and set it to a different value manually.
    sub.gradeFrozen = True
    sub.grade = Decimal('50.00')
    sub.save()

    self.assertEqual(sub.gradeSnapshots.count(), initial_count + 1)
    grades = list(sub.gradeSnapshots.values_list('grade', flat=True).order_by('created'))
    self.assertEqual(grades[-1], Decimal('50.00'))

  def test_no_snapshot_when_grade_unchanged(self):
    sub = setUpSubmission(self)
    count_before = sub.gradeSnapshots.count()
    # Save again without changing anything grade-relevant.
    sub.save()
    # Grade stays the same → signal skips writing a new snapshot.
    self.assertEqual(sub.gradeSnapshots.count(), count_before)


class TestBackfillCommand(TestCase):
  def setUp(self):
    setUpClient(self)

  def test_backfill_seeds_submission_without_snapshots(self):
    from core.tests.factories import SubmissionFactory
    sub = SubmissionFactory()  # no snapshot (signal muted)
    self.assertEqual(sub.gradeSnapshots.count(), 0)

    BackfillCommand().handle(dry_run=False)

    self.assertEqual(sub.gradeSnapshots.count(), 1)
    snap = sub.gradeSnapshots.first()
    self.assertTrue(snap.seed)
    self.assertEqual(snap.grade, sub.grade)

  def test_backfill_is_idempotent(self):
    from core.tests.factories import SubmissionFactory
    sub = SubmissionFactory()

    BackfillCommand().handle(dry_run=False)
    BackfillCommand().handle(dry_run=False)

    self.assertEqual(sub.gradeSnapshots.count(), 1)

  def test_backfill_skips_submission_with_existing_snapshot(self):
    sub = setUpSubmission(self)  # already has a real snapshot
    count_before = sub.gradeSnapshots.count()

    BackfillCommand().handle(dry_run=False)

    self.assertEqual(sub.gradeSnapshots.count(), count_before)
