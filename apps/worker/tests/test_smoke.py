import curie_worker


def test_package_importable() -> None:
    assert curie_worker.__version__ == "0.0.0"
