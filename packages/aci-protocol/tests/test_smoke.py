import aci_protocol


def test_package_importable() -> None:
    assert aci_protocol.__version__ == "0.0.0"
