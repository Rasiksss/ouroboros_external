"""Точка входа комнаты (`_room_entry.py`) — локальным интерпретатором."""
from __future__ import annotations

import json

_OK_CODE = "def run(inputs):\n    return {'total': sum(inputs['xs'])}\n"


def test_success_writes_result_json_to_stdout(room_entry) -> None:
    done = room_entry({"code": _OK_CODE, "inputs": {"xs": [1, 2, 3]}})

    assert done.returncode == 0
    assert json.loads(done.stdout) == {"total": 6}


def test_result_keeps_cyrillic_unescaped(room_entry) -> None:
    code = "def run(inputs):\n    return {'имя': 'выручка'}\n"

    done = room_entry({"code": code, "inputs": {}})

    assert done.stdout.decode("utf-8") == '{"имя": "выручка"}'


def test_exception_gives_traceback_and_exit_code_1(room_entry) -> None:
    code = "def run(inputs):\n    raise ValueError('плохой вход')\n"

    done = room_entry({"code": code, "inputs": {}})

    assert done.returncode == 1
    assert done.stdout == b""
    assert "ValueError: плохой вход" in done.stderr.decode("utf-8")


def test_nan_in_result_is_an_error(room_entry) -> None:
    code = "def run(inputs):\n    return {'x': float('nan')}\n"

    done = room_entry({"code": code, "inputs": {}})

    assert done.returncode == 1
    assert "ValueError" in done.stderr.decode("utf-8")


def test_non_dict_result_is_an_error(room_entry) -> None:
    done = room_entry({"code": "def run(inputs):\n    return [1, 2]\n", "inputs": {}})

    assert done.returncode == 1
    assert "run() должна вернуть dict, вернула list" in done.stderr.decode("utf-8")


def test_missing_run_function_is_an_error(room_entry) -> None:
    done = room_entry({"code": "x = 1\n", "inputs": {}})

    assert done.returncode == 1
    assert "KeyError" in done.stderr.decode("utf-8")


def test_print_in_atom_code_does_not_reach_stdout(room_entry) -> None:
    code = "def run(inputs):\n    print('шум отладки')\n    return {'ok': True}\n"

    done = room_entry({"code": code, "inputs": {}})

    assert done.returncode == 0
    assert json.loads(done.stdout) == {"ok": True}
    assert "шум отладки" in done.stderr.decode("utf-8")


def test_entry_text_has_no_words_denied_by_the_station(entry_text) -> None:
    """Сервис отвергает команду, в аргументах которой есть эти слова (DENY_COMMAND_TOKENS)."""
    text = entry_text.lower()

    for token in ("sudo", "mount", "iptables", "docker", "podman"):
        assert token not in text
