from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from ahp_acp import AcpClient
from tests.support import INIT, Recorder, argv_for

MakeClient = Callable[..., AcpClient]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def make_client(recorder: Recorder) -> MakeClient:
    def make(script: Mapping[str, Any], **kwargs: Any) -> AcpClient:
        script = {**script, "responses": {"initialize": INIT, **script.get("responses", {})}}
        kwargs.setdefault("on_update", recorder.on_update)
        kwargs.setdefault("on_permission", recorder.on_permission)
        spawn_options = kwargs.pop("spawn_options", {"grace": 0.3})
        return AcpClient(
            argv_for(script),
            spawn_options=spawn_options,
            client_info={"name": "test-client", "version": "0"},
            **kwargs,
        )

    return make
