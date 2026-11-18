"""Привязка повторной доставки инструмента к одному заданию (ADR-0098)."""
import asyncio
import time
import hashlib
import json
from dataclasses import dataclass

from finengine.assistant.tool_call import BoundCall
from finengine.store import state


@dataclass(frozen=True)
class Operation:
    owner: str
    key: str
    fingerprint: str
    job_id: str
    created: bool
    result: str | None

    async def wait(self) -> str:
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            doc = await asyncio.to_thread(state.get, "tool_operation", owner=self.owner, key=self.key)
            if doc and doc.get("result") is not None:
                return doc["result"]
            await asyncio.sleep(0.5)
        raise RuntimeError("Принятое задание не завершено; сбор не возобновляется, нужен новый ref")

    def finish(self, text: str) -> None:
        state.put("tool_operation", owner=self.owner, key=self.key, doc={
            "fingerprint": self.fingerprint, "job_id": self.job_id, "result": text,
        })


def claim_operation(call: BoundCall, name: str, job_id: str) -> Operation:
    identity = (call.ctx.owner_id, call.ctx.run_id, call.ctx.task_id, call.ctx.operation_id)
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([name, call.args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    doc = state.claim("tool_operation", owner=call.record.user_id, key=key, doc={
        "fingerprint": fingerprint, "job_id": job_id, "result": None,
    })
    if doc["fingerprint"] != fingerprint:
        raise ValueError("operation_id уже использован с другими аргументами")
    return Operation(str(call.record.user_id), key, fingerprint, doc["job_id"], doc["job_id"] == job_id, doc["result"])
