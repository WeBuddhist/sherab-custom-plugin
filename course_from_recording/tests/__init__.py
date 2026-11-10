"""Tests for course_from_recording. Run under CMS test settings."""

from unittest import mock


def views_outside_transactions(test):
    """
    The author views run outside a database transaction (see views.AuthorAPIView), but every test runs inside
    one. Django REST framework marks the open transaction as broken when a view answers 400; in a test that is
    the test's own, so the test's next query would fail. Only in tests: in production there is none to mark.
    """
    patcher = mock.patch("rest_framework.views.set_rollback")
    patcher.start()
    test.addCleanup(patcher.stop)
