# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import pytest

from core.models import TestCategoryFile
from core.serializers.testCategoryFile import TestCategoryFileSerializer
from core.serializers.testCategory import TestCategorySerializer
from core.tests.factories import TestCategoryFactory


@pytest.mark.django_db
def test_create_test_category_file():
    category = TestCategoryFactory()
    data = {
        'category': category.id,
        'name': 'FooTest.java',
        'content': 'import org.junit.jupiter.api.Test;\npublic class FooTest { @Test void a(){} }\n',
        'sortKey': 0,
    }
    serializer = TestCategoryFileSerializer(data=data)
    assert serializer.is_valid(), serializer.errors
    f = serializer.save()
    assert f.category == category
    assert f.name == 'FooTest.java'
    # Creating a file triggers the sync signal -> a TestCase for method 'a'.
    assert category.testCases.filter(functionName='a').exists()


@pytest.mark.django_db
def test_category_serializer_nests_test_files():
    category = TestCategoryFactory()
    TestCategoryFile.objects.create(
        category=category, name='FooTest.java',
        content='import org.junit.jupiter.api.Test;\npublic class FooTest { @Test void a(){} }\n',
        sortKey=0,
    )
    from django.test import RequestFactory
    request = RequestFactory().get('/')
    data = TestCategorySerializer(category, context={'request': request}).data
    assert 'testFiles' in data
    assert len(data['testFiles']) == 1
    assert data['testFiles'][0]['name'] == 'FooTest.java'
    assert 'content' in data['testFiles'][0]
