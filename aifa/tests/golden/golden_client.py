"""HTTP-клиент записи: вход по учётке, запросы к службе, чтение SSE (ADR-0005, F5).

Только стандартная библиотека (ADR-0020). Запись ходит лишь на старый dev AIFA или на loopback
(туннель к `aifa-sym`): любой другой адрес отклоняется до первого запроса. Cookie сессии живёт в
памяти объекта и нигде не сохраняется — ни в записи, ни в файле.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

# Адреса, на которые запись вообще может ходить. Менять — только решением владельца (F9).
ALLOWED_HOSTS = frozenset({"finagent.homyakagent.ru", "127.0.0.1", "localhost"})
UNSET_BUILD = "не указана"


class ClientError(RuntimeError):
    """Отказ стенда или недоступное предусловие; текст — для оператора."""


class StandError(ClientError):
    """Адрес не из списка стендов, на которые разрешено ходить записи."""


def check_stand(url: str) -> str:
    """Хост стенда, если адрес разрешён; иначе `StandError` до всякого сетевого вызова."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or host not in ALLOWED_HOSTS:
        allowed = ", ".join(sorted(ALLOWED_HOSTS))
        raise StandError(
            f"адрес {host or url!r} не разрешён записи: ходить можно только на {allowed} "
            "(старый dev AIFA или туннель к aifa-sym); prod — никогда"
        )
    return host


@dataclass(frozen=True)
class ClientSettings:
    base_url: str
    username: str
    password: str
    request_timeout_s: float = 60.0
    stream_timeout_s: float = 120.0  # кадр-биение идёт каждые 15 с: тишина дольше — обрыв
    turn_timeout_s: float = 1800.0  # самый длинный ход на dev — 14,4 мин (34-status.md)
    parse_timeout_s: float = 600.0
    poll_interval_s: float = 3.0
    build: str = UNSET_BUILD

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ClientSettings:
        def need(name: str) -> str:
            value = env.get(name, "")
            if not value:
                raise ClientError(f"не задана переменная окружения {name}")
            return value

        return cls(
            base_url=need("AIFA_GOLDEN_BASE_URL").rstrip("/"),
            username=need("AIFA_GOLDEN_USER"),
            password=need("AIFA_GOLDEN_PASSWORD"),
            build=env.get("AIFA_GOLDEN_BUILD") or UNSET_BUILD,
        )


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def detail(self) -> str:
        """Текст отказа службы (`detail` FastAPI) или начало тела."""
        try:
            detail = self.json().get("detail", "")
        except (ValueError, AttributeError):
            return self.body.decode("utf-8", "replace")[:200]
        return detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)


class ApiSession:
    def __init__(self, settings: ClientSettings) -> None:
        self.stand = check_stand(settings.base_url)
        self._settings = settings
        self._cookie = ""

    def login(self) -> None:
        reply = self.request(
            "POST",
            "/auth/login",
            json_body={"username": self._settings.username, "password": self._settings.password},
        )
        if reply.status != 200:
            raise ClientError(f"вход не удался: HTTP {reply.status}: {reply.detail()}")
        if not self._cookie:
            raise ClientError("вход не удался: служба не выдала cookie сессии")

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        data: bytes | None = None,
        content_type: str = "",
    ) -> Response:
        headers = {}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        try:
            with self._open(method, path, data, headers, self._settings.request_timeout_s) as reply:
                self._keep_cookie(reply.headers.get_all("Set-Cookie") or [])
                return Response(reply.status, reply.read())
        except urllib.error.HTTPError as exc:  # отказ службы — значение, а не исключение
            return Response(exc.code, exc.read())

    def upload(self, name: str, content: bytes) -> Response:
        """Загрузка документа так же, как экран: отложенный разбор, `pymupdf` (api.ts)."""
        boundary = uuid.uuid4().hex
        fields = {
            "company_name": "",
            "period": "",
            "doc_type": "",
            "company_type": "unknown",
            "ocr_engine": "pymupdf",
            "defer_parse": "1",
        }
        parts = [
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            ).encode()
            for key, value in fields.items()
        ]
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        body = b"".join(parts) + head + content + f"\r\n--{boundary}--\r\n".encode()
        return self.request(
            "POST",
            "/documents/upload",
            data=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def stream_events(self, path: str) -> Iterator[dict[str, Any]]:
        """Полезные нагрузки SSE по порядку; обрыв связи — `OSError`, решает вызывающий."""
        try:
            with self._open("GET", path, None, {}, self._settings.stream_timeout_s) as reply:
                yield from iter_sse(reply)
        except urllib.error.HTTPError as exc:
            raise ClientError(f"поток событий: HTTP {exc.code}") from exc

    def _open(
        self, method: str, path: str, data: bytes | None, headers: dict[str, str], timeout: float
    ) -> Any:
        request = urllib.request.Request(  # noqa: S310 — схема и хост проверены в check_stand
            self._settings.base_url + path, data=data, method=method, headers=headers
        )
        if self._cookie:
            request.add_header("Cookie", self._cookie)
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310

    def _keep_cookie(self, set_cookie: list[str]) -> None:
        # Вручную, а не cookiejar: у службы `Secure`, и jar не отдал бы cookie на http-туннеле.
        if set_cookie:
            self._cookie = set_cookie[0].split(";", 1)[0]


def iter_sse(lines: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """Разбор потока `text/event-stream`: `data:`-строки до пустой склеиваются в один JSON."""
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line == "":
            if data:
                yield json.loads("".join(data))
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield json.loads("".join(data))
