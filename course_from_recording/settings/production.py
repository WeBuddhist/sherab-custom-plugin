"""
Production Pluggable Django App settings for course_from_recording.
"""


def plugin_settings(settings):
    """
    Injects production settings into the Django settings object.

    STUDIO_TOOL_SERVICE comes from the studio_tool_service Tutor plugin's CMS
    settings patch; this only makes sure the attributes exist.
    """
    if not hasattr(settings, "STUDIO_TOOL_SERVICE"):
        settings.STUDIO_TOOL_SERVICE = {}
    if not hasattr(settings, "COURSE_FROM_RECORDING_MAX_UPLOAD_MB"):
        settings.COURSE_FROM_RECORDING_MAX_UPLOAD_MB = 2048
