from app.json_config import default_software_providers


def test_default_software_providers_contains_avaya() -> None:
    providers = default_software_providers()
    assert "avaya_workplace" in providers
