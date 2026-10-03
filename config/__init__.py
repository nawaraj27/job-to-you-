"""
Project package.

The Celery app is imported here so that `@shared_task` in any app binds to it
as soon as Django starts, whether the process is a web worker, a Celery worker
or a management command.
"""

from .celery import app as celery_app

__all__ = ("celery_app",)
