"""
Offline Content - App Configuration.
"""
import logging

from django.apps import AppConfig

log = logging.getLogger(__name__)


class OfflineContentConfig(AppConfig):
    name = "offline_content"
    label = "offline_content"
    verbose_name = "Offline Content"

    # Required by edx_django_utils.plugins.get_plugin_apps() to register in INSTALLED_APPS.
    plugin_app = {}

    def ready(self):
        from . import signals  # pylint: disable=unused-import, import-outside-toplevel

        log.debug("%s is ready.", self.label)
