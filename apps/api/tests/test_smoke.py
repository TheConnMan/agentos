import curie_api


def test_package_importable() -> None:
    assert curie_api.__version__ == "0.0.0"
