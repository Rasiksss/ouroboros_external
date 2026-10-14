from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from ouroboros.storage.settings.schema import AppSettings


def test_the_kafka_block_has_its_defaults():
    settings = AppSettings()

    assert {key: settings.get(key) for key in type(settings).model_fields if key.startswith("OUROBOROS_KAFKA_")} == {
        "OUROBOROS_KAFKA_BOOTSTRAP_SERVERS": "",
        "OUROBOROS_KAFKA_TOPIC_PREFIX": "ouroboros",
        "OUROBOROS_KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
        "OUROBOROS_KAFKA_SASL_MECHANISM": "PLAIN",
        "OUROBOROS_KAFKA_SASL_USERNAME": "",
        "OUROBOROS_KAFKA_SASL_PASSWORD": "",
        "OUROBOROS_KAFKA_SSL_CA_PATH": "",
        "OUROBOROS_KAFKA_SSL_CERT_PATH": "",
        "OUROBOROS_KAFKA_SSL_KEY_PATH": "",
        "OUROBOROS_KAFKA_API_VERSION": "",
        "OUROBOROS_KAFKA_MAX_MESSAGE_BYTES": 33_554_432,
    }
    assert isinstance(settings.OUROBOROS_KAFKA_SASL_PASSWORD, SecretStr)


@pytest.mark.parametrize("value", [0, -1])
def test_the_message_size_ceiling_refuses_a_non_positive_value_at_load(value):
    with pytest.raises(ValidationError, match="OUROBOROS_KAFKA_MAX_MESSAGE_BYTES"):
        AppSettings.model_validate({"OUROBOROS_KAFKA_MAX_MESSAGE_BYTES": value})


def test_a_removed_topology_key_left_in_the_environment_is_accepted_and_read_by_nothing():
    removed = {
        "OUROBOROS_KAFKA_EVENT_TOPIC_PARTITIONS": 12,
        "OUROBOROS_KAFKA_REPLICATION_FACTOR": 3,
        "OUROBOROS_KAFKA_MIN_INSYNC_REPLICAS": 2,
    }

    AppSettings.model_validate(removed)

    assert set(removed).isdisjoint(AppSettings.model_fields)


@pytest.mark.parametrize(
    "key,value",
    [("OUROBOROS_KAFKA_SECURITY_PROTOCOL", "SASL_GSSAPI"), ("OUROBOROS_KAFKA_SASL_MECHANISM", "OAUTHBEARER")],
)
def test_kafka_security_literals_refuse_an_unsupported_value_at_load(key, value):
    with pytest.raises(ValidationError, match=key):
        AppSettings.model_validate({key: value})
