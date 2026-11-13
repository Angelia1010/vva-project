"""每个测试在独立的临时目录里运行，避免写到项目的 data/ 目录（如业务库 data/ops.db）。"""
import pytest


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
