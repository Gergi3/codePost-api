# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field

from core.constants import MAX_DATASET_SIZE
from core.models import AssignmentDataSet


class AssignmentDataSetSerializer(serializers.ModelSerializer):
    """Serializer for AssignmentDataSet model"""
    
    # Read-only computed fields (camelCase)
    fileUrl = serializers.SerializerMethodField()
    fileSize = serializers.SerializerMethodField()
    fileName = serializers.SerializerMethodField()

    # camelCase aliases
    mountPath = serializers.CharField(source='mount_path', required=False, allow_blank=True)
    isActive = serializers.BooleanField(source='is_active', required=False)
    isTestResource = serializers.BooleanField(source='is_test_resource', required=False)
    isStudentVariant = serializers.BooleanField(source='is_student_variant', required=False)

    class Meta:
        model = AssignmentDataSet
        fields = [
            'id',
            'assignment',
            'name',
            'description',
            'file',
            'fileUrl',
            'fileSize',
            'fileName',
            'mountPath',
            'isActive',
            'created',
            'modified',
            'hidden',
            'isTestResource',
            'isStudentVariant',
            'autogradeAllVariants',
        ]
        read_only_fields = ['id', 'created', 'modified', 'fileUrl', 'fileSize', 'fileName', 'hidden']

    def to_internal_value(self, data):
        data = data.copy()
        if 'mount_path' in data and 'mountPath' not in data:
            data['mountPath'] = data['mount_path']
        if 'is_active' in data and 'isActive' not in data:
            data['isActive'] = data['is_active']
        if 'is_student_variant' in data and 'isStudentVariant' not in data:
            data['isStudentVariant'] = data['is_student_variant']
        return super().to_internal_value(data)
    
    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_fileUrl(self, obj):
        """Get the URL to download the dataset file"""
        if obj.file:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.file.url)
            return obj.file.url
        return None
    
    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_fileSize(self, obj):
        """Get the size of the dataset file in bytes"""
        if obj.file:
            try:
                return obj.file.size
            except:
                return None
        return None
    
    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_fileName(self, obj):
        """Get the original filename"""
        if obj.file:
            try:
                return obj.file.name.split('/')[-1]
            except:
                return None
        return None


class AssignmentDataSetCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating AssignmentDataSet"""

    mountPath = serializers.CharField(source='mount_path', required=False, allow_blank=True)
    isActive = serializers.BooleanField(source='is_active', required=False)
    isTestResource = serializers.BooleanField(source='is_test_resource', required=False)
    isStudentVariant = serializers.BooleanField(source='is_student_variant', required=False)

    class Meta:
        model = AssignmentDataSet
        fields = [
            'assignment',
            'name',
            'description',
            'file',
            'mountPath',
            'isActive',
            'hidden',
            'isTestResource',
            'isStudentVariant',
            'autogradeAllVariants',
        ]

    def to_internal_value(self, data):
        data = data.copy()
        if 'mount_path' in data and 'mountPath' not in data:
            data['mountPath'] = data['mount_path']
        if 'is_active' in data and 'isActive' not in data:
            data['isActive'] = data['is_active']
        if 'is_test_resource' in data and 'isTestResource' not in data:
            data['isTestResource'] = data['is_test_resource']
        if 'is_student_variant' in data and 'isStudentVariant' not in data:
            data['isStudentVariant'] = data['is_student_variant']
        return super().to_internal_value(data)

    def validate_file(self, value):
        """Validate file size, then that the content matches the extension (a renamed CSV
        or an HTML error page saved as .zip would otherwise only fail in a student's code)."""
        if value.size > MAX_DATASET_SIZE:
            raise serializers.ValidationError(
                f"File size exceeds maximum allowed size of {MAX_DATASET_SIZE / (1024**3):.1f} GB"
            )

        from core.services.dataset_validation import dataset_file_problem
        problem = dataset_file_problem(value.name or '', value, value.size)
        if problem:
            raise serializers.ValidationError(problem)

        return value


class AssignmentDataSetUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating AssignmentDataSet (without file upload)"""

    mountPath = serializers.CharField(source='mount_path', required=False, allow_blank=True)
    isActive = serializers.BooleanField(source='is_active', required=False)
    isTestResource = serializers.BooleanField(source='is_test_resource', required=False)
    isStudentVariant = serializers.BooleanField(source='is_student_variant', required=False)

    class Meta:
        model = AssignmentDataSet
        fields = [
            'name',
            'description',
            'mountPath',
            'isActive',
            'hidden',
            'isTestResource',
            'isStudentVariant',
            'autogradeAllVariants',
        ]

    def to_internal_value(self, data):
        data = data.copy()
        if 'mount_path' in data and 'mountPath' not in data:
            data['mountPath'] = data['mount_path']
        if 'is_active' in data and 'isActive' not in data:
            data['isActive'] = data['is_active']
        if 'is_test_resource' in data and 'isTestResource' not in data:
            data['isTestResource'] = data['is_test_resource']
        if 'is_student_variant' in data and 'isStudentVariant' not in data:
            data['isStudentVariant'] = data['is_student_variant']
        return super().to_internal_value(data)
