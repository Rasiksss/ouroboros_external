from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest
from pydantic import SecretStr

from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.parametrize import parametrize_from_dict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER = "kafka-leak-check.invalid:9093"
USERNAME = "leak-check-user"
PASSWORD = "leak-check-password"

VALID_SETTINGS = {
    "OUROBOROS_KAFKA_BOOTSTRAP_SERVERS": f" {SERVER} , ,kafka-b.invalid:9094",
    "OUROBOROS_KAFKA_TOPIC_PREFIX": " fleet-a ",
    "OUROBOROS_KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT",
    "OUROBOROS_KAFKA_SASL_MECHANISM": "SCRAM-SHA-512",
    "OUROBOROS_KAFKA_SASL_USERNAME": USERNAME,
    "OUROBOROS_KAFKA_SASL_PASSWORD": PASSWORD,
    "OUROBOROS_KAFKA_API_VERSION": "3.9",
    "OUROBOROS_KAFKA_MAX_MESSAGE_BYTES": 4_194_304,
}


def _settings(**overrides) -> AppSettings:
    return AppSettings.model_validate({**VALID_SETTINGS, **overrides})


def _connection(**overrides):
    from infra.kafka_client import KafkaConnection

    fields = {
        "bootstrap_servers": (SERVER,),
        "topic_prefix": "fleet-a",
        "security_protocol": "PLAINTEXT",
        "sasl_mechanism": "PLAIN",
        "sasl_username": "",
        "sasl_password": SecretStr(""),
        "ssl_ca_path": "",
        "ssl_cert_path": "",
        "ssl_key_path": "",
        "api_version": (3, 9),
        "max_message_bytes": 33_554_432,
    }
    fields.update(overrides)
    return KafkaConnection(**fields)


def test_importing_the_description_loads_no_client_library_and_no_settings_store():
    code = (
        "import sys\n"
        "import infra.kafka_client as module\n"
        "for name in ('parse_kafka_bootstrap_servers', 'KafkaOffsetReset', 'KafkaConnection',"
        " 'IKafkaClientFactory', 'kafka_client_factory'):\n"
        "    getattr(module, name)\n"
        "leaked = [m for m in sys.modules if m == 'kafka' or m.startswith('kafka.')]\n"
        "assert not leaked, leaked\n"
        "assert 'ouroboros.storage.settings.store' not in sys.modules\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO_ROOT))

    assert proc.returncode == 0, proc.stderr


def test_the_bootstrap_parser_strips_splits_and_drops_empty_entries():
    from infra.kafka_client import parse_kafka_bootstrap_servers

    assert parse_kafka_bootstrap_servers(" a:1 ,, b:2,  ,c:3 ") == ["a:1", "b:2", "c:3"]
    assert parse_kafka_bootstrap_servers("") == []
    assert parse_kafka_bootstrap_servers(None) == []


def test_the_offset_reset_policies_are_the_two_a_backend_names():
    from infra.kafka_client import KafkaOffsetReset

    assert {member.name: member.value for member in KafkaOffsetReset} == {
        "EARLIEST": "earliest",
        "LATEST": "latest",
    }


def test_a_connection_is_parsed_from_every_setting_of_the_block(tmp_path):
    from infra.kafka_client import KafkaConnection

    paths = {name: tmp_path / f"{name}.pem" for name in ("ca", "cert", "key")}
    for path in paths.values():
        path.write_text("pem", encoding="utf-8")

    connection = KafkaConnection.from_settings(
        _settings(
            OUROBOROS_KAFKA_SECURITY_PROTOCOL="SASL_SSL",
            OUROBOROS_KAFKA_SSL_CA_PATH=f" {paths['ca']} ",
            OUROBOROS_KAFKA_SSL_CERT_PATH=str(paths["cert"]),
            OUROBOROS_KAFKA_SSL_KEY_PATH=str(paths["key"]),
        )
    )

    assert connection == _connection(
        bootstrap_servers=(SERVER, "kafka-b.invalid:9094"),
        topic_prefix="fleet-a",
        security_protocol="SASL_SSL",
        sasl_mechanism="SCRAM-SHA-512",
        sasl_username=USERNAME,
        sasl_password=SecretStr(PASSWORD),
        ssl_ca_path=str(paths["ca"]),
        ssl_cert_path=str(paths["cert"]),
        ssl_key_path=str(paths["key"]),
        api_version=(3, 9),
        max_message_bytes=4_194_304,
    )


refusal_cases = {
    "empty servers": {
        "overrides": {"OUROBOROS_KAFKA_BOOTSTRAP_SERVERS": " , "},
        "named": ["OUROBOROS_KAFKA_BOOTSTRAP_SERVERS", "OUROBOROS_BROKER_BACKEND"],
    },
    "an empty version": {
        "overrides": {"OUROBOROS_KAFKA_API_VERSION": " "},
        "named": ["OUROBOROS_KAFKA_API_VERSION"],
    },
    "a version with no minor": {
        "overrides": {"OUROBOROS_KAFKA_API_VERSION": "3"},
        "named": ["OUROBOROS_KAFKA_API_VERSION"],
    },
    "a version with a patch": {
        "overrides": {"OUROBOROS_KAFKA_API_VERSION": "3.7.1"},
        "named": ["OUROBOROS_KAFKA_API_VERSION"],
    },
    "a version with a letter": {
        "overrides": {"OUROBOROS_KAFKA_API_VERSION": "v3.7"},
        "named": ["OUROBOROS_KAFKA_API_VERSION"],
    },
}


@parametrize_from_dict(refusal_cases)
def test_unparsable_settings_are_refused_naming_keys_and_no_value(overrides, named):
    from infra.kafka_client import KafkaConnection

    with pytest.raises(RuntimeError) as refusal:
        KafkaConnection.from_settings(_settings(**overrides))

    message = str(refusal.value)
    assert [key for key in named if key not in message] == []
    assert [value for value in (SERVER, "kafka-leak-check.invalid", USERNAME, PASSWORD) if value in message] == []


unchecked_cases = {
    "a SASL user name under PLAINTEXT": {
        "overrides": {"OUROBOROS_KAFKA_SECURITY_PROTOCOL": "PLAINTEXT", "OUROBOROS_KAFKA_SASL_PASSWORD": ""},
        "field": "sasl_username",
        "value": USERNAME,
    },
    "a server entry without a port": {
        "overrides": {"OUROBOROS_KAFKA_BOOTSTRAP_SERVERS": "kafka-leak-check.invalid"},
        "field": "bootstrap_servers",
        "value": ("kafka-leak-check.invalid",),
    },
    "an illegal prefix": {
        "overrides": {"OUROBOROS_KAFKA_TOPIC_PREFIX": "bad:prefix"},
        "field": "topic_prefix",
        "value": "bad:prefix",
    },
}


@parametrize_from_dict(unchecked_cases)
def test_a_setting_the_connection_does_not_check_builds_without_a_refusal(overrides, field, value):
    from infra.kafka_client import KafkaConnection

    connection = KafkaConnection.from_settings(_settings(**overrides))

    assert getattr(connection, field) == value


def test_a_directly_built_connection_without_servers_is_refused():
    with pytest.raises(RuntimeError, match="OUROBOROS_KAFKA_BOOTSTRAP_SERVERS"):
        _connection(bootstrap_servers=())


def test_the_connections_representation_carries_no_password():
    connection = _connection(
        security_protocol="SASL_PLAINTEXT", sasl_username=USERNAME, sasl_password=SecretStr(PASSWORD)
    )

    assert PASSWORD not in repr(connection)
    assert PASSWORD not in str(connection)


def test_names_are_formed_from_the_prefix():
    connection = _connection(topic_prefix="fleet-a")

    assert connection.topic("inbound") == "fleet-a.inbound"
    assert connection.group_id("inbound", "supervisor") == "fleet-a.inbound.supervisor"


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    named = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    named |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    return named


def test_the_cache_never_names_the_kafka_description():
    for path in sorted((REPO_ROOT / "infra" / "cache").glob("*.py")):
        named = _imported_modules(path)
        assert not [name for name in named if name == "kafka" or name.startswith(("kafka.", "infra.kafka_client"))]


def test_the_kafka_description_names_no_redis_module_and_no_redis_setting():
    path = REPO_ROOT / "infra" / "kafka_client.py"
    named = _imported_modules(path)

    assert not [name for name in named if name == "redis" or name.startswith(("redis.", "infra.redis_client"))]
    assert "OUROBOROS_REDIS_" not in path.read_text(encoding="utf-8")


def test_the_zero_timeout_fetch_fits_inside_one_poll_slice_and_the_ceiling_is_no_constant():
    import infra.kafka_client as module

    assert module.KAFKA_ZERO_TIMEOUT_FETCH_MS <= 100 < module.KAFKA_POLL_SLICE_MS
    assert not hasattr(module, "KAFKA_MAX_MESSAGE_BYTES")
