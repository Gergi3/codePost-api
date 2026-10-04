# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import logging
import docker
import os
import shutil
import tempfile
from django.utils import timezone
from core.models import Environment
from autograder.services.image_manager import ImageManager
from autograder.testUtils.buildHelpers import createDockerFile
from autograder.services.executors import get_executor_class
from log.models import Event
import json
import time



logger = logging.getLogger(__name__)

class Builder:
    def __init__(self, environment_id: int):
        self.environment_id = environment_id
        try:
            self.client = docker.from_env()
        except Exception as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            raise e

    def verify_image(self, image_tag: str, env: Environment) -> bool:
        """
        Verify the built image by running a container and checking:
        1. Write access to /work (as codepost user)
        2. Read access to public datasets (if any)
        3. Language specific execution and package installation capability
        """
        try:
            logger.info(f"[Builder] Verifying image {image_tag}...")
            
            # Use shell for verification to be language-agnostic
            # 1. Print ID
            # 2. Try to touch a file in /work
            # 3. Try to touch a file in /outputs
            check_command = (
                "echo 'Verifying permissions...'; "
                "id; "
                "touch /work/verify_write.txt && echo 'Write to /work: OK' || { echo 'Write to /work: FAILED'; exit 1; }; "
            )
            
            # Language specific checks
            lang = env.language.lower()
            if 'python' in lang:
                check_command += (
                    "echo 'Checking Python...'; "
                    "python3 -c 'print(\"Python Exec: OK\")' || { echo 'Python Exec: FAILED'; exit 1; }; "
                    "pip install --retries 0 --timeout 20 requests > /dev/null && echo 'Pip Install: OK' || { echo 'Pip Install: FAILED'; exit 1; }; "
                )
            elif 'node' in lang or 'javascript' in lang or 'js' in lang:
                check_command += (
                    "echo 'Checking Node...'; "
                    "node -e 'console.log(\"Node Exec: OK\")' || { echo 'Node Exec: FAILED'; exit 1; }; "
                    "npm init -y > /dev/null && npm install lodash > /dev/null && echo 'NPM Install: OK' || { echo 'NPM Install: FAILED'; exit 1; }; "
                )
            elif 'java' in lang:
                check_command += (
                    "echo 'Checking Java...'; "
                    "java -version 2>&1 | grep 'version' && echo 'Java Version: OK' || { echo 'Java Version: FAILED'; exit 1; }; "
                    "echo 'public class Check { public static void main(String[] args) { System.out.println(\"Java Exec: OK\"); } }' > Check.java && "
                    "javac Check.java && java Check || { echo 'Java Compile/Run: FAILED'; exit 1; }; "
                )
            elif 'c' in lang or 'cpp' in lang:
                # C/C++ (gcc/g++)
                check_command += (
                    "echo 'Checking C/C++...'; "
                    "g++ --version | grep 'g++' && echo 'G++ Version: OK' || { echo 'G++ Version: FAILED'; exit 1; }; "
                    "echo '#include <iostream>\nint main() { std::cout << \"C++ Exec: OK\" << std::endl; return 0; }' > check.cpp && "
                    "g++ -o check check.cpp && ./check || { echo 'C++ Compile/Run: FAILED'; exit 1; }; "
                )
            elif 'ruby' in lang:
                check_command += (
                    "echo 'Checking Ruby...'; "
                    "ruby -e 'puts \"Ruby Exec: OK\"' || { echo 'Ruby Exec: FAILED'; exit 1; }; "
                    "gem install --no-document json > /dev/null && echo 'Gem Install: OK' || { echo 'Gem Install: FAILED'; exit 1; }; "
                )
            elif 'php' in lang:
                check_command += (
                    "echo 'Checking PHP...'; "
                    "php -r 'echo \"PHP Exec: OK\n\";' || { echo 'PHP Exec: FAILED'; exit 1; }; "
                    # Check composer if installed, use whereis or command -v
                    "if command -v composer >/dev/null 2>&1; then "
                    "composer require --no-interaction psr/log > /dev/null && echo 'Composer Install: OK' || { echo 'Composer Install: FAILED'; exit 1; }; "
                    "else echo 'Composer not found (skipping install check)'; fi; "
                )
            elif 'r' in lang:
                 check_command += (
                    "echo 'Checking R...'; "
                    "Rscript -e 'print(\"R Exec: OK\")' || { echo 'R Exec: FAILED'; exit 1; }; "
                    # R install packages might be slow/complex, skipping install check for now unless critical
                )
                
            check_command += "echo 'VERIFICATION SUCCESS'"

            # Docker Environment for caches
            docker_env = {
                "NPM_CONFIG_CACHE": "/tmp/npm-cache",
                "PIP_CACHE_DIR": "/tmp/pip-cache",
                "PIP_ROOT_USER_ACTION": "ignore",
                "GEM_HOME": "/tmp/gems",
                "COMPOSER_CACHE_DIR": "/tmp/composer-cache",
            }
            
            # Docker Volumes
            # Get executor class to resolve volumes dynamically
            executor_cls = get_executor_class(env.language)
            
            # Docker Environment for caches (Ideally should also come from executor, but kept here for now or we can use executor._get_docker_environment method if static)
            # Actually, let's use the executor logic if we can, but _get_docker_environment is an instance method usually.
            # However, INIT_DOCKER_VOLUME is a class attribute.
            
            # Docker Volumes
            volumes = {}
            if executor_cls:
                volumes = executor_cls.INIT_DOCKER_VOLUME.copy()
            else:
                 # Fallback if no executor found (shouldn't happen for valid langs)
                 volumes = {
                     "codepost-pip-cache": {'bind': '/tmp/pip-cache', 'mode': 'rw'},
                     "codepost-npm-cache": {'bind': '/tmp/npm-cache', 'mode': 'rw'},
                 }

            # Run container with volumes mounted
            # Use sh -c to execute the command string
            # Network must be enabled for install checks
            log_generator = self.client.containers.run(
                image_tag,
                command=["sh", "-c", check_command],
                volumes=volumes,
                remove=True,
                user='codepost', # Enforce user
                stdout=True,
                stderr=True,
                stream=True,
                environment=docker_env
            )
            
            logs = ""
            for chunk in log_generator:
                logs += chunk.decode('utf-8')  # type: ignore[union-attr]  # Docker stream returns bytes
                
            if "VERIFICATION SUCCESS" in logs:
                logger.info(f"[Builder] Verification passed for {image_tag}")
                env.build_logs += "\n[Verification] Permission & Sanity checks passed."
                env.save()
                return True
            else:
                logger.error(f"[Builder] Verification failed for {image_tag}. Logs:\n{logs}")
                env.build_logs += f"\n[Verification] Failed:\n{logs}"
                env.save()
                return False
                    
        except Exception as e:
             logger.error(f"[Builder] Verification exception: {e}")
             env.build_logs += f"\n[Verification] Error: {e}"
             env.save()
             return False

    def fix_cache_permissions(self, image_tag: str, env: Environment):
        """
        Runs a temporary container as root to chown the cache volumes to codepost user.
        This handles the issue where named volumes are created as root on the host.
        """
        try:
            logger.info(f"[Builder] Fixing cache permissions for {image_tag}")
            
            executor_cls = get_executor_class(env.language)
            if not executor_cls or not hasattr(executor_cls, 'INIT_DOCKER_VOLUME'):
                return

            volumes = executor_cls.INIT_DOCKER_VOLUME.copy()
            if not volumes:
                return

            # Construct chown command for all mounted volumes
            # volumes dict: {'vol_name': {'bind': '/path', 'mode': 'rw'}}
            paths = [v['bind'] for v in volumes.values()]
            if not paths:
                return
            
            # chown -R codepost:codepost /path1 /path2 ...
            cmd = f"chown -R codepost:codepost {' '.join(paths)}"
            
            self.client.containers.run(
                image_tag,
                command=["sh", "-c", cmd],
                volumes=volumes,
                remove=True,
                user='root', # Run as root to fix permissions
                # No network needed, just fs ops
            )
            logger.info(f"[Builder] Successfully fixed permissions for: {paths}")
            env.build_logs += f"\n[Builder] Fixed permissions on cache volumes: {', '.join(paths)}"
            env.save()

        except Exception as e:
            logger.error(f"[Builder] Failed to fix permissions: {e}")
            env.build_logs += f"\n[Builder] Warning: Failed to fix cache permissions: {e}"
            env.save()

    def build(self):
        """
        Builds the Docker image for the environment.
        Updates Environment model with status and logs.
        """
        try:
            env = Environment.objects.get(id=self.environment_id)
        except Environment.DoesNotExist:
            logger.error(f"Environment {self.environment_id} not found.")
            return {
                "success": False,
                "error": f"Environment {self.environment_id} not found"
            }


        start_time = timezone.now()
        env.build_status = 1  # Building
        env.build_logs = f"Starting build...\nTimestamp: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        env.save()
        logger.info(f"[Builder] Starting build for env {self.environment_id}")

        try:
            meta = {
                "event": "build_started",
                "environment_id": self.environment_id,
                "timestamp": time.time()
            }
            Event.objects.create(
                category="builder",
                user="system",
                description=f"Build started for Env {self.environment_id}",
                courseID=env.assignment.course.id,
                meta=json.dumps(meta)
            )
        except Exception:
            pass

        build_dir = tempfile.mkdtemp()
        try:
            # 1. Create Dockerfile
            dockerfile_path = os.path.join(build_dir, 'Dockerfile')
            
            # Determine language key
            lang_key = env.language
            if lang_key == 'python3':
                lang_key = 'python'

            # Java images bake the pinned JUnit/Mockito jars at build time via a
            # `COPY libs-pom.xml` in the buildSpecs `baseExtra`. Stage that pom into
            # the build context so the COPY resolves. (Only java-27 declares the
            # baseExtra today; copying for any java* language is harmless.)
            if 'java' in env.language.lower():
                libs_pom_src = os.path.join(
                    os.path.dirname(__file__), '..', 'testUtils', 'libs-pom.xml'
                )
                if os.path.exists(libs_pom_src):
                    shutil.copy(libs_pom_src, os.path.join(build_dir, 'libs-pom.xml'))
            
            # Write dependencies file to disk if present
            if env.requirements:
                if 'javascript' in env.language or 'js' in env.language or 'node' in env.language:
                    filename = 'package.json'
                elif 'java' in env.language:
                    filename = 'pom.xml'
                else:
                    filename = 'requirements.txt'
                
                with open(os.path.join(build_dir, filename), 'w') as f:
                    f.write(env.requirements)

            # Get build directories from executor
            executor_cls = get_executor_class(env.language)
            build_dirs = executor_cls.BUILD_CACHE_DIRECTORIES if executor_cls else []
            env.build_logs += f"Using manifest: \n{env.requirements}\n\n"
            env.build_logs += f"Using build directories: \n{build_dirs}\n\n"

            # Generate Dockerfile content
            content = createDockerFile(
                lang_key,
                env.buildType,
                env.dockerfile,
                env.dockerRunInstructions,
                env.id,
                dependencies_file_content=env.requirements,
                env_vars=env.env_vars,
                build_directories=build_dirs
            )

            with open(dockerfile_path, 'w') as f:
                f.write(content)

            # 2. Build Image
            tag = f"codepost-env-{env.id}:{int(timezone.now().timestamp())}"
            env.build_logs += f"Building image {tag}...\n"
            env.save()

            try:
                build_logs_generator = self.client.api.build(
                    path=build_dir,
                    tag=tag,
                    decode=True,
                    rm=True
                )
                
                last_save = timezone.now()
                for chunk in build_logs_generator:
                    if 'stream' in chunk:
                        line = chunk['stream']
                        env.build_logs += line
                        # Throttle saves to every 2 seconds to reduce DB load
                        if (timezone.now() - last_save).total_seconds() > 2:
                             env.save()
                             last_save = timezone.now()
                    elif 'error' in chunk:
                        raise Exception(chunk['error'])
                
                # Final save of logs
                env.save()

                # Fix permissions on cache volumes (Docker named volumes are often root-owned)
                self.fix_cache_permissions(tag, env)
                        
                # Verify Image before finalizing
                if not self.verify_image(tag, env):
                     raise Exception("Image verification failed (permissions check). See logs.")

                # Success
                env.build_status = 2 # Success
                env.image_name = tag
                env.last_built = timezone.now()
                
                # Save version to history
                ImageManager.save_image_version(env.id, tag, env.requirements)
                
                # Trigger cleanup of old images
                ImageManager.cleanup_old_images(env.id)
                env.build_logs += "\nBuild successful!"
                logger.info(f"Build successful for env {env.id}")
                env.save()

                try:
                    meta = {
                        "event": "build_success",
                        "environment_id": env.id,
                        "image": tag,
                        "timestamp": time.time()
                    }
                    Event.objects.create(
                        category="builder",
                        user="system",
                        description=f"Build successful for Env {env.id}",
                        courseID=env.assignment.course.id,
                        meta=json.dumps(meta)
                    )
                except Exception:
                    pass

                return {
                    "success": True,
                    "image": tag,
                    "logs": env.build_logs
                }
                
            except Exception as e:
                env.build_status = 3 # Failed
                env.build_logs += f"\nBuild failed: {e}"
                env.save()
                logger.error(f"Build failed for env {env.id}: {e}")

                try:
                    meta = {
                        "event": "build_failed",
                        "environment_id": env.id,
                        "error": str(e),
                        "timestamp": time.time()
                    }
                    Event.objects.create(
                        category="builder",
                        user="system",
                        description=f"Build failed for Env {env.id}",
                        courseID=env.assignment.course.id,
                        meta=json.dumps(meta)
                    )
                except Exception:
                    pass

                return {
                    "success": False,
                    "error": str(e),
                    "logs": env.build_logs
                }

        except Exception as e:
            env.build_status = 3 # Failed
            env.build_logs += f"\nUnexpected error: {e}"
            env.save()
            logger.error(f"Unexpected error in build for env {env.id}: {e}")

            try:
                meta = {
                    "event": "build_error",
                    "environment_id": env.id,
                    "error": str(e),
                    "timestamp": time.time()
                }
                Event.objects.create(
                    category="builder",
                    user="system",
                    description=f"Unexpected build error for Env {env.id}",
                    courseID=env.assignment.course.id,
                    meta=json.dumps(meta)
                )
            except Exception:
                pass

            return {
                "success": False,
                "error": str(e),
                "logs": env.build_logs
            }
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)