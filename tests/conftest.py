import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JSHQ_DATA_DIR", str(tmp_path))
    import storage
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SYSTEM_CONFIG_PATH", tmp_path / "system_config.json")
    def no_network(*args, **kwargs):
        raise AssertionError("network access forbidden in tests")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    return tmp_path
