# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Binary assignment/submission files are stored as data URIs
("data:<mime>;base64,..."). add_additional_files must decode them so the
container gets the real bytes (e.g. an image a notebook reads), while text
files are still written as UTF-8.
"""
import base64
import io
import tarfile
from unittest import mock

from django.test import SimpleTestCase

from autograder.services.executors.mock_file import MockFile
from autograder.services.executors.python import PythonExecutor

JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00binary\x00payload"


def _injected(additional_files):
    mf = MockFile(data="x = 1\n", name="student.py", extension=".py")
    mf.get_course = lambda: None  # type: ignore[attr-defined]
    executor = PythonExecutor(mf)
    executor.additional_files = additional_files
    container = mock.MagicMock()
    executor.add_additional_files(container)

    files = {}
    for call in container.put_archive.call_args_list:
        dest, archive = call.args
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                extracted = tar.extractfile(member)
                assert extracted is not None
                files[f"{dest.rstrip('/')}/{member.name}"] = extracted.read()
    return files


class AdditionalFilesBinaryTests(SimpleTestCase):
    def test_data_uri_is_decoded_to_binary(self):
        uri = "data:image/jpeg;base64," + base64.b64encode(JPEG_BYTES).decode()
        files = _injected({"images/covariance.jpg": uri})
        self.assertEqual(files["/work/images/covariance.jpg"], JPEG_BYTES)

    def test_text_file_is_written_as_utf8(self):
        files = _injected({"helper.py": "print('héllo')\n"})
        self.assertEqual(files["/work/helper.py"], "print('héllo')\n".encode("utf-8"))
