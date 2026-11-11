"""
Production Pluggable Django App settings for ai_course_creator.
"""


def plugin_settings(settings):
    """
    Injects production settings into the Django settings object.

    The OpenRouter credentials (``OPENROUTER_API_KEY`` / ``OPENROUTER_MODEL``) are
    rendered into the CMS production settings from Tutor ``config.yml`` by the
    ``configuration_plugin`` patch, so there is nothing to override here beyond
    making sure the attributes exist.
    """
    if not hasattr(settings, "OPENROUTER_API_KEY"):
        settings.OPENROUTER_API_KEY = ""
    if not hasattr(settings, "OPENROUTER_MODEL"):
        settings.OPENROUTER_MODEL = "google/gemini-2.5-flash-lite"
    if not hasattr(settings, "AI_COURSE_CREATOR_MAX_UPLOAD_BYTES"):
        settings.AI_COURSE_CREATOR_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
    if not hasattr(settings, "AI_COURSE_CREATOR_ENABLED"):
        settings.AI_COURSE_CREATOR_ENABLED = True
