# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from core.models import TestCategoryFile
from core.serializers.testCategoryFile import TestCategoryFileSerializer
from core.views.template import ListProtectedViewSet
from rest_framework.permissions import IsAuthenticated
from core.permissions.permissions import TestCategoryFilePermissions


class TestCategoryFileViewSet(ListProtectedViewSet):
  """
  list:
  Return a list of all the testCategoryFiles.

  create:
  Create a new testCategoryFile.

  retrieve:
  Return the given testCategoryFile.

  update:
  Update a testCategoryFile.

  partial_update:
  Update a testCategoryFile.

  delete:
  Delete a testCategoryFile.
  """
  queryset = TestCategoryFile.objects.all()
  serializer_class = TestCategoryFileSerializer
  permission_classes = (IsAuthenticated, TestCategoryFilePermissions)

  # NOTE: We do NOT re-sync TestCases here. The TestCategoryFile post_save signal
  # (core/models.py: update_test_cases_from_file) already triggers update_test_cases
  # on every save. Calling it a second time from the view raced the signal and
  # created duplicate TestCase rows.
