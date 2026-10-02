# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
# Create a docker file from a set of dependencies and a language
import os
import json


def createDockerFile(
    language, build_type, customDockerFile="", dependencies=None, environmentID=0, dependencies_file_content=None, env_vars=None, build_directories=None
):
    buildSpecs = json.load(open(os.path.join(os.path.dirname(__file__), "buildSpecs.json")))

    """
    Helper function to create a dockerfile string from a language, build_type, and dependencies
    """
    if dependencies is None:
        dependencies = []
    if build_directories is None:
        build_directories = []
    
    if build_type == "default":
        customDockerFile = ""

    lookup = language if build_type == "default" else build_type
    baseStr = ""
    if lookup in buildSpecs:
        # Apt-get update gets cached, which causes some problems for new package installs
        # To get a cachce miss for this layer, we need to make it unqiue
        # However it takes some time, so we only want to do it if we are installing packages
        # This is a pretty ugly solution, but it's a common problem with ubuntu docker files and might be
        # the most elegant we can get
        uniqueStr = (
            " && echo {}".format(environmentID)
            if (len(dependencies) > 0 or len(customDockerFile) > 0)
            else ""
        )
        baseStr = buildSpecs[lookup]["base"].format(updateID=uniqueStr)
    _installCmd = buildSpecs[lookup]["install"] if lookup in buildSpecs else None
    userAddCmd = buildSpecs[lookup]["useradd"] if lookup in buildSpecs else ""

    # Create user, home directory, and cache directories with correct permissions
    # We pre-create cache directories so they are owned by codepost even when volume mounted (if initialized)
    
    # helper for language detection
    _lang = language.lower() if language else ""
    
    caches_to_create = []
    
    # Logic refactored to use Executor definitions passed via build_directories
    if build_directories:
        caches_to_create.extend(build_directories)


    cache_mkdir_cmd = ""
    if caches_to_create:
        dirs_space_sep = " ".join(caches_to_create)
        cache_mkdir_cmd = (
            f"RUN mkdir -p {dirs_space_sep} && "
            f"chown -R codepost:codepost {dirs_space_sep}\n"
        )

    dirStr = (
        "\nRUN mkdir -m 777 /work\n"
        "{}\n"
        "ENV HOME=/home/codepost\n"
        "RUN mkdir -p /home/codepost && chown -R codepost:codepost /home/codepost\n"
        "{}"
        "RUN mkdir -p /shared && chmod 555 /shared\n"
        "RUN ln -s /shared /home/codepost/shared\n"
        "RUN ln -s /shared /work/shared\n"
        "RUN mkdir -p /srv && ln -s /shared /srv/shared\n"
        "RUN chown -R codepost:codepost /work\n"
    ).format(userAddCmd, cache_mkdir_cmd)


    dependencyStr = ""

    # Inject Environment Variables
    if env_vars:
        for key, value in env_vars.items():
            # Escape quotes in value if needed, strict escaping is hard but let's do basic
            safe_val = value.replace('"', '\\"')
            dependencyStr += f'ENV {key}="{safe_val}"\n'
    
    # Handle requirements / package.json / pom.xml logic (Generic from buildSpecs)
    if dependencies_file_content:
        if language in buildSpecs and "manifestInstall" in buildSpecs[language]:
             dependencyStr += "\n" + buildSpecs[language]["manifestInstall"] + "\n"
        # Fallback / Legacy for partial matches if needed, but we aim for strict config
        elif 'javascript' in language or 'js' in language:
             # Keep this just in case 'javascript' key is missed or language var varies
             pass 

    for d in dependencies:
        d_stripped = d.strip()
        if len(d_stripped) > 0 and not d_stripped.startswith('//') and d_stripped != '...':
            dependencyStr += "RUN {}\n".format(d)

    return baseStr + dirStr + dependencyStr + customDockerFile + "\nWORKDIR /work\nUSER codepost\n"
