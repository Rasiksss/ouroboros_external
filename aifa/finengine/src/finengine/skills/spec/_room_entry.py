"""Точка входа кода временного атома внутри комнаты станции. Только stdlib."""
import contextlib
import json
import sys


def main() -> None:
    request = json.load(sys.stdin)
    namespace: dict = {}
    with contextlib.redirect_stdout(sys.stderr):   # print кода атома не портит результат
        exec(compile(request["code"], "<atom>", "exec"), namespace)
        result = namespace["run"](request["inputs"])
    if not isinstance(result, dict):
        raise TypeError(f"run() должна вернуть dict, вернула {type(result).__name__}")
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))  # NaN — ошибка


main()
