import pytest
from brain.app import Brain
from brain.config import Settings


@pytest.fixture
def brain(tmp_path):
    b = Brain(Settings(data_dir=tmp_path / "data"))
    yield b
    b.close()
