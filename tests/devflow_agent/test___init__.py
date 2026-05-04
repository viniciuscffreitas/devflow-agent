"""Tests for devflow_agent package metadata."""

import devflow_agent


def test_version_is_set():
    assert devflow_agent.__version__ == "0.2.0"


def test_version_is_string():
    assert isinstance(devflow_agent.__version__, str)
