#!/usr/bin/env python3
"""Выгрузка сессий ассистента за период — вход для ночного самообучения.

Встроенный поиск по сессиям обычно не годится для полного разбора: видит не все сессии и режет
длинные. Поэтому скрипт читает историю (SQLite, только чтение) и пишет по файлу Markdown на
сессию плюс оглавление; агент читает их файловыми инструментами.

Ожидаемая схема (адаптируйте запросы в build() под своё хранилище):
  sessions(id, source, started_at, last_activity_at, title, hidden)
  messages(id, session_id, role, content, tool_calls, tool_name, timestamp, active)
  tool_calls — JSON-список в формате OpenAI: [{"function": {"name", "arguments"}}].

Каталог профиля — на уровень выше scripts/, где лежит скрипт, либо --home; ожидаются
<home>/state.db и <home>/skills. Период — --days или SELFLEARN_DAYS (по умолчанию 1).
Нет подходящих сессий → последняя строка {"wakeAgent": false}: планировщик может не будить агента.
Самопроверка: session_digest.py --self-test
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

SKIP_SOURCES = ("tool", "subagent", "cron")  # прогоны самообучения, вложенные задачи, планировщик
USER_MAX, ASSISTANT_MAX, ARGS_MAX, RESULT_MAX = 6000, 4000, 300, 300
FILE_MAX = 120_000  # байт на сессию; больше — урезаем вывод инструментов, затем середину


def clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f" …[+{len(text) - limit} симв.]"


def tool_calls_text(raw):
    try:
        calls = json.loads(raw) if raw else []
    except ValueError:
        return []
    out = []
    for call in calls if isinstance(calls, list) else []:
        fn = (call or {}).get("function") or {}
        out.append(f"→ `{fn.get('name', '?')}` {clip(fn.get('arguments', ''), ARGS_MAX)}")
    return out


def render_session(sess, messages, result_max=RESULT_MAX):
    sid, source, started, title = sess
    lines = [f"# {title or '(без названия)'}", "",
             f"- session: `{sid}`, source: {source}, начало: {fmt_time(started)}", ""]
    for role, content, tool_calls, tool_name, ts in messages:
        if role == "user":
            lines += [f"## 👤 Пользователь · {fmt_time(ts)}", clip(content, USER_MAX), ""]
        elif role == "assistant":
            body = clip(content, ASSISTANT_MAX)
            calls = tool_calls_text(tool_calls)
            if body or calls:
                lines += [f"## 🤖 Агент · {fmt_time(ts)}"] + ([body] if body else []) + calls + [""]
        elif role == "tool":
            lines += [f"← `{tool_name or 'tool'}`: {clip(content, result_max)}", ""]
    return "\n".join(lines)


def fmt_time(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "?"


def fit(sess, messages):
    text = render_session(sess, messages)
    if len(text.encode()) > FILE_MAX:
        text = render_session(sess, messages, result_max=120)
    if len(text.encode()) > FILE_MAX:  # ценнее реплики людей и агента — сначала убираем вывод инструментов
        text = render_session(sess, [m for m in messages if m[0] != "tool"])
    data = text.encode()
    if len(data) > FILE_MAX:
        head, tail = data[: FILE_MAX // 2], data[-FILE_MAX // 2 + 2000:]
        text = (head.decode(errors="ignore") + "\n\n…[середина сессии вырезана по размеру]…\n\n"
                + tail.decode(errors="ignore"))
    return text


def linked_skills(home: Path) -> list:
    """Каталоги скилов-симлинков (репозитории проектов): skill_manage их не видит, агент правит файлы по пути."""
    out = []
    for root, dirs, files in os.walk(home / "skills"):  # os.walk не заходит в симлинки; битые попадают в files
        for d in [d for d in dirs + files if not d.startswith(".")]:
            link = Path(root) / d
            if link.is_symlink() and (link.is_dir() or not link.exists()):
                out.append((str(link.relative_to(home / "skills")), os.path.realpath(link), link.exists()))
        dirs[:] = [d for d in dirs if not d.startswith(".")]
    return sorted(out)


def build(home: Path, days: float, out_dir: Path) -> int:
    db = home / "state.db"
    since = time.time() - days * 86400
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    rows = con.execute(
        "select id, source, started_at, title from sessions"
        " where coalesce(last_activity_at, started_at) >= ? and coalesce(hidden, 0) = 0"
        " order by started_at", (since,)).fetchall()
    picked = [r for r in rows if r[1] not in SKIP_SOURCES]
    skipped = len(rows) - len(picked)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, mode=0o700)
    rows_out = []
    for sess in picked:
        msgs = con.execute(
            "select role, content, tool_calls, tool_name, timestamp from messages"
            " where session_id = ? and coalesce(active, 1) = 1 and role != 'session_meta' order by id",
            (sess[0],)).fetchall()
        users = sum(1 for m in msgs if m[0] == "user")
        if not users:  # пустые и служебные без реплик людей
            skipped += 1
            continue
        n = len(rows_out) + 1
        text = fit(sess, msgs)
        name = f"{n:03d}_{sess[0]}.md"
        (out_dir / name).write_text(text, encoding="utf-8")
        title = (sess[3] or "").replace("|", "/")
        rows_out.append(f"| {n} | {name} | {fmt_time(sess[2])} | {title} | {users} | {len(msgs)} | {len(text.encode()) // 1024} |")
    index = [f"# Сессии профиля {home.name} за {days:g} дн. (с {fmt_time(since)})", "",
             f"Подходящих сессий: {len(rows_out)}; пропущено служебных ({', '.join(SKIP_SOURCES)}) и без реплик людей: {skipped}.",
             "Ссылка на сессию в отчёте: `" + home.name + "/<session>`.", "",
             "| # | файл | начало | название | сообщ. польз. | всего | КБ |", "|---|---|---|---|---|---|---|"] + rows_out
    linked = linked_skills(home)
    index += ["", "## Скилы, подключённые симлинком",
              "Лежат в репозиториях проектов; skill_manage их не видит — правь файлы по пути.", ""]
    index += [f"- skills/{rel} → `{target}`" + ("" if ok else " (каталог недоступен)") for rel, target, ok in linked] or ["- (нет)"]
    (out_dir / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    con.close()
    return len(rows_out)


def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "demo"
        home.mkdir()
        con = sqlite3.connect(home / "state.db")
        con.executescript(
            "create table sessions(id, source, started_at, last_activity_at, title, hidden);"
            "create table messages(id integer primary key, session_id, role, content, tool_calls, tool_name, timestamp, active);")
        now = time.time()
        con.executemany("insert into sessions values (?,?,?,?,?,?)", [
            ("s1", "chat", now - 3600, now - 60, "Живая", 0),
            ("s2", "subagent", now - 3600, now - 60, "Вложенная", 0),
            ("s3", "chat", now - 9 * 86400, None, "Старая", 0),
            ("s4", "chat", now - 600, now - 60, "Пустая", 0)])
        con.executemany("insert into messages(session_id, role, content, tool_calls, tool_name, timestamp, active) values (?,?,?,?,?,?,?)", [
            ("s1", "user", "сделай X", None, None, now - 3500, 1),
            ("s1", "assistant", "", json.dumps([{"function": {"name": "terminal", "arguments": "{\"command\":\"ls\"}"}}]), None, now - 3400, 1),
            ("s1", "tool", "x" * 1000, None, "terminal", now - 3300, 1),
            ("s1", "user", "откатили", None, None, now - 3200, 0)])
        con.commit()
        con.close()
        out = Path(tmp) / "out"
        assert build(home, 1, out) == 1
        text = (out / "001_s1.md").read_text(encoding="utf-8")
        assert "сделай X" in text and "`terminal`" in text and "+700 симв." in text and "откатили" not in text, text
        index = (out / "index.md").read_text(encoding="utf-8")
        assert "и без реплик людей: 2" in index and "Старая" not in index and "Пустая" not in index, index
        sk = home / "skills"
        (sk / "own").mkdir(parents=True)
        ext = Path(tmp) / "ext" / "x"
        ext.mkdir(parents=True)
        (sk / "linked").symlink_to(ext.parent)
        (sk / "gone").symlink_to(Path(tmp) / "missing")
        assert linked_skills(home) == [("gone", str(Path(tmp, "missing").resolve()), False),
                                       ("linked", str(ext.parent.resolve()), True)], linked_skills(home)
        build(home, 1, out)
        index = (out / "index.md").read_text(encoding="utf-8")
        assert "skills/linked →" in index and "(каталог недоступен)" in index, index
    print("self-test ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", type=Path, default=Path(sys.argv[0]).absolute().parent.parent)
    ap.add_argument("--days", type=float, default=float(os.environ.get("SELFLEARN_DAYS") or 1))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    out_dir = a.home / "cache" / "selflearn" / "latest"
    n = build(a.home, a.days, out_dir)
    print(f"Выгрузка сессий для самообучения: {n} сесс. за {a.days:g} дн.")
    print(f"Оглавление: {out_dir / 'index.md'}")
    if n == 0:
        print(json.dumps({"wakeAgent": False}))


if __name__ == "__main__":
    main()
