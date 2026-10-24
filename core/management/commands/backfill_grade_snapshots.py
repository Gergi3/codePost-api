# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Seed one grade snapshot per submission that has none.

Run once after deploying the SubmissionGradeSnapshot feature to give existing submissions
a starting history point. Idempotent: skips submissions that already have snapshots.
"""
from django.core.management.base import BaseCommand
from django.utils.timezone import now

from core.models import Submission, SubmissionGradeSnapshot


class Command(BaseCommand):
  help = 'Backfill one seed grade snapshot for every submission that has no snapshots yet.'

  def add_arguments(self, parser):
    parser.add_argument('--dry-run', action='store_true', help='Report counts without writing.')

  def handle(self, *args, **options):
    dry_run = options['dry_run']
    # Submissions that have no snapshots at all.
    already_done = SubmissionGradeSnapshot.objects.values_list('submission_id', flat=True).distinct()
    qs = Submission.objects.exclude(id__in=already_done)
    total = qs.count()
    if dry_run:
      self.stdout.write(f'Would seed {total} snapshot(s) (dry run).')
      return

    created = 0
    for sub in qs.iterator(chunk_size=500):
      ts = sub.dateEdited or sub.dateUploaded or sub.modified or now()
      snap = SubmissionGradeSnapshot(submission=sub, grade=sub.grade, seed=True)
      snap.save()
      # Override the auto-set created timestamp to match when the submission was last edited.
      SubmissionGradeSnapshot.objects.filter(pk=snap.pk).update(created=ts)
      created += 1

    self.stdout.write(self.style.SUCCESS(f'Seeded {created} snapshot(s).'))
