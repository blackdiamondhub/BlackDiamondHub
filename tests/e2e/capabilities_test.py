"""Smoke placeholder."""
import json, subprocess
import pytest
from stack_test import ENV

pytestmark = pytest.mark.skipif(ENV.get("SUITE") != "caps", reason="run_stack.sh SUITE=caps only")


def test_installed():
    out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    print(out)
