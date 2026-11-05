import pytest
from pipeline_support import build_repo


@pytest.fixture
def repo(tmp_path):
    return build_repo(tmp_path)
