# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from rest_framework import serializers
from core.serializers.template import ModelSerializerWithPOSTCheck
from core.models import TestCase


class TestCaseSerializer(ModelSerializerWithPOSTCheck):

  class Meta:
    model = TestCase
    fields = ('id', 'testCategory', 'sortKey', 'description', 'type', 'pointsFail', 'pointsPass', 'text', 'modified',
              'exposed', 'instances', 'explanation', 'lastSolutionRun', 'testCode', 'targetCellId', 'rubricItem', 'functionName', 'className', 'timeout',
              'hidden', 'learningObjectives')
    POST_permissions_fields = ('testCategory',)
    read_only_fields = ('modified', 'instances', 'className',)
    extra_kwargs = {"text": {"trim_whitespace": False}, "description": {
        "trim_whitespace": False}, "explanation": {"trim_whitespace": False}}

  def validate(self, attrs):
    attrs = super().validate(attrs)
    # A LearningObjective is scoped to a single assignment; the test case it links to must
    # live in that same assignment. Otherwise the objective silently disappears from
    # submissionTestResults (which filters by the submission's assignment).
    objectives = attrs.get('learningObjectives')
    test_category = attrs.get('testCategory') or getattr(self.instance, 'testCategory', None)
    if objectives and test_category is not None:
      assignment_id = test_category.assignment_id
      mismatched = [o for o in objectives if o.assignment_id != assignment_id]
      if mismatched:
        raise serializers.ValidationError({
          'learningObjectives': [
            f"Learning objective {o.id} ({o.shortId}) belongs to a different assignment than this test case."
            for o in mismatched
          ]
        })
    return attrs


class TestCaseStudentSerializer(ModelSerializerWithPOSTCheck):

  class Meta:
    model = TestCase
    fields = ('id', 'testCategory', 'sortKey', 'description', 'pointsFail', 'pointsPass', 'explanation', 'exposed', 'rubricItem',
              'hidden', 'learningObjectives')
    POST_permissions_fields = ('testCategory',)
    extra_kwargs = {"description": {"trim_whitespace": False}, "explanation": {"trim_whitespace": False}}
