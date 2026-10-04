# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Every generated image must expose the shared dataset folder under all the names student
code uses — ~/shared (home), ./shared (working dir) and /srv/shared (JupyterHub) — so a
notebook written on JupyterHub finds its data unchanged in the autograder. These links are
what core.services.mount_paths assumes when it canonicalises mount paths to /shared."""
import pytest

from autograder.testUtils.buildHelpers import createDockerFile


@pytest.mark.parametrize('language', ['python-2.7', 'java', 'node-20'])
def test_generated_image_links_every_shared_folder_spelling(language):
    dockerfile = createDockerFile(language, 'default', environmentID=1)

    assert 'ENV HOME=/home/codepost\n' in dockerfile
    assert 'RUN ln -s /shared /home/codepost/shared\n' in dockerfile
    assert 'RUN ln -s /shared /work/shared\n' in dockerfile
    assert 'RUN mkdir -p /srv && ln -s /shared /srv/shared\n' in dockerfile
    # Links are created before any instructor-supplied Dockerfile lines and before USER drops root.
    assert dockerfile.index('ln -s /shared /srv/shared') < dockerfile.index('WORKDIR /work\nUSER codepost')
