import agentos_runner


def test_package_importable() -> None:
    assert agentos_runner.__version__ == "0.0.0"
