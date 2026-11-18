from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from infra.runtime_schema import RuntimeSchemaOutdatedError, assert_runtime_schema


@pytest.mark.parametrize(
    "present,version,ok", [(False, None, False), (True, 33, False), (True, 34, True), (True, 35, False)]
)
def test_schema_contract_is_read_only(present, version, ok):
    statements = []

    class Connection:
        def execute(self, sql):
            statements.append(str(sql))
            return SimpleNamespace(
                scalar_one=lambda: "gigaagent_schema_version" if present else None, scalar_one_or_none=lambda: version
            )

    @contextmanager
    def connect():
        yield Connection()

    engine = SimpleNamespace(connect=connect)
    if ok:
        assert_runtime_schema(engine)
    else:
        with pytest.raises(RuntimeSchemaOutdatedError):
            assert_runtime_schema(engine)
    assert all(s.startswith("SELECT ") for s in statements)
