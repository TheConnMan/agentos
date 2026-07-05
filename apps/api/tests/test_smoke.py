import agentos_api


def test_package_importable() -> None:
    assert agentos_api.__version__ == "0.0.0"
