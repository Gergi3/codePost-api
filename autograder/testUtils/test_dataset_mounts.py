# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Path setup for Django
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'codepost.settings')

import django
django.setup()

from autograder.services.executors.base import Executor

class TestDatasetMounting(unittest.TestCase):
    
    def setUp(self):
        # Create a dummy executor subclass since Executor is abstract
        class TestExecutor(Executor):
            def _detect_imports(self, code): return []
            def execute(self): pass
            @classmethod
            def is_executable(cls, filename): return True
            
        dataset_mock = MagicMock()
        dataset_mock.file.path = "/assignment_datasets/test.csv"
        dataset_mock.name = "test_dataset"
        dataset_mock.mount_path = None
        dataset_mock.is_active = True
        
        # Mocking the main file passed to Executor
        file_mock = MagicMock()
        file_mock.get_file_info.return_value = (None, None, None)
        
        self.dataset_mock = dataset_mock
        self.executor = TestExecutor(file_mock, datasets=[dataset_mock])

    @patch('shutil.copy2')
    @patch('os.path.exists')
    @patch.dict(os.environ, {
        'WORKER_DATASET_ROOT': '/assignment_datasets',
        'HOST_DATASET_ROOT': '/mnt/datasets'
    })
    def test_direct_mount_translation(self, mock_exists, mock_copy):
        """
        Verify that when a file is in WORKER_DATASET_ROOT, it is mounted directly 
        from HOST_DATASET_ROOT without copying.
        """
        mock_exists.return_value = True
        
        # Call the method
        temp_dir = "/tmp/staging"
        mounts = self.executor._prepare_dataset_staging(temp_dir)
        
        # Expected bind source: /mnt/datasets/test.csv (Translated)
        expected_bind = "/mnt/datasets/test.csv"
        
        self.assertIn(expected_bind, mounts)
        self.assertEqual(mounts[expected_bind]['bind'], '/shared/test_dataset')
        self.assertEqual(mounts[expected_bind]['mode'], 'ro')
        
        # Verify NO copy happened
        mock_copy.assert_not_called()
        
    @patch('shutil.copy2')
    @patch('os.path.exists')
    @patch('os.chmod')
    @patch.dict(os.environ, {}, clear=True) # Clear relevant env vars
    def test_fallback_copyx_logic(self, mock_chmod, mock_exists, mock_copy):
        """
        Verify fallback to copy logic when env vars are missing.
        """
        mock_exists.return_value = True
        
        # Setup
        self.dataset_mock.file.path = "/some/other/path/test.csv"
        
        temp_dir = "/tmp/staging"
        mounts = self.executor._prepare_dataset_staging(temp_dir)
        
        # Expected bind source: staged path in temp dir
        expected_bind = "/tmp/staging/test.csv"
        
        self.assertIn(expected_bind, mounts)
        
        # Verify copy DID happen
        mock_copy.assert_called_once()
        
    @patch('shutil.copy2')
    @patch('os.path.exists')
    @patch('os.chmod')
    @patch.dict(os.environ, {
        'WORKER_DATASET_ROOT': '/assignment_datasets',
        'HOST_DATASET_ROOT': '/mnt/datasets'
    })
    def test_prefixes_resolve_like_the_generated_image(self, mock_chmod, mock_exists, mock_copy):
        """Generated images have WORKDIR /work, HOME /home/codepost, and ~/shared, ./shared and
        /srv/shared all symlinked to /shared. The relative spellings of the shared folder bind
        at /shared/...; absolute paths bind exactly where typed; `./` is the working dir; `~/`
        is the codepost home. A trailing slash still appends the dataset name."""
        mock_exists.return_value = True
        bind = "/mnt/datasets/test.csv"

        for mount_path, expected in [
            ("~/shared/housing.csv", "/shared/housing.csv"),
            ("/home/codepost/shared/housing.csv", "/home/codepost/shared/housing.csv"),
            ("/srv/shared/housing.csv", "/srv/shared/housing.csv"),
            ("/srv/shared/", "/srv/shared/test_dataset"),
            ("./shared/housing.csv", "/shared/housing.csv"),
            ("/shared/housing.csv", "/shared/housing.csv"),
            ("shared/housing.csv", "/shared/housing.csv"),
            ("~/shared/", "/shared/test_dataset"),
            ("./housing.csv", "/work/housing.csv"),
            ("~/housing.csv", "/home/codepost/housing.csv"),
            ("./data/", "/work/data/test_dataset"),
            ("~/", "/home/codepost/test_dataset"),
            (".", "/work/test_dataset"),
            ("~", "/home/codepost/test_dataset"),
        ]:
            self.dataset_mock.mount_path = mount_path
            mounts = self.executor._prepare_dataset_staging("/tmp/staging")
            self.assertEqual(mounts[bind]['bind'], expected, mount_path)

    @patch('shutil.copy2')
    @patch('os.path.exists')
    @patch('os.chmod')
    @patch.dict(os.environ, {
        'WORKER_DATASET_ROOT': '/assignment_datasets',
        'HOST_DATASET_ROOT': '/mnt/datasets'
    })
    def test_directory_mount_filename_logic(self, mock_chmod, mock_exists, mock_copy):
        """
        Verify that when mount_path ends in a slash, the dataset.name is used for the filename,
        NOT the random disk filename.
        """
        mock_exists.return_value = True
        
        # Setup dataset with different name vs disk filename
        self.dataset_mock.name = "expected_name.csv"
        self.dataset_mock.file.path = "/assignment_datasets/random_disk_name_123.csv"
        self.dataset_mock.mount_path = "/srv/share/test/"
        
        mounts = self.executor._prepare_dataset_staging("/tmp/staging")
        
        # We expect the bind source to be correct (direct mount logic)
        expected_bind = "/mnt/datasets/random_disk_name_123.csv"
        self.assertIn(expected_bind, mounts)
        
        # CRITICAL CHECK: The container bind target should use dataset.name
        # /srv/share/test/expected_name.csv
        expected_target = "/srv/share/test/expected_name.csv"
        self.assertEqual(mounts[expected_bind]['bind'], expected_target)
        
    @patch('shutil.copy2')
    @patch('os.path.exists')
    @patch('os.chmod')
    @patch.dict(os.environ, {
        'WORKER_DATASET_ROOT': '/assignment_datasets',
        'HOST_DATASET_ROOT': '/mnt/datasets',
        'WORKER_STAGING_ROOT': '/staging',
        'HOST_STAGING_ROOT': '/tmp/codepost-staging'
    })
    def test_staging_translation_logic(self, mock_chmod, mock_exists, mock_copy):
        """
        Verify that normal staging logic still translates paths for Docker-in-Docker
        when direct mount is NOT used (e.g. file outside dataset root).
        """
        mock_exists.return_value = True
        
        # File outside dataset root
        self.dataset_mock.file.path = "/staging/some_upload/test.csv"
        
        # Staging dir inside worker staging root
        temp_dir = "/staging/job_123"
        
        mounts = self.executor._prepare_dataset_staging(temp_dir)
        
        # Expected bind source: Translated staging path
        # /staging/job_123/test.csv -> /tmp/codepost-staging/job_123/test.csv
        expected_bind = "/tmp/codepost-staging/job_123/test.csv"
        
        self.assertIn(expected_bind, mounts)
        
        # Verify copy happened
        mock_copy.assert_called_once()


if __name__ == '__main__':
    unittest.main()
