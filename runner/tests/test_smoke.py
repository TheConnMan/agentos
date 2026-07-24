import curie_runner


def test_package_importable() -> None:
    assert curie_runner.__version__ == "0.0.0"
