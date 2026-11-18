from sqlalchemy import text
from sqlalchemy.engine import Engine

from infra.sql_connection import sql_connection
from ouroboros.storage.runtime_models import RuntimeSession
from ouroboros.storage.runtime_serialization import dump_json
from ouroboros.utils import utc_now_iso
from supervisor.interfaces.runtime_sessions import IRuntimeSessionRepository


class SqlRuntimeSessionRepository(IRuntimeSessionRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_or_update_session(self, session: RuntimeSession) -> RuntimeSession:
        """One statement: an upsert whose values come from the caller, not from a prior read."""
        now = utc_now_iso()
        if not session.created_at:
            session.created_at = now
        session.last_seen_at = session.last_seen_at or now
        payload = session.model_dump(mode="json")
        with sql_connection(self._engine) as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO sessions (
                        session_id, source, owner_external_id, auth_context_hash,
                        status, created_at, last_seen_at, expires_at, payload_json
                    ) VALUES (
                        :session_id, :source, :owner_external_id, :auth_context_hash,
                        :status, :created_at, :last_seen_at, :expires_at, :payload_json
                    )
                    ON CONFLICT(session_id) DO UPDATE SET
                        source=excluded.source,
                        owner_external_id=excluded.owner_external_id,
                        auth_context_hash=excluded.auth_context_hash,
                        status=excluded.status,
                        last_seen_at=excluded.last_seen_at,
                        expires_at=excluded.expires_at,
                        payload_json=excluded.payload_json
                    """
                ),
                {
                    "session_id": session.session_id,
                    "source": session.source,
                    "owner_external_id": session.owner_external_id,
                    "auth_context_hash": session.auth_context_hash,
                    "status": session.status,
                    "created_at": session.created_at,
                    "last_seen_at": session.last_seen_at,
                    "expires_at": session.expires_at,
                    "payload_json": dump_json(payload),
                },
            )
            conn.commit()
        return session
