# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Read-only serializers for the course gradebook (see core/services/gradebook.py).

Explicit classes (not inline_serializer) so the generated TypeScript client gets real,
named interfaces. Cell lists are parallel to the column metadata; each cell also carries
its column id so consumers don't depend on ordering alone.
"""
from rest_framework import serializers


class GradebookAssignmentColumnSerializer(serializers.Serializer):
  id = serializers.IntegerField()
  name = serializers.CharField()
  points = serializers.DecimalField(max_digits=5, decimal_places=2)


class GradebookQuizColumnSerializer(serializers.Serializer):
  id = serializers.IntegerField()
  title = serializers.CharField()


class GradebookAssignmentCellSerializer(serializers.Serializer):
  assignment = serializers.IntegerField()
  grade = serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True,
                                   help_text="The current grade; null until the submission is graded.")
  hasSubmission = serializers.BooleanField()
  isFinalized = serializers.BooleanField()
  submissionId = serializers.IntegerField(allow_null=True,
                                          help_text="Id of the best submission; null when no submission.")
  personalBest = serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True,
                                          help_text="Highest grade ever recorded for this submission.")
  firstMaxAt = serializers.DateTimeField(allow_null=True,
                                         help_text="Earliest timestamp when the personal-best grade was reached.")


class GradebookQuizCellSerializer(serializers.Serializer):
  quiz = serializers.IntegerField()
  score = serializers.DecimalField(max_digits=8, decimal_places=2, allow_null=True,
                                   help_text="Official score per scoringPolicy; null until fully graded.")
  maxScore = serializers.DecimalField(max_digits=8, decimal_places=2, allow_null=True)
  needsGrading = serializers.BooleanField()
  hasAttempts = serializers.BooleanField()


class GradebookRowSerializer(serializers.Serializer):
  student = serializers.EmailField()
  section = serializers.CharField(allow_null=True,
                                  help_text="The student's section name(s); null when unsectioned.")
  assignmentCells = GradebookAssignmentCellSerializer(many=True)
  quizCells = GradebookQuizCellSerializer(many=True)
  totalEarned = serializers.DecimalField(max_digits=10, decimal_places=2)
  totalPossible = serializers.DecimalField(max_digits=10, decimal_places=2)
  percent = serializers.DecimalField(max_digits=6, decimal_places=2, allow_null=True,
                                     help_text="Earned/possible over graded work only; null when nothing is graded.")
  completedAllMaxAt = serializers.DateTimeField(allow_null=True,
                                                help_text="Latest firstMaxAt across assignments; the moment the student "
                                                          "last reached their personal best on every submitted assignment.")


class GradebookResponseSerializer(serializers.Serializer):
  assignments = GradebookAssignmentColumnSerializer(many=True)
  quizzes = GradebookQuizColumnSerializer(many=True)
  rows = GradebookRowSerializer(many=True)
