# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Check every dependency codePost needs and print pass/warn/fail per item. For ops
triage after a deploy or an outage. Read-only apart from a cache probe key.

    manage.py doctor            # database, migrations, broker, channel layer, workers, beat, docker, secrets, OAuth
    manage.py doctor --ai       # also fire a tiny completion through each configured AI provider
    manage.py doctor --json

Exit status is 1 when any check fails, so it can gate a deploy script.
"""
import json
import sys
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

OK, WARN, FAIL, SKIP = 'ok', 'warning', 'error', 'skipped'


class Command(BaseCommand):
    help = (
        "Check database, migrations, Celery broker/workers/beat, channel layer, Docker, "
        "secrets and the OAuth client (plus AI providers with --ai). Exits 1 on any failure."
    )

    def add_arguments(self, parser):
        parser.add_argument('--ai', action='store_true',
                            help='Also test each configured AI provider with a tiny completion (network).')
        parser.add_argument('--json', action='store_true', help='Emit JSON instead of text.')

    def handle(self, *args, **options):
        checks = [
            ('database', self._database),
            ('migrations', self._migrations),
            ('cache', self._cache),
            ('celery broker', self._broker),
            ('channel layer', self._channel_layer),
            ('celery workers', self._workers),
            ('celery beat', self._beat),
            ('docker', self._docker),
            ('disk', self._disk),
            ('secrets', self._secrets),
            ('oauth client', self._oauth_client),
        ]
        if options['ai']:
            checks.append(('ai providers', self._ai_providers))

        results = []
        for name, fn in checks:
            try:
                result = fn()
            except Exception as exc:  # a check must never take the whole report down
                result = {'status': FAIL, 'label': 'Check crashed', 'detail': str(exc)}
            result['name'] = name
            results.append(result)

        failed = any(r['status'] == FAIL for r in results)
        if options['json']:
            self.stdout.write(json.dumps({'checked_at': timezone.now().isoformat(),
                                          'overall': FAIL if failed else (
                                              WARN if any(r['status'] == WARN for r in results) else OK),
                                          'checks': results}, indent=2, default=str))
        else:
            width = max(len(r['name']) for r in results)
            for r in results:
                style = {OK: self.style.SUCCESS, WARN: self.style.WARNING,
                         FAIL: self.style.ERROR, SKIP: self.style.NOTICE}[r['status']]
                status_txt = f"{r['status'].upper():<8}"
                self.stdout.write(f"{style(status_txt)} {r['name']:<{width}}  {r['label']}")
                if r.get('detail'):
                    self.stdout.write(f"{'':8} {'':{width}}  {r['detail']}")
        if failed:
            sys.exit(1)

    # ------------------------------------------------------------------ checks
    # The database/migrations/cache/workers/disk probes are the same ones behind the
    # superadmin System Health page (core.views.system), so the CLI and the UI agree.

    @staticmethod
    def _database():
        from core.views.system import _check_database
        return _check_database()

    @staticmethod
    def _migrations():
        from core.views.system import _check_migrations
        return _check_migrations()

    @staticmethod
    def _cache():
        from core.views.system import _check_cache
        return _check_cache()

    @staticmethod
    def _workers():
        from core.views.system import _check_celery
        return _check_celery()

    @staticmethod
    def _disk():
        from core.views.system import _check_disk
        return _check_disk()

    @staticmethod
    def _broker():
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            return {'status': SKIP, 'label': 'Eager mode (no broker needed)', 'detail': None}
        from core.views.system import _check_redis
        url = settings.CELERY_BROKER_URL
        if _check_redis() == OK:
            return {'status': OK, 'label': f'PING ok ({_redact(url)})', 'detail': None}
        return {'status': FAIL, 'label': f'Unreachable ({_redact(url)})',
                'detail': 'Celery tasks (autograder, AI, scheduled publishes) cannot be queued.'}

    @staticmethod
    def _channel_layer():
        url = getattr(settings, 'CHANNEL_LAYER_REDIS_URL', '')
        if not url:
            return {'status': SKIP, 'label': 'In-memory channel layer (dev)', 'detail': None}
        import redis
        try:
            client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
            try:
                client.ping()
            finally:
                client.close()
            return {'status': OK, 'label': f'PING ok ({_redact(url)})', 'detail': None}
        except Exception as exc:
            return {'status': FAIL, 'label': f'Unreachable ({_redact(url)})',
                    'detail': f'Websockets (environment shell, live updates) will not work: {exc}'}

    @staticmethod
    def _beat():
        """Beat has no ping; infer it from recent periodic-task results. The schedule's
        tightest entries run every 5 minutes, so nothing in 15 minutes means beat is down
        (or django_celery_results isn't storing results)."""
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            return {'status': SKIP, 'label': 'Eager mode (no beat)', 'detail': None}
        from django_celery_results.models import TaskResult
        since = timezone.now() - timedelta(minutes=15)
        scheduled = set(e['task'] for e in settings.CELERY_BEAT_SCHEDULE.values())
        recent = TaskResult.objects.filter(task_name__in=scheduled, date_created__gte=since)
        last = recent.order_by('-date_created').values_list('task_name', 'date_created').first()
        if last:
            return {'status': OK, 'label': f'Running — last periodic task {last[0]} at {last[1]:%H:%M:%S}',
                    'detail': None}
        ever = TaskResult.objects.filter(task_name__in=scheduled).order_by('-date_created').values_list(
            'date_created', flat=True).first()
        detail = (f'Last periodic task result: {ever:%Y-%m-%d %H:%M}.' if ever
                  else 'No periodic task has ever recorded a result.')
        return {'status': FAIL, 'label': 'No periodic task ran in the last 15 minutes',
                'detail': detail + ' Scheduled publishes, feedback releases, quiz generation and '
                                   'attempt finalization depend on beat.'}

    @staticmethod
    def _docker():
        """Only meaningful where the autograder runs (worker hosts); elsewhere a missing
        daemon is expected, so it is reported as skipped rather than failed."""
        try:
            import docker
        except ImportError:
            return {'status': SKIP, 'label': 'docker SDK not installed (not a worker host)', 'detail': None}
        try:
            client = docker.from_env(timeout=3)
            try:
                client.ping()
                version = client.version().get('Version', '?')
            finally:
                client.close()
            return {'status': OK, 'label': f'Daemon reachable (v{version})', 'detail': None}
        except Exception as exc:
            return {'status': WARN, 'label': 'Daemon unreachable',
                    'detail': f'{exc}. Expected on API-only hosts; on a worker host the autograder cannot run.'}

    @staticmethod
    def _secrets():
        problems = []
        if settings.SECRET_KEY == 'your-secret-key-for-dev-only-must-be-at-least-32-bytes':
            problems.append('SECRET_KEY is the dev default')
        if settings.FIELD_ENCRYPTION_KEY == 'a8ZLWym0lKXZW2iwQw8mx3kdsX7EiGxjfznpPeGBZ4M=':
            problems.append('FIELD_ENCRYPTION_KEY is the dev default (encrypted fields are not secret)')
        if settings.DEBUG:
            problems.append('DEBUG=True')
        if not problems:
            return {'status': OK, 'label': 'Production secrets set, DEBUG off', 'detail': None}
        status = WARN if settings.DEBUG and not getattr(settings, 'DOCKER', False) else FAIL
        return {'status': status, 'label': '; '.join(problems),
                'detail': 'Expected only on a local dev checkout.' if status == WARN else
                          'Must be fixed before this instance serves real users.'}

    @staticmethod
    def _oauth_client():
        from oauth2_provider.models import Application
        apps = Application.objects.filter(client_type=Application.CLIENT_PUBLIC,
                                          authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE)
        n = apps.count()
        if n == 0:
            return {'status': WARN, 'label': 'No public OAuth client registered',
                    'detail': 'Claude Desktop/claude.ai connectors rely on dynamic registration; '
                              'run seed_oauth_application for the fallback client.'}
        return {'status': OK, 'label': f'{n} public client(s): ' + ', '.join(apps.values_list('name', flat=True)[:5]),
                'detail': None}

    @staticmethod
    def _ai_providers():
        """One tiny completion per distinct provider configuration (organizations with a
        provider, plus courses using their own settings)."""
        import asyncio
        from core.models import Course, Organization
        from core.services.ai_service import AIService

        configs = []
        for org in Organization.objects.exclude(ai_provider__isnull=True).exclude(ai_provider=''):
            configs.append((f'org {org.id} {org.name}', org))
        for course in Course.objects.filter(ai_use_own_settings=True).exclude(ai_provider=''):
            configs.append((f'course {course.id} {course.name}', course))
        if not configs:
            return {'status': SKIP, 'label': 'No AI provider configured anywhere', 'detail': None}

        lines, failures = [], 0
        for label, holder in configs:
            svc = AIService.for_config(provider=holder.ai_provider or '', api_key=holder.ai_api_key or '',
                                       base_url=holder.ai_base_url or '', model=holder.ai_model or '')
            try:
                result = asyncio.run(svc.test_connection())
                ok = bool(result.get('success'))
            except Exception as exc:
                result, ok = {'error': str(exc)}, False
            failures += not ok
            lines.append(f"{label}: {holder.ai_provider}/{svc.model} — "
                         f"{'ok' if ok else 'FAILED: ' + str(result.get('error') or result)[:160]}")
        return {'status': FAIL if failures else OK,
                'label': f'{len(configs) - failures}/{len(configs)} provider configs answered',
                'detail': '\n' + '\n'.join(f'{"":17}{line}' for line in lines)}


def _redact(url):
    """Hide a password in a redis:// URL."""
    if '@' in url and '://' in url:
        scheme, rest = url.split('://', 1)
        return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"
    return url
