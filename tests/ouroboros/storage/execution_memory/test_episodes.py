from ouroboros.storage.execution_memory.repository import SqlExecutionMemoryRepository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.execution_memory_conformance import ExecutionMemoryRepositoryConformance


class TestSqlEpisodes(ExecutionMemoryRepositoryConformance):
    def make_repository(self) -> SqlExecutionMemoryRepository:
        return sql_execution_memory_repository()
