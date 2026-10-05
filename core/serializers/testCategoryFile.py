# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from core.serializers.template import ModelSerializerWithPOSTCheck
from core.models import TestCategoryFile


class TestCategoryFileSerializer(ModelSerializerWithPOSTCheck):
    __test__ = False

    class Meta:
        model = TestCategoryFile
        fields = ('id', 'category', 'name', 'content', 'sortKey')
        POST_permissions_fields = ('category',)
