import json
import subprocess
import sys

import pytest

import ahp_host_flow
import ahp_host_flow.sandbox

CODE = """
import sys, json
import ahp_host_flow
import ahp_host_flow.sandbox.protocol
print(json.dumps(sorted(
    m for m in sys.modules
    if m.split('.')[0] in ('ahp_host', 'ahp_client', 'ahp_protocol', 'jsonschema')
    or m == 'ahp_host_flow' or m.startswith('ahp_host_flow.')
)))
"""


def test_version() -> None:
    assert ahp_host_flow.__version__


def test_package_exports_resolve() -> None:
    for package in (ahp_host_flow, ahp_host_flow.sandbox):
        for name in package.__all__:
            assert getattr(package, name) is not None
    with pytest.raises(AttributeError):
        ahp_host_flow.nope  # noqa: B018


def test_package_import_is_light() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", CODE],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(result.stdout) == [
        "ahp_host_flow",
        "ahp_host_flow.sandbox",
        "ahp_host_flow.sandbox.protocol",
    ]
