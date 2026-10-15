"""Дымовая проверка WS ouroboros изнутри сети aifa-sym (0.6): рукопожатие и один ход.

Запускается одноразовым контейнером из образа ouroboros (в нём есть `websockets`), см. `run.sh`.
Заголовки владельца здесь ставит скрипт, а не `ouro_client`: проверяется сам контур, не служба.

    python ws_chat.py --url ws://ouroboros:8080/ws --prompt "привет"
    python ws_chat.py --url ws://ouroboros:8080/ws --no-owner     # ждёт отказ рукопожатия
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid

import websockets

OWNER_HEADER = "X-Ouroboros-Owner-Id"
CHAT_HEADER = "X-Ouroboros-Chat-Id"


def _headers(args: argparse.Namespace) -> dict[str, str]:
    headers = {CHAT_HEADER: args.chat}
    if not args.no_owner:
        headers[OWNER_HEADER] = args.owner
    return headers


async def _connect(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    headers = _headers(args)
    try:
        return await websockets.connect(args.url, additional_headers=headers, open_timeout=20)
    except TypeError:  # websockets < 14: параметр называется extra_headers
        return await websockets.connect(args.url, extra_headers=headers, open_timeout=20)


async def _turn(ws, prompt: str, timeout: float) -> tuple[str, list[str]]:  # type: ignore[no-untyped-def]
    await ws.send(
        json.dumps(
            {"type": "chat", "content": prompt, "client_message_id": f"smoke-{uuid.uuid4().hex[:8]}"},
            ensure_ascii=False,
        )
    )
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"нет ответа ассистента за {timeout:.0f} с; кадры: {seen}")
        frame = json.loads(await asyncio.wait_for(ws.recv(), left))
        kind = frame.get("type")
        seen.append(f"{kind}:{frame.get('role', '')}{':progress' if frame.get('is_progress') else ''}")
        if kind == "error":
            raise RuntimeError(f"кадр error: {frame}")
        if kind == "chat" and frame.get("role") == "assistant" and not frame.get("is_progress"):
            return str(frame.get("content", "")), seen


async def main(args: argparse.Namespace) -> int:
    if args.no_owner:
        try:
            ws = await _connect(args)
        except Exception as exc:  # любой отказ рукопожатия: 403 до accept или закрытие 1008
            print(f"OK отказ рукопожатия без {OWNER_HEADER}: {type(exc).__name__}")
            return 0
        await ws.close()
        print(f"FAIL рукопожатие без {OWNER_HEADER} принято — контур открыт любому")
        return 1

    ws = await _connect(args)
    try:
        reply, frames = await _turn(ws, args.prompt, args.timeout)
    finally:
        await ws.close()
    print(f"кадры: {' '.join(frames)}")
    print(f"ответ ({len(reply)} знаков): {reply[:600]}")
    if args.expect and args.expect.lower() not in reply.lower():
        print(f"FAIL в ответе нет {args.expect!r}")
        return 1
    if not reply.strip():
        print("FAIL пустой ответ")
        return 1
    print("OK ответ модели получен")
    return 0


def parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://ouroboros:8080/ws")
    parser.add_argument("--owner", default="smoke")
    parser.add_argument("--chat", default="smoke-1")
    parser.add_argument("--prompt", default="привет")
    parser.add_argument("--expect", default="")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--no-owner", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse())))
