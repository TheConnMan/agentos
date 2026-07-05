import plugin_format


def test_package_importable() -> None:
    assert plugin_format.__version__ == "0.0.0"
