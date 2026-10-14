from ouroboros.app.server_runtime import has_remote_provider, has_supervisor_provider
from ouroboros.storage.settings.schema import AppSettings


def test_gigachat_base_url_with_verify_ssl_false_enables_supervisor():
    settings = AppSettings(
        GIGACHAT_B2BANK_BASE_URL="https://giga.example",
        GIGACHAT_B2BANK_VERIFY_SSL=False,
        OUROBOROS_MODEL="gigachat-b2bank::GigaChat-Pro",
    )

    assert has_remote_provider(settings) is True
    assert has_supervisor_provider(settings) is True


def test_gigachat_base_url_without_certs_enables_supervisor_when_verify_ssl_true():
    settings = AppSettings(
        GIGACHAT_B2BANK_BASE_URL="https://giga.example",
        GIGACHAT_B2BANK_VERIFY_SSL=True,
        OUROBOROS_MODEL="gigachat-b2bank::GigaChat-Pro",
    )

    assert has_remote_provider(settings) is True
    assert has_supervisor_provider(settings) is True
