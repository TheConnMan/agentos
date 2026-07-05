import agentos_worker


def test_package_importable() -> None:
    assert agentos_worker.__version__ == "0.0.0"
