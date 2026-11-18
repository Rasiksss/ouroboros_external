[AEF WorkStation] Описание API для АУА

Ссылка на OpenAPI: https://workstation.dev0.apps.azwx5oj1.k8s.delta.sbrf.ru/docs#/

Оглавление
1. Описание сервиса
2. Управление сессиями
2.1 Создание sandbox-сессии (POST /v1/sessions)
2.2 Получение состояния sandbox-сессии (GET /v1/sessions/{session_id})
2.3 Завершение сессии (POST /v1/sessions/{session_id}:terminate)
2.4 Продление активности сессии
3. Управление файлами
3.1 Загрузка файлов (POST /v1/sessions/{session_id}/files:upload)
3.2 Скачивание файла (POST /v1/sessions/{session_id}/files:download)
3.3 Просмотр дерева файлов (POST /v1/sessions/{session_id}/files:list)
4. Выполнение кода и bash-команд внутри сессии
4.1 Выполнение команды (POST /v1/sessions/{session_id}/exec)
4.2 Потоковое выполнение команды (POST /v1/sessions/{session_id}/exec:stream)
5. Возврат ошибок
6. Логирование
1. Описание сервиса

AEF WorkStation предназначен для предоставления AI-агенту безопасной, изолированной рабочей среды, в которой можно выполнять команды, запускать код и работать с файлами.

Агент не управляет runtime-механизмами напрямую и не работает с файловой системой хоста. Взаимодействие клиента с AEF WorkStation выполняется только через REST API.

1.1 Основные функции сервиса
Шаг    Действие    Метод
1    Создать sandbox-сессию    POST /v1/sessions
2    Получить состояние sandbox-сессии    GET /v1/sessions/{session_id}
3    Загрузить файлы    POST /v1/sessions/{session_id}/files:upload
4    Скачать файлы    POST /v1/sessions/{session_id}/files:download
5    Просмотреть дерево файлов    POST /v1/sessions/{session_id}/files:list
6    Выполнить команду    POST /v1/sessions/{session_id}/exec или POST /v1/sessions/{session_id}/exec:stream
7    Завершить сессию    POST /v1/sessions/{session_id}:terminate
Виртуальный путь    Назначение
/    Корень рабочей среды конкретной sandbox-сессии; используется по умолчанию в exec и files:list
/workspace    Рабочая директория для файлов проекта; единственный каталог, который может сохраняться между сессиями через опцию S3

Все пути в file API и exec API интерпретируются относительно рабочей среды текущей сессии. Путь не должен выходить за пределы рабочей среды.

2. Управление сессиями

Sandbox-сессия — основная сущность работы сервиса. Это изолированная рабочая среда, связанная с конкретным X-Session-Id. В рамках одной sandbox-сессии сохраняются загруженные файлы, результаты предыдущих операций и служебное состояние. Разные sandbox-сессии не имеют доступа к файлам и состоянию друг друга.

Клиенту достаточно знать три вещи:

Сущность    Зачем нужна
session_id    Идентифицирует sandbox-сессию
token    Подтверждает право выполнять операции в рамках сессии
REST API    Даёт единый способ создавать среду, выполнять команды и работать с файлами

AEF WorkStation самостоятельно отвечает за создание рабочей среды, запуск команд, контроль ограничений, продление активности и очистку ресурсов.

2.1 Создание sandbox-сессии (POST /v1/sessions)

Sandbox-сессия скрывает от клиента детали создания рабочей среды. Клиент получает только публичные атрибуты сессии и token для дальнейших операций.

Запрос (SessionCreateRequest)

Параметры заголовков запроса

Параметр    Обязательность/кратность    Формат    Описание
X-Session-Id    1..1    string    Идентификатор sandbox-сессии, задаётся клиентом. Разрешены буквы, цифры, точка, underscore и дефис
X-Session-Create-Secret    0..1    string    Общий секрет вызывающей стороны. Обязателен, если на сервисе включён IS_CREATE_SESSION_SECRET; иначе не проверяется
Content-Type    1..1    application/json    Формат тела запроса

Параметры тела запроса

Параметр    Обязательность/кратность    Тип    Описание
idleTimeoutSec    0..1    integer/null    Время жизни без активности; не может превышать системный лимит
absoluteTtlSec    0..1    integer/null    Максимальное время жизни независимо от активности; не может превышать системный лимит
workspacePersistence    0..1    object/null    Используется для S3. Запрос восстановления долговременного /workspace из объектного хранилища. Если не передан, обмена с S3 не происходит и /workspace стартует пустым
workspacePersistence.scopeId    1..1    string    Используется для S3. Адрес ячейки хранения, 1–128 символов. Не должен содержать сырой пользовательский sessionKey
workspacePersistence.root    0..1    string    Используется для S3. Единственное допустимое значение — /workspace; оно же по умолчанию

Пример JSON-тела:

{
  "idleTimeoutSec": 1800,
  "absoluteTtlSec": 7200,
  "workspacePersistence": {
    "scopeId": "3c8f9f4d8b6a",
    "root": "/workspace"
  }
}

Логика выполнения:

Сессия отсутствует: Создаётся новая запись состояния, генерируется token, подготавливается рабочая среда, сессия переводится в ready.
Сессия существует в ready: Сервис переиспользует существующую среду, новый token не возвращает.
Сессия в provisioning: Сервис возвращает ошибку текущего состояния или terminating.
Сессия в terminated или failed: Сервис пересоздаёт среду для того же X-Session-Id и выдаёт новый token.

Создание по одному X-Session-Id сериализуется: параллельные вызовы обрабатываются по очереди.

Ответ (SessionCreateResponse)

Параметры тела ответа

Параметр    Обязательность/кратность    Тип    Описание
id    1..1    string    Идентификатор sandbox-сессии
status    1..1    string    Текущий статус сессии
token    0..1    string/null    Bearer-token; возвращается только при создании или пересоздании сессии
createdAt    1..1    date-time    Дата создания текущей версии сессии
expiresAt    1..1    date-time    Расчётное время истечения сессии
quotas    1..1    object    Квоты на файлы и выполнение команд
workspacePersistence    0..1    object/null    Используется для S3. Состояние долговременного /workspace

Пример успешного ответа:


{
  "id": "example-session",
  "status": "ready",
  "token": "example-token",
  "createdAt": "2026-05-13T09:39:17Z",
  "expiresAt": "2026-05-13T11:39:17Z",
  "quotas": {
    "maxFileBytes": 20971520,
    "maxTotalBytes": 209715200,
    "execTimeoutSec": 300
  },
  "workspacePersistence": {
    "enabled": true,
    "root": "/workspace",
    "scopeId": "3c8f9f4d8b6a",
    "restoredRevisionId": "9f2c4e81a7b3",
    "snapshotRevisionId": null
  }
}

Значение token необходимо передавать во всех последующих вызовах для авторизации в рамках сессии.

Коды ответа:

200 OK: Существующая ready-сессия переиспользована. Token повторно не возвращается.
201 Created: Создана новая sandbox-сессия или пересоздана сессия в terminated или failed. Token возвращается.
400 Bad Request: Некорректный запрос.
401 Unauthorized: Не передан или не совпал X-Session-Create-Secret.
409 Conflict: Операция невозможна в текущем состоянии сессии.
422 Unprocessable Entity: Тело запроса не соответствует схеме.
500 Internal Server Error: Внутренняя ошибка сервиса или runtime.
2.2 Получение состояния sandbox-сессии (GET /v1/sessions/{session_id})

Для получения текущего состояния клиент вызывает GET /v1/sessions/{session_id}. Метод доступен для всех статусов сессии, но требует корректную авторизацию.

Запрос

Header:

Параметр    Обязательность/кратность    Формат    Описание
Authorization    1..1    Bearer    Token сессии
X-Session-Id    1..1    string    Должен совпадать с session_id в path

Body: Отсутствует.

Ответ

Header: Content-Type: application/json

Body:

Параметр    Обязательность/кратность    Тип    Описание
id    1..1    string    Идентификатор сессии
status    1..1    string    Текущий статус сессии
createdAt    1..1    date-time    Время создания
updatedAt    0..1    date-time/null    Время последнего обновления состояния
lastUsedAt    1..1    date-time    Время последней активности
expiresAt    1..1    date-time    Расчётное время истечения
fileBytesUsed    1..1    integer    Текущий объём файлов сессии
failReason    0..1    string/null    Причина ошибки, если сессия в failed
quotas    1..1    object    Квоты сессии
workspacePersistence    0..1    object/null    Состояние долговременного /workspace

Если сессия находится в ready, успешный вызов метода продлевает активность сессии.

Коды ответа: 200, 401, 404, 500.

2.3 Завершение сессии (POST /v1/sessions/{session_id}:terminate)

Для завершения sandbox-сессии клиент вызывает POST /v1/sessions/{session_id}:terminate.

Запрос

Параметры пути: session_id (1..1).
Заголовки: Authorization (Bearer), X-Session-Id (должен совпадать).
Тело: Отсутствует.

Логика выполнения:

Активная сессия переводится в terminating, удаляется рабочая среда и переходит в terminated.
Повторный terminate не считается ошибкой.
Если был передан workspacePersistence, содержимое /workspace выгружается в S3 до удаления.
Ответ

Body:

Параметр    Обязательность/кратность    Тип    Описание
id    1..1    string    Идентификатор сессии
status    1..1    string    Итоговый статус
terminatedAt    1..1    date-time    Время завершения

Коды ответа: 200, 401, 404, 409, 500.

2.4 Продление активности сессии

При успешных рабочих операциях сервис обновляет активность сессии. Активностью считаются: получение сведений о ready-сессии, exec, exec:stream, files:upload, files:download, files:list.

Срок истечения рассчитывается с учётом idleTimeoutSec и absoluteTtlSec. Сессия не может жить дольше absolute TTL. Если активности нет дольше idle TTL, сессия считается просроченной.

Запись отметки активности дебаунсится на SESSION_TOUCH_DEBOUNCE_SEC (по умолчанию 5 секунд).

3. Управление файлами
3.1 Загрузка файлов (POST /v1/sessions/{session_id}/files:upload)

Метод доступен только для сессии в статусе ready. Формат запроса — multipart/form-data.

Запрос

Параметры пути: session_id (1..1).
Заголовки: Authorization (Bearer), X-Session-Id, Content-Type: multipart/form-data.
Поля формы:

path (1..1): виртуальный путь назначения.
file (1..1): бинарное содержимое.

Проверки перед записью: статус ready, безопасность пути, цель не директория, размер файла, квота сессии, блокировка записи.

Ответ

Body:

Параметр    Обязательность/кратность    Тип    Описание
path    1..1    string    Нормализованный виртуальный путь файла
bytes    1..1    integer    Размер загруженного файла в байтах
sha256    1..1    string    SHA-256 загруженного файла

Коды ответа: 200, 400, 401, 403, 404, 409, 413, 422, 500.

3.2 Скачивание файла (POST /v1/sessions/{session_id}/files:download)

Метод доступен только для сессии в статусе ready.

Запрос

Заголовки: Authorization (Bearer), X-Session-Id, Content-Type: application/json.
Body: { "path": "/workspace/data.txt" }

Проверки: статус ready, безопасный путь, существование файла, объект является файлом.

Ответ

Сервис возвращает бинарное тело файла с Content-Type: application/octet-stream.
Заголовки ответа: X-File-Sha256, X-File-Bytes.

Коды ответа: 200, 400, 401, 403, 404, 409, 422, 500.

3.3 Просмотр дерева файлов (POST /v1/sessions/{session_id}/files:list)

Метод доступен только для сессии в статусе ready.

Запрос

Body:

Параметр    Обязательность/кратность    Тип    По умолчанию    Описание
path    0..1    string    /    Путь, с которого нужно построить дерево
recursive    0..1    boolean    true    Признак рекурсивного обхода
maxDepth    0..1    integer    5    Максимальная глубина обхода
includeSha256    0..1    boolean    true    Нужно ли рассчитывать SHA-256

Для больших каталогов рекомендуется передавать includeSha256=false.

Ответ

Узел файлового дерева:

Параметр    Обязательность/кратность    Тип    Описание
name    1..1    string    Имя файла или каталога
path    1..1    string    Виртуальный путь
kind    1..1    string    Тип узла: file или dir
bytes    0..1    integer/null    Размер файла
sha256    0..1    string/null    SHA-256 файла
children    0..n    array[object]    Дочерние элементы

Пример ответа:

json
Копировать
{
  "name": "workspace",
  "path": "/workspace",
  "kind": "dir",
  "bytes": null,
  "sha256": null,
  "children": [
    {
      "name": "data.txt",
      "path": "/workspace/data.txt",
      "kind": "file",
      "bytes": 128,
      "sha256": null,
      "children": []
    }
  ]
}

Коды ответа: 200, 400, 401, 403, 404, 409, 422, 500.

4. Выполнение кода и bash-команд внутри сессии
4.1 Выполнение команды (POST /v1/sessions/{session_id}/exec)

Для синхронного выполнения команды. Метод доступен только для сессии в статусе ready.

Запрос

Body:

Параметр    Обязательность/кратность    Тип    По умолчанию    Описание
command    1..n    array[string]    -    Список аргументов команды в формате argv
cwd    0..1    string    /    Рабочая директория внутри сессии
env    0..1    object    {}    Дополнительные переменные окружения из allowlist
stdin    0..1    string/null    null    Входные данные процесса
stdinEncoding    0..1    enum    utf8    utf8 или base64
timeoutSec    0..1    integer/null    quotas.execTimeoutSec    Timeout выполнения

Пример:

json
Копировать
{
  "command": ["sh", "-lc", "pwd && ls -la"],
  "cwd": "/workspace",
  "env": { "LANG": "C.UTF-8" },
  "stdin": null,
  "stdinEncoding": "utf8",
  "timeoutSec": 30
}

Проверки: сессия готова, токен валиден, команда разрешена политикой, cwd безопасен, env разрешён, таймаут допустим, квота файлов не превышена.

Ответ
Параметр    Обязательность/кратность    Тип    Описание
exitCode    1..1    integer    Код завершения
stdout    1..1    string    Стандартный вывод
stderr    1..1    string    Стандартный поток ошибок
stdoutTruncated    1..1    boolean    Признак усечения stdout
stderrTruncated    1..1    boolean    Признак усечения stderr
durationMs    1..1    integer    Длительность выполнения
timedOut    1..1    boolean    Признак timeout
workspaceRoot    1..1    string    Абсолютный путь корня сессии

Коды ответа: 200, 400, 401, 403, 404, 409, 413, 422, 500, 504.

4.2 Потоковое выполнение команды (POST /v1/sessions/{session_id}/exec:stream)

Используется, когда клиенту нужно получать вывод команды по мере выполнения.

Запрос

Параметры идентичны sync exec (см. 4.1).

Ответ

Формат ответа — application/x-ndjson. Каждая строка ответа является отдельным JSON-событием.

Структура события:

Поле    Для каких событий    Описание
kind    все    Тип события: stdout, stderr или exit
data    stdout, stderr    Фрагмент вывода
exitCode    exit    Код завершения
durationMs    exit    Длительность выполнения
timedOut    exit    Признак timeout
disconnected    exit    Признак разрыва соединения клиентом
workspaceRoot    exit    Абсолютный путь корня сессии

Пример потока:

Копировать
{"kind":"stdout","data":"/workspace\n"}
{"kind":"stderr","data":""}
{"kind":"exit","exitCode":0,"durationMs":120,"timedOut":false,"workspaceRoot":"/tmp/workstation/sessions/example-session"}

Поля с пустым значением в событие не включаются.

Коды ответа: 200, 400, 401, 403, 404, 409, 422, 500.

5. Возврат ошибок

Сервис возвращает доменные ошибки в едином JSON-формате:


{
  "code": "invalid-state",
  "message": "Операция не разрешена в текущем состоянии сессии",
  "details": {
    "sessionId": "example-session",
    "status": "terminating"
  }
}

Основные коды HTTP и причины:

400 bad-request: Некорректные входные параметры.
401 auth-required: Проблемы с токеном или X-Session-Id/X-Session-Create-Secret.
403 policy-deny: Команда, env или путь запрещены политикой.
404 not-found: Сессия, файл или путь не найдены.
409 invalid-state: Операция невозможна в текущем состоянии.
413 quota-exceeded: Превышена файловая квота.
422 validation-error: Ошибка схемы запроса FastAPI.
500 internal: Внутренняя ошибка сервиса или runtime.
503 service-unavailable: Readiness-проверка не пройдена.
504 timeout: Превышено время выполнения sync exec.
6. Логирование

Сервис пишет структурированные логи в двух представлениях: человекочитаемая строка в stdout и JSON в формате ECS в файл APP_LOG_DIR/app.log.

Уровни логирования:

DEBUG: Приём запроса и начало каждой операции.
INFO: Успешное завершение операции, штатные пропуски.
ERROR: Любая неуспешная операция (с типом, сообщением и stack trace).
WARNING: Только на уровне окружения (например, недоступный каталог логов).

Секреты вычищаются перед записью (authorization, token, secret, password, api_key заменяются на [redacted]). Строки длиннее 4096 символов и массивы длиннее 50 элементов обрезаются.

Общие поля всех записей: @timestamp, log.level, message, service.name, app.request_id, trace.id, span.id, app.agent_id, kubernetes.pod.name. Для ERROR дополнительно присутствуют error.type, error.message, error.stack_trace.

Каждый HTTP-запрос даёт пару записей middleware: http.request start (DEBUG) и http.request end (INFO).

Сводная таблица по методам (примеры JSON)
Метод    Уровень    Пример сообщения
POST /v1/sessions    DEBUG    request_accepted (create_session)
POST /v1/sessions    INFO    SessionService.create_session success
POST /v1/sessions    ERROR    SessionService.create_session failed (InternalServerErrorException)
GET /v1/sessions/{id}    DEBUG    request_accepted (get_session)
GET /v1/sessions/{id}    INFO    SessionService.get_session success
GET /v1/sessions/{id}    ERROR    request_failed (NotFoundException)
POST /v1/sessions/{id}:terminate    DEBUG    _terminate_existing_session start
POST /v1/sessions/{id}:terminate    INFO    _terminate_existing_session success
POST /v1/sessions/{id}/files:upload    DEBUG    upload_file start
POST /v1/sessions/{id}/files:upload    INFO    upload_file success
POST /v1/sessions/{id}/files:upload    ERROR    request_failed (QuotaExceededException)
POST /v1/sessions/{id}/files:download    DEBUG    download_file start
POST /v1/sessions/{id}/files:download    INFO    download_file success
POST /v1/sessions/{id}/files:download    ERROR    request_failed (NotFoundException)
POST /v1/sessions/{id}/files:list    DEBUG    list_files start
POST /v1/sessions/{id}/files:list    INFO    list_files success
POST /v1/sessions/{id}/files:list    ERROR    request_failed (PolicyDeniedException)
POST /v1/sessions/{id}/exec    DEBUG    exec start
POST /v1/sessions/{id}/exec    INFO    exec success
POST /v1/sessions/{id}/exec    ERROR    exec denied (PolicyDeniedException)
POST /v1/sessions/{id}/exec:stream    DEBUG    exec_stream start
POST /v1/sessions/{id}/exec:stream    INFO    exec_stream success
POST /v1/sessions/{id}/exec:stream    ERROR    exec_stream denied (PolicyDeniedException)

Результат также сохранен в файл /mnt/data/aef_workstation_api.md.
