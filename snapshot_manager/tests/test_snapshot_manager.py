import pytest


@pytest.fixture
def sample_fixture() -> int:
    return 2


def test_addition(sample_fixture: int):
    assert sample_fixture == 2
