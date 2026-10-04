# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Convergence service - Auto-healing for missing dependencies.

Uses a class-based pattern similar to Scanners, with language-specific
convergers that handle different error patterns and manifest formats.
"""
import abc
import logging
import re
import json
from typing import Set, Tuple, List, Dict, Any, Optional
from core.models import Environment
from django.db.models import F

logger = logging.getLogger(__name__)


class ConvergenceResult:
    """Result from convergence analysis."""
    def __init__(
        self,
        should_converge: bool = False,
        modules_to_add: Optional[Set[str]] = None,
        failed_submission_ids: Optional[List[int]] = None
    ):
        self.should_converge = should_converge
        self.modules_to_add = modules_to_add or set()
        self.failed_submission_ids = failed_submission_ids or []


class BaseConverger(abc.ABC):
    """
    Base class for language-specific convergers.
    
    Subclasses must implement:
    - LANGUAGE_FAMILY: str
    - PATTERNS: List[str] - regex patterns for missing module errors
    - STDLIB: Set[str] - standard library modules (don't try to install)
    - update_manifest() - how to add modules to requirements
    """
    
    LANGUAGE_FAMILY: str = ""
    NETWORK_TIMEOUT = 10 # seconds
    PATTERNS: List[str] = []
    STDLIB: Set[str] = set()
    
    # Threshold: Number of submissions that must fail with same module before adding
    MODULE_THRESHOLD = 3
    
    # Stability threshold: If success rate > this, don't auto-add
    STABILITY_THRESHOLD = 0.8
    
    # Minimum runs before applying stability check
    MIN_RUNS_FOR_STABILITY = 5

    @classmethod
    def extract_missing_modules(cls, logs: str) -> Set[str]:
        """Extract missing module names from error logs."""
        modules = set()
        for pattern in cls.PATTERNS:
            matches = re.findall(pattern, logs)
            for m in matches:
                if cls._is_valid_module(m):
                    modules.add(m)
        return modules

    @classmethod
    def _is_valid_module(cls, module: str) -> bool:
        """Validate module name - not stdlib, valid format."""
        if len(module) < 2 or len(module) > 50:
            return False
        if not re.match(r'^[\w\-\.]+$', module):
            return False
        base_module = module.split('.')[0]
        if base_module in cls.STDLIB:
            return False
        return True

    @classmethod
    @abc.abstractmethod
    def update_manifest(cls, current_manifest: str, modules: Set[str]) -> Tuple[str, Set[str]]:
        """
        Update manifest with new modules.
        
        Returns:
            Tuple[str, Set[str]]: (updated_manifest, modules_that_were_added)
        """
        pass


class PythonConverger(BaseConverger):
    """Converger for Python environments."""
    
    LANGUAGE_FAMILY = "python"
    PATTERNS = [
        r"ModuleNotFoundError: No module named '([\w\-]+)'",
        r"ImportError: No module named ([\w\-]+)",
        r"CODEPOST_AUTO_INSTALL_SUCCESS: ([\w\-]+)"
    ]
    STDLIB = {
        'os', 'sys', 'io', 're', 'json', 'math', 'random', 'datetime', 'time',
        'collections', 'itertools', 'functools', 'operator', 'string', 'types',
        'copy', 'pprint', 'pickle', 'shelve', 'dbm', 'sqlite3', 'csv', 'configparser',
        'pathlib', 'tempfile', 'shutil', 'glob', 'subprocess', 'threading', 'multiprocessing',
        'socket', 'ssl', 'email', 'html', 'xml', 'urllib', 'http', 'ftplib', 'smtplib',
        'logging', 'warnings', 'unittest', 'doctest', 'typing', 'abc', 'contextlib',
        'ast', 'dis', 'inspect', 'traceback', 'gc', 'weakref', 'array', 'struct',
        'codecs', 'unicodedata', 'locale', 'secrets', 'hashlib', 'hmac', 'base64',
        'binascii', 'zlib', 'gzip', 'bz2', 'lzma', 'zipfile', 'tarfile', 'argparse',
        'getopt', 'optparse', 'errno', 'ctypes', 'platform', 'sysconfig', 'builtins',
        'importlib', 'pkgutil', 'modulefinder', 'runpy', 'symtable', 'token',
        'tokenize', 'keyword', 'linecache', 'pickletools', 'pdb', 'profile', 'cProfile',
        'timeit', 'trace', 'atexit', 'signal', 'code', 'codeop', 'pyclbr',
        'tabnanny', 'py_compile', 'compileall', 'dataclasses', 'enum', 'graphlib',
    }

    @classmethod
    def update_manifest(cls, current_manifest: str, modules: Set[str]) -> Tuple[str, Set[str]]:
        """Add modules to requirements.txt format."""
        lines = set(current_manifest.splitlines()) if current_manifest else set()
        added = set()
        for mod in modules:
            if mod not in lines:
                lines.add(mod)
                added.add(mod)
        return ("\n".join(sorted(lines)), added)


class NodeConverger(BaseConverger):
    """Converger for Node.js/JavaScript environments."""
    
    LANGUAGE_FAMILY = "javascript"
    PATTERNS = [
        r"Error: Cannot find module '([\w\-\/]+)'"
    ]
    STDLIB = {'fs', 'path', 'http', 'https', 'url', 'util', 'os', 'crypto', 'events', 'stream', 'buffer'}

    @classmethod
    def update_manifest(cls, current_manifest: str, modules: Set[str]) -> Tuple[str, Set[str]]:
        """Add modules to package.json format."""
        try:
            data = json.loads(current_manifest) if current_manifest.strip() else {"dependencies": {}}
        except json.JSONDecodeError:
            data = {"dependencies": {}}
        
        if "dependencies" not in data:
            data["dependencies"] = {}
        
        added = set()
        for mod in modules:
            if mod not in data["dependencies"]:
                data["dependencies"][mod] = "*"
                added.add(mod)
        
        return (json.dumps(data, indent=2), added)


class RConverger(BaseConverger):
    """Converger for R environments."""
    
    LANGUAGE_FAMILY = "r"
    PATTERNS = [
        r"there is no package called ['\"\u2018]([\w\.]+)['\"\u2019]",
        r"CODEPOST_AUTO_INSTALL_SUCCESS: ([\w\.-]+)"
    ]
    STDLIB = {'base', 'stats', 'graphics', 'grDevices', 'utils', 'datasets', 'methods', 'grid'}

    @classmethod
    def update_manifest(cls, current_manifest: str, modules: Set[str]) -> Tuple[str, Set[str]]:
        """Add modules to line-based format (like requirements.txt)."""
        lines = set(current_manifest.splitlines()) if current_manifest else set()
        added = set()
        for mod in modules:
            if mod not in lines:
                lines.add(mod)
                added.add(mod)
        return ("\n".join(sorted(lines)), added)


class JavaConverger(BaseConverger):
    """Converger for Java environments with Maven pom.xml support."""
    
    LANGUAGE_FAMILY = "java"
    PATTERNS = [
        r"package ([\w\.]+) does not exist"
    ]
    STDLIB = {'java', 'javax', 'sun', 'com.sun'}

    # Test/support libraries baked into the java-27 image at /opt/codepost/libs
    # (see autograder/testUtils/libs-pom.xml). The converger must NOT add these to a
    # per-assignment pom — doing so could pull a second, conflicting copy onto the
    # classpath. Treated like stdlib: never converged.
    BAKED = {
        'org.junit', 'org.mockito', 'org.opentest4j', 'net.bytebuddy',
        'org.apache.commons.csv', 'org.hamcrest',
    }

    # Mapping of package prefixes to Maven coordinates
    # Format: prefix -> (groupId, artifactId, version, scope)
    # NOTE: JUnit/Mockito are baked (see BAKED) and are intentionally absent here so
    # the converger never re-adds them. Versions kept current for the rest.
    MAVEN_MAPPINGS = {
        'com.google.gson': ('com.google.code.gson', 'gson', '2.11.0', None),
        'org.testng': ('org.testng', 'testng', '7.10.2', 'test'),
        'org.apache.commons.lang': ('org.apache.commons', 'commons-lang3', '3.14.0', None),
        'org.apache.commons.io': ('commons-io', 'commons-io', '2.16.1', None),
        'org.apache.commons.collections': ('org.apache.commons', 'commons-collections4', '4.4', None),
        'org.apache.http': ('org.apache.httpcomponents', 'httpclient', '4.5.14', None),
        'com.fasterxml.jackson': ('com.fasterxml.jackson.core', 'jackson-databind', '2.17.1', None),
        'org.slf4j': ('org.slf4j', 'slf4j-api', '2.0.13', None),
        'org.json': ('org.json', 'json', '20240303', None),
        'com.google.guava': ('com.google.guava', 'guava', '33.2.1-jre', None),
    }

    @classmethod
    def _is_valid_module(cls, module: str) -> bool:
        """Java packages - check prefix against stdlib and baked libraries."""
        for std in cls.STDLIB:
            if module.startswith(std):
                return False
        for baked in cls.BAKED:
            if module == baked or module.startswith(baked):
                return False
        return len(module) >= 2 and re.match(r'^[\w\.]+$', module) is not None

    @classmethod
    def _find_maven_dependency(cls, package: str) -> Optional[Tuple[str, str, str, Optional[str]]]:
        """
        Find Maven coordinates for a package.
        
        Strategy:
        1. Check static mappings first (fast, reliable for common packages)
        2. If not found, query Maven Central search API
        """
        # 1. Check static mappings first
        for prefix, coords in cls.MAVEN_MAPPINGS.items():
            if package.startswith(prefix):
                return coords
        
        # 2. Try Maven Central search API
        resolved = cls._resolve_from_maven_api(package)
        if resolved:
            return resolved
        
        return None

    @classmethod
    def _resolve_from_maven_api(cls, package: str) -> Optional[Tuple[str, str, str, Optional[str]]]:
        """
        Query Maven Central search API to find artifact for a package.
        
        Uses fc: (fully qualified class) search to find artifacts.
        """
        try:
            import requests
            
            # Query Maven Central
            url = "https://search.maven.org/solrsearch/select"
            params = {
                "q": f"fc:{package}",
                "rows": "5",
                "wt": "json"
            }
            # Setup session with retries
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            
            session = requests.Session()
            retry_strategy = Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET"]
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session.mount("https://", adapter)
            session.mount("http://", adapter)

            resp = session.get(url, params=params, timeout=cls.NETWORK_TIMEOUT)
            if resp.status_code != 200:
                return None
            
            data = resp.json()
            docs = data.get('response', {}).get('docs', [])
            if not docs:
                return None
            
            # Heuristic: Prefer result where GroupID shares prefix with package
            best_doc = docs[0]
            best_score = 0
            
            for doc in docs:
                group = doc.get('g', '')
                # Calculate match score (how much of group matches package)
                score = 0
                if package.startswith(group):
                    score = len(group)
                
                if score > best_score:
                    best_score = score
                    best_doc = doc
            
            group_id = best_doc.get('g')
            artifact_id = best_doc.get('a')
            version = best_doc.get('v', 'LATEST')
            
            if group_id and artifact_id:
                logger.info(f"JavaConverger: Resolved '{package}' via Maven API -> {group_id}:{artifact_id}:{version}")
                return (group_id, artifact_id, version, None)
            
        except Exception as e:
            logger.debug(f"JavaConverger: Maven API resolution failed for '{package}': {e}")
        
        return None

    @classmethod
    def update_manifest(cls, current_manifest: str, modules: Set[str]) -> Tuple[str, Set[str]]:
        """
        Update Maven pom.xml with new dependencies.
        
        Generates or updates pom.xml format with <dependency> entries.
        """
        import xml.etree.ElementTree as ET
        
        added = set()
        added_artifacts = set()
        
        # Parse existing pom.xml or create new structure
        if current_manifest.strip():
            try:
                root = ET.fromstring(current_manifest)
                deps_elem = root.find('dependencies')
                if deps_elem is None:
                    deps_elem = ET.SubElement(root, 'dependencies')
                # Track existing artifacts
                for dep in deps_elem.findall('dependency'):
                    artifact = dep.find('artifactId')
                    if artifact is not None:
                        added_artifacts.add(artifact.text)
            except ET.ParseError:
                # Invalid XML, create new
                root = None
        else:
            root = None
        
        # Build new dependencies
        new_deps = []
        for mod in modules:
            maven_coords = cls._find_maven_dependency(mod)
            if maven_coords:
                group_id, artifact_id, version, scope = maven_coords
                if artifact_id not in added_artifacts:
                    new_deps.append(maven_coords)
                    added_artifacts.add(artifact_id)
                    added.add(mod)
                    logger.info(f"JavaConverger: Mapping '{mod}' to {group_id}:{artifact_id}:{version}")
            else:
                logger.warning(f"JavaConverger: No mapping for '{mod}', skipping")
        
        if not new_deps:
            return (current_manifest, added)
        
        # Generate pom.xml string
        deps_xml = ""
        for group_id, artifact_id, version, scope in new_deps:
            if scope:
                deps_xml += f'\n    <dependency><groupId>{group_id}</groupId><artifactId>{artifact_id}</artifactId><version>{version}</version><scope>{scope}</scope></dependency>'
            else:
                deps_xml += f'\n    <dependency><groupId>{group_id}</groupId><artifactId>{artifact_id}</artifactId><version>{version}</version></dependency>'
        
        if root is not None:
            # Append to existing
            deps_elem = root.find('dependencies')
            if deps_elem is None:
                deps_elem = ET.SubElement(root, 'dependencies')
            for group_id, artifact_id, version, scope in new_deps:
                dep = ET.SubElement(deps_elem, 'dependency')
                ET.SubElement(dep, 'groupId').text = group_id
                ET.SubElement(dep, 'artifactId').text = artifact_id
                ET.SubElement(dep, 'version').text = version
                if scope:
                    ET.SubElement(dep, 'scope').text = scope
            return (ET.tostring(root, encoding='unicode'), added)
        else:
            # Create new pom.xml
            pom = f'''<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.codepost</groupId>
  <artifactId>submission</artifactId>
  <version>1.0</version>
  <dependencies>{deps_xml}
  </dependencies>
</project>'''
            return (pom, added)


# Registry of convergers by language family
CONVERGERS: Dict[str, type] = {
    'python': PythonConverger,
    'javascript': NodeConverger,
    'node': NodeConverger,
    'r': RConverger,
    'java': JavaConverger,
}


class Converger:
    """
    Main convergence service.
    
    Uses language-specific convergers and tracks failed submissions
    to enable automatic rerun when dependencies are added.
    """

    @staticmethod
    def get_converger(language: str) -> Optional[type]:
        """Get the appropriate converger for a language."""
        language_key = language.lower()
        for family, converger_cls in CONVERGERS.items():
            if family in language_key:
                return converger_cls
        return None

    @staticmethod
    def record_successful_run(environment_id: int):
        """Record a successful run for stability tracking."""
        try:
            env = Environment.objects.get(pk=environment_id)
            env.successful_runs = F('successful_runs') + 1
            env.total_runs = F('total_runs') + 1
            env.save(update_fields=['successful_runs', 'total_runs'])
        except Exception as e:
            logger.error(f"Converger: Failed to record success: {e}")

    @staticmethod
    def record_failed_run(environment_id: int):
        """Record a failed run for stability tracking."""
        try:
            env = Environment.objects.get(pk=environment_id)
            env.total_runs = F('total_runs') + 1
            env.save(update_fields=['total_runs'])
        except Exception as e:
            logger.error(f"Converger: Failed to record failure: {e}")

    @classmethod
    def analyze_and_converge(
        cls,
        environment_id: int,
        logs: str,
        submission_id: Optional[int] = None
    ) -> Tuple[bool, Set[str], List[int]]:
        """
        Analyzes logs for missing dependencies using threshold heuristic.
        Tracks submission IDs per module for later rerun.
        
        Args:
            environment_id: The Environment ID
            logs: Execution logs to analyze
            submission_id: Optional submission ID for tracking
        
        Returns:
            Tuple[bool, Set[str], List[int]]: 
                (should_rerun, added_modules, submission_ids_to_rerun)
        """
        added_modules: Set[str] = set()
        submissions_to_rerun: List[int] = []
        
        try:
            env = Environment.objects.get(pk=environment_id)
            if not env.auto_detect:
                return (False, added_modules, submissions_to_rerun)

            converger_cls = cls.get_converger(env.language)
            if not converger_cls:
                return (False, added_modules, submissions_to_rerun)

            # Stability check
            if env.total_runs >= converger_cls.MIN_RUNS_FOR_STABILITY:
                success_rate = env.successful_runs / env.total_runs if env.total_runs > 0 else 0
                if success_rate >= converger_cls.STABILITY_THRESHOLD:
                    logger.info(f"Converger: Env {environment_id} is stable ({success_rate:.0%})")
                    return (False, added_modules, submissions_to_rerun)

            # Extract missing modules
            missing_modules = converger_cls.extract_missing_modules(logs)
            if not missing_modules:
                return (False, added_modules, submissions_to_rerun)

            logger.info(f"Converger: Found missing modules: {missing_modules}")

            # Update convergence stats with submission tracking
            # Format: {module: {"count": N, "submissions": [id1, id2, ...]}}
            stats: Dict[str, Any] = env.convergence_stats or {}
            modules_hitting_threshold = set()
            
            for mod in missing_modules:
                if mod not in stats:
                    stats[mod] = {"count": 0, "submissions": []}
                
                stats[mod]["count"] += 1
                if submission_id and submission_id not in stats[mod]["submissions"]:
                    stats[mod]["submissions"].append(submission_id)
                
                if stats[mod]["count"] >= converger_cls.MODULE_THRESHOLD:
                    logger.info(f"Converger: Module '{mod}' hit threshold")
                    modules_hitting_threshold.add(mod)
                    # Collect submissions to rerun
                    submissions_to_rerun.extend(stats[mod]["submissions"])
                else:
                    logger.info(f"Converger: Module '{mod}' count: {stats[mod]['count']}/{converger_cls.MODULE_THRESHOLD}")
            
            # Save updated stats
            env.convergence_stats = stats
            env.save(update_fields=['convergence_stats'])
            
            if not modules_hitting_threshold:
                return (False, added_modules, submissions_to_rerun)

            # Update manifest with modules that hit threshold
            new_manifest, added_modules = converger_cls.update_manifest(
                env.requirements or "", 
                modules_hitting_threshold
            )
            
            if added_modules:
                # Remove modules from stats (they've been added)
                for mod in added_modules:
                    if mod in stats:
                        del stats[mod]
                
                env.requirements = new_manifest
                env.convergence_stats = stats
                env.save(update_fields=['requirements', 'convergence_stats'])
                logger.info(f"Converger: Added {added_modules} to Env {environment_id}")

            # Deduplicate submission IDs
            submissions_to_rerun = list(set(submissions_to_rerun))
            
            return (bool(added_modules), added_modules, submissions_to_rerun)

        except Exception as e:
            logger.error(f"Converger Error for Env {environment_id}: {e}")
            return (False, added_modules, submissions_to_rerun)

    @classmethod
    def rerun_failed_submissions(cls, environment_id: int, submission_ids: List[int]):
        """
        Queue reruns for submissions that failed due to missing dependencies.
        """
        if not submission_ids:
            return
        
        logger.info(f"Converger: Queueing reruns for {len(submission_ids)} submissions")
        
        try:
            from autograder.run import RunSubmission
            for sub_id in submission_ids:
                RunSubmission.delay(sub_id)  # type: ignore[operator]  # celery .delay() untyped
                logger.info(f"Converger: Queued rerun for submission {sub_id}")
        except Exception as e:
            logger.error(f"Converger: Failed to queue reruns: {e}")
