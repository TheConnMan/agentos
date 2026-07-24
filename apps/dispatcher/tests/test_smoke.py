import curie_dispatcher


def test_package_importable() -> None:
    assert curie_dispatcher.__version__ == "0.0.0"
