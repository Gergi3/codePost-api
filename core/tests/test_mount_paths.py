# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""One shared folder, many spellings: ~/shared, /srv/shared (JupyterHub), /shared, ./shared
and shared/ must all store and mount identically. An instructor's notebook written against
`~/shared/data.csv` on JupyterHub has to find the same file in the autograder."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from core.models import AssignmentDataSet
from core.services.mount_paths import container_mount_path, normalize_mount_path


@pytest.mark.parametrize('typed', ['shared/pdf', '~/shared/pdf', './shared/pdf', '  ~/shared/pdf  '])
def test_relative_spellings_of_the_shared_folder_store_as_shared(typed):
    assert normalize_mount_path(typed) == 'shared/pdf'


@pytest.mark.parametrize('typed', [
    '/srv/shared/pdf', '/home/codepost/shared/pdf', '/work/shared/pdf', '/shared/pdf', '/srv/shared/', '/opt/data/x',
])
def test_absolute_paths_are_never_rewritten(typed):
    """Absolute means absolute: an instructor who types /srv/shared/pdf gets a bind exactly
    there (it also keeps working in images built before the /srv/shared link existed)."""
    assert normalize_mount_path(typed) == typed
    name = 'pdf'
    expected = typed + name if typed.endswith('/') else typed
    assert container_mount_path(typed, name) == expected


@pytest.mark.parametrize('typed,stored', [
    ('~/shared', 'shared/'),
    ('./shared', 'shared/'),
    ('.', './'),
    ('~', '~/'),
    ('./data.csv', './data.csv'),
    ('~/data.csv', '~/data.csv'),
    ('/etc/conf.json', '/etc/conf.json'),
    ('mnist/', 'mnist/'),
    ('', ''),
    (None, ''),
])
def test_other_paths_keep_their_shape(typed, stored):
    assert normalize_mount_path(typed) == stored


@pytest.mark.parametrize('stored,expected', [
    ('shared/pdf', '/shared/pdf'),
    ('~/shared/pdf', '/shared/pdf'),
    ('shared/', '/shared/a.csv'),
    ('mnist/', '/shared/mnist/a.csv'),
    ('./a.csv', '/work/a.csv'),
    ('~/a.csv', '/home/codepost/a.csv'),
    ('/etc/conf.json', '/etc/conf.json'),
    ('', '/shared/a.csv'),
])
def test_container_mount_path(stored, expected):
    assert container_mount_path(stored, 'a.csv') == expected


@pytest.mark.django_db
def test_save_normalizes_jupyterhub_spellings():
    from core.tests.factories import CourseFactory
    import factory
    from django.db.models.signals import post_save

    with factory.django.mute_signals(post_save):
        assignment = CourseFactory().assignments.first()

    ds = AssignmentDataSet.objects.create(
        assignment=assignment, name='pdf', mount_path='~/shared/pdf',
        file=SimpleUploadedFile('pdf', b'x'))
    assert ds.mount_path == 'shared/pdf'

    ds.mount_path = './shared/pdf'
    ds.save()
    ds.refresh_from_db()
    assert ds.mount_path == 'shared/pdf'

    # Absolute stays literal.
    ds.mount_path = '/srv/shared/pdf'
    ds.save()
    ds.refresh_from_db()
    assert ds.mount_path == '/srv/shared/pdf'
