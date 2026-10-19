"""Хранилище игрового агента: один файл SQLite на три процесса.

С файлом работают сразу сервер MCP (только чтение), задания по расписанию и веб (оба пишут).
Поэтому режим `WAL` (читатель не ждёт писателя), ожидание замка 5000 мс и короткие транзакции:
каждая функция записи открывает свою `BEGIN IMMEDIATE` и закрывает её сразу.

`BEGIN IMMEDIATE`, а не отложенная транзакция: при отложенной два процесса могут оба прочитать,
затем оба попросить запись — и один получит `database is locked` без ожидания. Немедленная
берёт замок записи первой же командой и ждёт его в пределах `busy_timeout`.

Файл живёт месяцами и переживает обновления кода. Пересоздавать его нельзя никогда; файл с
версией схемы новее известной код не трогает и падает с понятным текстом. Файл версии 1 первое
открытие на запись переводит на версию 2 одной транзакцией: добавляет таблицы, прежние не трогает.
Версия 4 добавляет игры Nintendo eShop: колонки страницы, кода каталога и срока скидки, пары
«та же игра в другом магазине» и историю цен; переход с 3 на 4 тоже одной транзакцией, данные целы.

Сети модуль не знает, карточку myharness не читает.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from eshop_store import это_eshop
from steam_store import МАГАЗИН, Игра, Отзывы

ВЕРСИЯ_СХЕМЫ = 4
ИМЯ_ФАЙЛА = "games.sqlite"
# Правило проекта: всё, что пишет в домашний каталог, переопределяется окружением.
ПЕРЕМЕННАЯ_КАТАЛОГА = "GAMES_DATA_DIR"
КАТАЛОГ_ПО_УМОЛЧАНИЮ = Path("~/.local/share/games-agent")
ОЖИДАНИЕ_ЗАМКА_МС = 5000
# «выход» — игра календаря вышла сегодня; «сверка» — пора сверить вкус (раз в полгода).
РОДЫ_ЛЕНТЫ = ("скидка", "сбой", "итог", "выход", "сверка")

СХЕМА = """
CREATE TABLE IF NOT EXISTS meta (
    ключ TEXT PRIMARY KEY,
    значение TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
    номер INTEGER PRIMARY KEY,
    имя TEXT NOT NULL,
    описание TEXT NOT NULL DEFAULT '',
    жанры TEXT NOT NULL DEFAULT '[]',
    дата_текст TEXT NOT NULL DEFAULT '',
    дата TEXT,
    вышла INTEGER NOT NULL DEFAULT 1,
    цена INTEGER,
    цена_без_скидки INTEGER,
    скидка INTEGER NOT NULL DEFAULT 0,
    валюта TEXT NOT NULL DEFAULT '',
    бесплатна INTEGER NOT NULL DEFAULT 0,
    отзывы_сводка TEXT NOT NULL DEFAULT '',
    отзывы_положительных INTEGER NOT NULL DEFAULT 0,
    отзывы_всего INTEGER NOT NULL DEFAULT 0,
    обновлено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suggestions (
    номер INTEGER PRIMARY KEY,
    причина TEXT NOT NULL,
    предложено TEXT NOT NULL,
    оценка INTEGER,
    причина_оценки TEXT,
    оценено TEXT
);
CREATE TABLE IF NOT EXISTS calendar (
    номер INTEGER PRIMARY KEY,
    причина TEXT NOT NULL,
    добавлено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
    номер INTEGER PRIMARY KEY,
    цена INTEGER NOT NULL,
    скидка INTEGER NOT NULL,
    отмечено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed (
    номер INTEGER PRIMARY KEY AUTOINCREMENT,
    время TEXT NOT NULL,
    род TEXT NOT NULL,
    текст TEXT NOT NULL
);
"""

# Таблицы, которые добавила версия 2. Переход 1 → 2 — только добавление: таблицы версии 1 не
# меняются, и их данные целы. Новый файл получает обе части схемы сразу.
СХЕМА_2 = """
CREATE TABLE IF NOT EXISTS refusals (
    номер INTEGER NOT NULL,
    отпечаток TEXT NOT NULL,
    отказано TEXT NOT NULL,
    PRIMARY KEY (номер, отпечаток)
);
CREATE TABLE IF NOT EXISTS library (
    номер INTEGER PRIMARY KEY,
    имя TEXT NOT NULL,
    часы REAL NOT NULL DEFAULT 0,
    часы_2нед REAL NOT NULL DEFAULT 0,
    последний_запуск INTEGER
);
CREATE TABLE IF NOT EXISTS steam_wishlist (
    номер INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS played (
    номер INTEGER PRIMARY KEY,
    понравилась INTEGER NOT NULL,
    отмечено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS marks (
    ключ TEXT PRIMARY KEY,
    значение TEXT NOT NULL
);
"""

# Версия 3: адрес обложки из магазина. У новых игр Steam кладёт её по пути с хешем, и по номеру
# игры его не угадать (живой сценарий: у Sengoku Rance обложки не было).
СХЕМА_3 = """
ALTER TABLE games ADD COLUMN обложка TEXT
"""

# Версия 4: игры Nintendo eShop. Номер игры eShop (nsuid) лежит в той же колонке `номер`:
# nsuid — 14 цифр от `7001…`, номер Steam — до 8 цифр (`eshop_store.НАЧАЛО_НОМЕРОВ`).
# `адрес` — страница товара eShop, `объект` — код записи каталога, `скидка_до` — конец скидки.
# `pairs` — та же игра в другом магазине, правило записи — у `записать_пару`; `пара` `NULL` —
# искали, не нашли. `price_history` — цены по дням смены: исторического минимума eShop нигде
# нет, копим свой.
СХЕМА_4 = """
ALTER TABLE games ADD COLUMN адрес TEXT;
ALTER TABLE games ADD COLUMN объект TEXT;
ALTER TABLE games ADD COLUMN скидка_до TEXT;
CREATE TABLE IF NOT EXISTS pairs (
    номер INTEGER PRIMARY KEY,
    пара INTEGER,
    сверено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_history (
    номер INTEGER NOT NULL,
    цена INTEGER NOT NULL,
    валюта TEXT NOT NULL,
    отмечено TEXT NOT NULL,
    PRIMARY KEY (номер, отмечено)
);
"""


class НоваяСхема(RuntimeError):
    """Файл записан кодом новее этого: читать и писать его старым кодом нельзя."""


def путь() -> Path:
    """`$GAMES_DATA_DIR/games.sqlite`; без переменной — `~/.local/share/games-agent/`."""
    каталог = os.environ.get(ПЕРЕМЕННАЯ_КАТАЛОГА)
    return (Path(каталог) if каталог else КАТАЛОГ_ПО_УМОЛЧАНИЮ).expanduser() / ИМЯ_ФАЙЛА


def _сейчас() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def открыть(только_чтение: bool = False) -> sqlite3.Connection:
    """Соединение с файлом. На запись — каталог и схема создаются, если их нет.

    `isolation_level=None`: модуль сам открывает транзакции (`BEGIN IMMEDIATE`), неявная
    отложенная транзакция модуля `sqlite3` дала бы мгновенный отказ замка, описанный выше.
    """
    файл = путь()
    if только_чтение:
        if not файл.exists():
            raise FileNotFoundError(f"хранилища игр нет: {файл}")
        с = sqlite3.connect(
            f"{файл.as_uri()}?mode=ro", uri=True, isolation_level=None,
            timeout=ОЖИДАНИЕ_ЗАМКА_МС / 1000,
        )
    else:
        файл.parent.mkdir(parents=True, exist_ok=True)
        с = sqlite3.connect(файл, isolation_level=None, timeout=ОЖИДАНИЕ_ЗАМКА_МС / 1000)
    try:
        с.row_factory = sqlite3.Row
        с.execute(f"PRAGMA busy_timeout = {ОЖИДАНИЕ_ЗАМКА_МС}")
        if not только_чтение:
            # Режим журнала записан в самом файле: достаточно поставить его пишущему.
            _включить_wal(с)
        _сверить_схему(с, файл, только_чтение)
    except BaseException:
        с.close()
        raise
    return с


def _включить_wal(с: sqlite3.Connection) -> None:
    """`PRAGMA journal_mode` не ждёт замка по `busy_timeout`: два процесса, разом открывшие
    новый файл, получали отказ сразу (замер просмотра — 2–3 из 4). Ждём сами, тот же срок."""
    срок = time.monotonic() + ОЖИДАНИЕ_ЗАМКА_МС / 1000
    while True:
        try:
            с.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= срок:
                raise
            time.sleep(0.05)


def _версия(с: sqlite3.Connection) -> int | None:
    есть = с.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if not есть:
        return None
    строка = с.execute("SELECT значение FROM meta WHERE ключ = 'версия_схемы'").fetchone()
    return int(строка[0]) if строка else None


def _сверить_схему(с: sqlite3.Connection, файл: Path, только_чтение: bool) -> None:
    версия = _версия(с)
    _отвергнуть_версию(версия, файл)
    if версия == ВЕРСИЯ_СХЕМЫ:
        return
    if только_чтение:
        # Читатель — сервер MCP — переходить не вправе: переход пишет в файл.
        if версия is None:
            raise НоваяСхема(f"{файл}: схема не создана — файл ещё ни разу не открывали на запись")
        raise НоваяСхема(
            f"{файл}: версия схемы {версия}, код ждёт {ВЕРСИЯ_СХЕМЫ}. Переход сделает первое "
            "открытие на запись — задание или веб."
        )
    with _запись(с):
        # Внутри замка версия читается заново: другой процесс мог создать схему или перейти
        # на новую версию раньше.
        версия = _версия(с)
        _отвергнуть_версию(версия, файл)
        if версия == ВЕРСИЯ_СХЕМЫ:
            return
        # `executescript` сам завершает открытую транзакцию, поэтому таблицы создаются
        # по одной командой `execute` — внутри той же транзакции, что и запись версии.
        # Обрыв посреди перехода откатывает всё: файл остаётся прежней версии целиком.
        части = {
            None: (СХЕМА, СХЕМА_2, СХЕМА_3, СХЕМА_4),
            1: (СХЕМА_2, СХЕМА_3, СХЕМА_4),
            2: (СХЕМА_3, СХЕМА_4),
            3: (СХЕМА_4,),
        }[версия]
        for часть in части:
            for команда in часть.split(";"):
                if команда.strip():
                    с.execute(команда)
        с.execute(
            "INSERT INTO meta (ключ, значение) VALUES ('версия_схемы', ?) "
            "ON CONFLICT (ключ) DO UPDATE SET значение = excluded.значение",
            (str(ВЕРСИЯ_СХЕМЫ),),
        )


def _отвергнуть_версию(версия: int | None, файл: Path) -> None:
    """Код знает пустой файл, версии 1–3 (переходы есть) и свою. Остальное — отказ, файл
    не тронут."""
    if версия is not None and версия > ВЕРСИЯ_СХЕМЫ:
        raise НоваяСхема(
            f"{файл}: версия схемы {версия}, этот код знает только {ВЕРСИЯ_СХЕМЫ}. "
            "Обнови код; файл не тронут."
        )
    if версия is not None and версия not in (1, 2, 3, ВЕРСИЯ_СХЕМЫ):
        # Отметить незнакомый файл новой версией без перестройки таблиц значило бы сломать
        # запросы молча.
        raise НоваяСхема(
            f"{файл}: версия схемы {версия}, код ждёт {ВЕРСИЯ_СХЕМЫ}, а перехода нет. Файл не тронут."
        )


@contextmanager
def _запись(с: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Одна короткая транзакция записи: фиксация на выходе, откат при исключении."""
    with с:
        с.execute("BEGIN IMMEDIATE")
        yield с


# ---- запись ----


def _запомнить_игру(с: sqlite3.Connection, игра: Игра) -> None:
    """`запомнить_игру` внутри открытой транзакции."""
    с.execute(
        """
        INSERT INTO games (номер, имя, описание, жанры, дата_текст, дата, вышла, цена,
            цена_без_скидки, скидка, валюта, бесплатна, отзывы_сводка,
            отзывы_положительных, отзывы_всего, обновлено, обложка, адрес, объект,
            скидка_до)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (номер) DO UPDATE SET
            имя = excluded.имя, описание = excluded.описание, жанры = excluded.жанры,
            дата_текст = excluded.дата_текст, дата = excluded.дата, вышла = excluded.вышла,
            цена = excluded.цена, цена_без_скидки = excluded.цена_без_скидки,
            скидка = excluded.скидка, валюта = excluded.валюта,
            бесплатна = excluded.бесплатна, отзывы_сводка = excluded.отзывы_сводка,
            отзывы_положительных = excluded.отзывы_положительных,
            отзывы_всего = excluded.отзывы_всего, обновлено = excluded.обновлено,
            -- Игра без адреса обложки (собрана не из appdetails) не стирает прежний адрес.
            обложка = COALESCE(excluded.обложка, games.обложка),
            адрес = COALESCE(excluded.адрес, games.адрес),
            объект = COALESCE(excluded.объект, games.объект),
            скидка_до = excluded.скидка_до
        """,
        (
            игра.номер, игра.имя, игра.описание,
            json.dumps(list(игра.жанры), ensure_ascii=False),
            игра.дата_текст, игра.дата.isoformat() if игра.дата else None,
            int(игра.вышла), игра.цена, игра.цена_без_скидки, игра.скидка, игра.валюта,
            int(игра.бесплатна), игра.отзывы.сводка, игра.отзывы.положительных,
            игра.отзывы.всего, _сейчас(), игра.обложка or None, игра.адрес or None,
            игра.объект or None, игра.скидка_до or None,
        ),
    )


def запомнить_игру(с: sqlite3.Connection, игра: Игра) -> None:
    """Строка игры по номеру. Повтор обновляет все поля: магазин — источник правды. Исключения —
    обложка, адрес страницы eShop и код каталога: пустые не стирают прежние (игра, собранная не из
    полной записи, их не знает). Срок скидки пишется как пришёл: нет скидки — нет срока."""
    with _запись(с):
        _запомнить_игру(с, игра)


def _обновить_цену(
    с: sqlite3.Connection, номер: int, *, цена: int | None, цена_без_скидки: int | None,
    скидка: int, валюта: str, скидка_до: str,
) -> None:
    """`обновить_цену` внутри открытой транзакции."""
    с.execute(
        """
        UPDATE games SET цена = ?, цена_без_скидки = ?, скидка = ?, валюта = ?,
            скидка_до = ?, обновлено = ?
        WHERE номер = ?
        """,
        (цена, цена_без_скидки, скидка, валюта, скидка_до or None, _сейчас(), номер),
    )


def обновить_цену(
    с: sqlite3.Connection, номер: int, *, цена: int | None, цена_без_скидки: int | None,
    скидка: int, валюта: str, скидка_до: str,
) -> None:
    """Свежая цена в карточке игры — без полной записи каталога (ежедневные цены eShop).
    Игры в `games` нет — ничего не делает: карточку заводит `запомнить_игру`."""
    with _запись(с):
        _обновить_цену(с, номер, цена=цена, цена_без_скидки=цена_без_скидки, скидка=скидка,
                       валюта=валюта, скидка_до=скидка_до)


def предложить(с: sqlite3.Connection, номер: int, причина: str) -> bool:
    """Заводит предложение. `False` — игра уже предлагалась, с любой оценкой или без неё."""
    with _запись(с):
        курсор = с.execute(
            "INSERT OR IGNORE INTO suggestions (номер, причина, предложено) VALUES (?, ?, ?)",
            (номер, причина, _сейчас()),
        )
        return курсор.rowcount == 1


def оценить(с: sqlite3.Connection, номер: int, плюс: bool, причина: str) -> None:
    """Оценка человека. Игре календаря строки предложения может не быть — она заводится.
    Плюс кладёт игру в желаемое: желаемое — это предложения с плюсом."""
    сейчас = _сейчас()
    with _запись(с):
        с.execute(
            """
            INSERT INTO suggestions (номер, причина, предложено, оценка, причина_оценки, оценено)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (номер) DO UPDATE SET
                оценка = excluded.оценка, причина_оценки = excluded.причина_оценки,
                оценено = excluded.оценено
            """,
            (номер, причина, сейчас, int(плюс), причина, сейчас),
        )


def снять_оценку(с: sqlite3.Connection, номер: int) -> None:
    """Удаляет строку предложения целиком: игру можно предложить снова."""
    with _запись(с):
        с.execute("DELETE FROM suggestions WHERE номер = ?", (номер,))


def в_календарь(с: sqlite3.Connection, номер: int, причина: str) -> bool:
    """`False` — игра уже стоит в календаре."""
    with _запись(с):
        курсор = с.execute(
            "INSERT OR IGNORE INTO calendar (номер, причина, добавлено) VALUES (?, ?, ?)",
            (номер, причина, _сейчас()),
        )
        return курсор.rowcount == 1


def отметить_цену(
    с: sqlite3.Connection, номер: int, цена: int, скидка: int, текст_ленты: str | None = None,
    валюта: str = "",
) -> bool:
    """Записывает последнюю известную цену. `True` — новая скидка: ровно один раз на пару
    «игра, цена со скидкой». Новая — если скидка есть и прежней записи нет, либо прежняя цена
    другая, либо прежде скидки не было.

    `текст_ленты` новой скидки ложится в ленту В ТОЙ ЖЕ транзакции: иначе обрыв между двумя
    записями оставил бы скидку отмеченной, но не сообщённой — и назавтра она пропала бы молча.

    Смена цены ложится строкой в `price_history` в той же транзакции: из неё
    `минимум_наблюдений`. Та же цена день за днём строк не множит. Первая строка истории
    пишется и при прежней цене: цена, известная до версии 4, тоже входит в минимум."""
    сейчас = _сейчас()
    with _запись(с):
        прежняя = с.execute(
            "SELECT цена, скидка FROM prices WHERE номер = ?", (номер,)
        ).fetchone()
        новая = скидка > 0 and (
            прежняя is None or прежняя["цена"] != цена or прежняя["скидка"] == 0
        )
        с.execute(
            """
            INSERT INTO prices (номер, цена, скидка, отмечено) VALUES (?, ?, ?, ?)
            ON CONFLICT (номер) DO UPDATE SET
                цена = excluded.цена, скидка = excluded.скидка, отмечено = excluded.отмечено
            """,
            (номер, цена, скидка, сейчас),
        )
        история_есть = с.execute(
            "SELECT 1 FROM price_history WHERE номер = ? LIMIT 1", (номер,)
        ).fetchone()
        if прежняя is None or прежняя["цена"] != цена or not история_есть:
            # Две смены цены в одну секунду — в истории остаётся последняя.
            с.execute(
                """
                INSERT INTO price_history (номер, цена, валюта, отмечено) VALUES (?, ?, ?, ?)
                ON CONFLICT (номер, отмечено) DO UPDATE SET
                    цена = excluded.цена, валюта = excluded.валюта
                """,
                (номер, цена, валюта, сейчас),
            )
        if новая and текст_ленты:
            с.execute(
                "INSERT INTO feed (время, род, текст) VALUES (?, ?, ?)",
                (сейчас, "скидка", текст_ленты),
            )
        return новая


def _в_ленту(с: sqlite3.Connection, род: str, текст: str) -> None:
    """`в_ленту` внутри открытой транзакции."""
    if род not in РОДЫ_ЛЕНТЫ:
        raise ValueError(f"род ленты {род!r}: допустимы {', '.join(РОДЫ_ЛЕНТЫ)}")
    с.execute(
        "INSERT INTO feed (время, род, текст) VALUES (?, ?, ?)", (_сейчас(), род, текст)
    )


def в_ленту(с: sqlite3.Connection, род: str, текст: str) -> None:
    """Строка ленты. Род — `скидка`, `сбой` либо `итог`; чужой род — ошибка вызова."""
    with _запись(с):
        _в_ленту(с, род, текст)


def запомнить_отказ(с: sqlite3.Connection, номер: int, отпечаток: str) -> None:
    """Модель отказала игре при этом вкусе. Повтор при том же отпечатке ничего не меняет."""
    with _запись(с):
        с.execute(
            "INSERT OR IGNORE INTO refusals (номер, отпечаток, отказано) VALUES (?, ?, ?)",
            (номер, отпечаток, _сейчас()),
        )


def убрать_из_календаря(с: sqlite3.Connection, номер: int) -> None:
    """Игра вышла или выпала из выдачи — строка календаря удаляется."""
    with _запись(с):
        с.execute("DELETE FROM calendar WHERE номер = ?", (номер,))


def обновить_библиотеку(с: sqlite3.Connection, игры: list) -> None:
    """Библиотека Steam целиком заменяет прежнюю одной транзакцией: проданная или удалённая
    игра уходит, читатель не видит половины. Элемент — любой объект с полями `номер`, `имя`,
    `часы`, `часы_2нед`, `последний_запуск` (`steam_account.Своя`)."""
    with _запись(с):
        с.execute("DELETE FROM library")
        с.executemany(
            "INSERT OR REPLACE INTO library (номер, имя, часы, часы_2нед, последний_запуск) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (и.номер, и.имя, float(и.часы), float(и.часы_2нед), и.последний_запуск)
                for и in игры
            ],
        )


def обновить_список_steam(с: sqlite3.Connection, номера: list[int]) -> None:
    """Список желаемого Steam целиком заменяет прежний одной транзакцией."""
    with _запись(с):
        с.execute("DELETE FROM steam_wishlist")
        с.executemany(
            "INSERT OR IGNORE INTO steam_wishlist (номер) VALUES (?)", [(н,) for н in номера]
        )


def уже_играл(с: sqlite3.Connection, номер: int, понравилась: bool) -> None:
    """Игра вне Steam, в которую человек уже играл: из выдачи она уходит. Повтор меняет ответ."""
    with _запись(с):
        с.execute(
            """
            INSERT INTO played (номер, понравилась, отмечено) VALUES (?, ?, ?)
            ON CONFLICT (номер) DO UPDATE SET
                понравилась = excluded.понравилась, отмечено = excluded.отмечено
            """,
            (номер, int(понравилась), _сейчас()),
        )


def _записать_пару(с: sqlite3.Connection, номер: int, пара: int | None) -> None:
    """`записать_пару` внутри открытой транзакции."""
    сейчас = _сейчас()
    с.execute(
        "INSERT INTO pairs (номер, пара, сверено) VALUES (?, ?, ?) "
        "ON CONFLICT (номер) DO UPDATE SET пара = excluded.пара, сверено = excluded.сверено",
        (номер, пара, сейчас),
    )
    if пара is not None:
        с.execute(
            "INSERT INTO pairs (номер, пара, сверено) VALUES (?, ?, ?) "
            "ON CONFLICT (номер) DO UPDATE SET "
            "пара = excluded.пара, сверено = excluded.сверено WHERE pairs.пара IS NULL",
            (пара, номер, сейчас),
        )


def записать_пару(с: sqlite3.Connection, номер: int, пара: int | None) -> None:
    """Та же игра в другом магазине; обе строки — одной транзакцией.

    Строка самого `номер` пишется всегда. Обратная строка `пара → номер` — только если у `пара`
    найденной пары ещё нет (строки нет либо в ней `NULL`): у одной игры Steam бывают две записи
    eShop (Switch и Switch 2 Edition), и вторая не перетирает первую. Пары нет (`None`) —
    искали, не нашли: трогается только строка `номер`, чтобы второй раз не искать."""
    with _запись(с):
        _записать_пару(с, номер, пара)


def включить_eshop(с: sqlite3.Connection, вкл: bool) -> None:
    """Включает или выключает eShop: отметка `eshop` — «вкл» либо «выкл»."""
    поставить_отметку(с, "eshop", "вкл" if вкл else "выкл")


def eshop_включён(с: sqlite3.Connection) -> bool:
    """Выбор человека: искать ли игры Nintendo eShop. Одно место правды для веба, заданий и
    советника. Отметки нет — включён: так было до выключателя."""
    return отметка(с, "eshop") != "выкл"


def поставить_отметку(с: sqlite3.Connection, ключ: str, значение: str) -> None:
    """Отметка заданий: «последняя_сверка», «выход_сообщён:<номер>», «eshop» (выключатель
    eShop, `включить_eshop`), «страна_steam» и «страна_eshop» (`задать_страны`). Своя таблица, а не `meta`:
    ключ отметки не может задеть версию схемы."""
    with _запись(с):
        с.execute(
            "INSERT INTO marks (ключ, значение) VALUES (?, ?) "
            "ON CONFLICT (ключ) DO UPDATE SET значение = excluded.значение",
            (ключ, значение),
        )


def снять_отметку(с: sqlite3.Connection, ключ: str) -> None:
    """Убирает отметку; её нет — ничего не делает (выход из Steam снимает «страна_профиля»)."""
    with _запись(с):
        с.execute("DELETE FROM marks WHERE ключ = ?", (ключ,))


def _код_страны(код: str) -> str:
    """Код ISO 3166-1: ровно две заглавные латинские буквы; иное — `ValueError`."""
    if not (isinstance(код, str) and len(код) == 2 and all("A" <= б <= "Z" for б in код)):
        raise ValueError(f"код страны {код!r}: нужны две заглавные латинские буквы ISO 3166-1")
    return код


def _задать_страны(
    с: sqlite3.Connection, *, steam: str | None = None, eshop: str | None = None
) -> None:
    """`задать_страны` внутри открытой транзакции."""
    новые = {
        ключ: _код_страны(код)
        for ключ, код in (("страна_steam", steam), ("страна_eshop", eshop))
        if код is not None
    }
    с.executemany(
        "INSERT INTO marks (ключ, значение) VALUES (?, ?) "
        "ON CONFLICT (ключ) DO UPDATE SET значение = excluded.значение",
        list(новые.items()),
    )


def задать_страны(
    с: sqlite3.Connection, *, steam: str | None = None, eshop: str | None = None
) -> None:
    """Страна магазина Steam и страна eShop — отметки «страна_steam» и «страна_eshop».
    `None` — страну этого магазина не трогать. Коды проверяются до записи, обе отметки ложатся
    одной транзакцией: неверный код одного магазина не записывает и верный код другого."""
    with _запись(с):
        _задать_страны(с, steam=steam, eshop=eshop)


def _отметка_выхода(номер: int) -> str:
    """Ключ отметки «выход игры уже сообщён» (задания, `jobs.py`)."""
    return f"выход_сообщён:{номер}"


def _удалить_данные_зоны(с: sqlite3.Connection, номер: int) -> None:
    """Строки, верные только для этого номера в его магазине и стране: карточка, цены, история
    цен, отказы модели и пары — с обеих сторон (строка самого номера и строки, где он — пара).
    Вызывается внутри открытой транзакции."""
    for таблица in ("games", "prices", "price_history", "refusals"):
        с.execute(f"DELETE FROM {таблица} WHERE номер = ?", (номер,))
    с.execute("DELETE FROM pairs WHERE номер = ? OR пара = ?", (номер, номер))


def _снять_из_работы(с: sqlite3.Connection, номер: int) -> None:
    """Решения по игре — предложение, календарь, «уже играл», отказы модели, отметка о выходе —
    без карточки, цен и пар. Нужна при смене страны Steam игре из списка желаемого Steam, которой
    нет в новой стране: список ведёт Steam, карточка ему нужна. Внутри открытой транзакции."""
    for таблица in ("suggestions", "calendar", "played", "refusals"):
        с.execute(f"DELETE FROM {таблица} WHERE номер = ?", (номер,))
    с.execute("DELETE FROM marks WHERE ключ = ?", (_отметка_выхода(номер),))


def _удалить_игру(с: sqlite3.Connection, номер: int) -> None:
    """`удалить_игру` внутри открытой транзакции."""
    _снять_из_работы(с, номер)
    _удалить_данные_зоны(с, номер)


def удалить_игру(с: sqlite3.Connection, номер: int) -> None:
    """Игра уходит из хранилища одной транзакцией: предложения, календарь, «уже играл», отметка
    о выходе и данные зоны (`_удалить_данные_зоны`). Библиотека и список желаемого Steam — данные
    самого Steam — не трогаются. Нужна при смене региона: игры нет в новой стране."""
    with _запись(с):
        _удалить_игру(с, номер)


def _заменить_номер(с: sqlite3.Connection, старый: int, новый: int) -> None:
    """`заменить_номер` внутри открытой транзакции."""
    if старый == новый:
        return
    for таблица in ("suggestions", "calendar", "played"):
        # `OR IGNORE`: строка, чей новый номер уже занят, остаётся на старом и удаляется ниже.
        с.execute(f"UPDATE OR IGNORE {таблица} SET номер = ? WHERE номер = ?", (новый, старый))
        с.execute(f"DELETE FROM {таблица} WHERE номер = ?", (старый,))
    с.execute(
        "UPDATE OR IGNORE marks SET ключ = ? WHERE ключ = ?",
        (_отметка_выхода(новый), _отметка_выхода(старый)),
    )
    с.execute("DELETE FROM marks WHERE ключ = ?", (_отметка_выхода(старый),))
    _удалить_данные_зоны(с, старый)


def заменить_номер(с: sqlite3.Connection, старый: int, новый: int) -> None:
    """Игра получила другой номер (eShop, другая зона) — одной транзакцией.

    Решения человека и заданий переносятся на новый номер: строки `suggestions` (с оценкой),
    `calendar`, `played` и отметка о выходе. Если у нового номера такая строка уже есть, она
    остаётся как была, а строка старого номера удаляется: новое не затирается. Карточка, цены,
    история цен, отказы модели и пары старого номера удаляются и не переносятся — это данные
    прежней зоны; их заново наберут задания. Библиотека и список Steam не трогаются. Номер равен
    сам себе — ничего не делает."""
    with _запись(с):
        _заменить_номер(с, старый, новый)


def _сбросить_цены(с: sqlite3.Connection, *, eshop: bool) -> None:
    """`сбросить_цены` внутри открытой транзакции."""
    номера = [
        (н,)
        for (н,) in с.execute(
            "SELECT номер FROM prices UNION SELECT номер FROM price_history"
        ).fetchall()
        if это_eshop(н) == eshop
    ]
    с.executemany("DELETE FROM prices WHERE номер = ?", номера)
    с.executemany("DELETE FROM price_history WHERE номер = ?", номера)


def сбросить_цены(с: sqlite3.Connection, *, eshop: bool) -> None:
    """Последние цены и история цен одного магазина (`это_eshop`) — одной транзакцией. Нужна при
    смене страны: сменилась валюта, прежние цены и минимум несравнимы с новыми."""
    with _запись(с):
        _сбросить_цены(с, eshop=eshop)


def _сбросить_пары(с: sqlite3.Connection) -> None:
    """`сбросить_пары` внутри открытой транзакции."""
    с.execute("DELETE FROM pairs")


def сбросить_пары(с: sqlite3.Connection) -> None:
    """Все пары «та же игра в другом магазине», найденные и ненайденные. Нужна при смене зоны
    eShop: номера и каталог другие, пары ищутся заново."""
    with _запись(с):
        _сбросить_пары(с)


class Запись:
    """Записи пересборки внутри одной открытой транзакции (`транзакция`): применяются все или
    ни одной. Методы — те же функции модуля без своей транзакции. После выхода из `транзакция`
    запись закрыта: вызов — `RuntimeError`, а не молчаливая запись без транзакции."""

    def __init__(self, с: sqlite3.Connection) -> None:
        self._с: sqlite3.Connection | None = с

    def _соединение(self) -> sqlite3.Connection:
        if self._с is None:
            raise RuntimeError("транзакция закрыта: запись вне `транзакция` не допускается")
        return self._с

    def запомнить_игру(self, игра: Игра) -> None:
        _запомнить_игру(self._соединение(), игра)

    def удалить_игру(self, номер: int) -> None:
        _удалить_игру(self._соединение(), номер)

    def снять_из_работы(self, номер: int) -> None:
        _снять_из_работы(self._соединение(), номер)

    def обновить_цену(
        self, номер: int, *, цена: int | None, цена_без_скидки: int | None, скидка: int,
        валюта: str, скидка_до: str,
    ) -> None:
        _обновить_цену(self._соединение(), номер, цена=цена, цена_без_скидки=цена_без_скидки,
                       скидка=скидка, валюта=валюта, скидка_до=скидка_до)

    def заменить_номер(self, старый: int, новый: int) -> None:
        _заменить_номер(self._соединение(), старый, новый)

    def сбросить_цены(self, *, eshop: bool) -> None:
        _сбросить_цены(self._соединение(), eshop=eshop)

    def сбросить_пары(self) -> None:
        _сбросить_пары(self._соединение())

    def записать_пару(self, номер: int, пара: int | None) -> None:
        _записать_пару(self._соединение(), номер, пара)

    def задать_страны(self, *, steam: str | None = None, eshop: str | None = None) -> None:
        _задать_страны(self._соединение(), steam=steam, eshop=eshop)

    def в_ленту(self, род: str, текст: str) -> None:
        _в_ленту(self._соединение(), род, текст)


@contextmanager
def транзакция(с: sqlite3.Connection) -> Iterator[Запись]:
    """Одна транзакция на много записей (пересборка при смене региона): фиксация на выходе,
    откат всего при исключении."""
    запись = Запись(с)
    try:
        with _запись(с):
            yield запись
    finally:
        запись._с = None


# ---- чтение ----

# Игры, которых выдача не показывает: с минусом, купленные, «уже играл».
_СКРЫТЫЕ = """
    SELECT номер FROM suggestions WHERE оценка = 0
    UNION SELECT номер FROM library
    UNION SELECT номер FROM played
"""

# Поля игры для соединения с другими таблицами. Игры может не быть в `games`
# (предложение завели раньше, чем запомнили игру), поэтому везде `LEFT JOIN`.
_ПОЛЯ_ИГРЫ = """
    g.имя, g.описание, g.жанры, g.дата_текст, g.дата, g.вышла, g.цена, g.цена_без_скидки,
    g.скидка, g.валюта, g.бесплатна, g.отзывы_сводка, g.отзывы_положительных,
    g.отзывы_всего, g.обновлено, g.обложка, g.адрес, g.объект, g.скидка_до
"""

# Та же игра в другом магазине: одна плашка показывает обе цены. Пары может не быть — `NULL`.
_ПОЛЯ_ПАРЫ = """
    pr.пара AS пара, e.цена AS пара_цена, e.цена_без_скидки AS пара_цена_без_скидки,
    e.скидка AS пара_скидка, e.валюта AS пара_валюта, e.адрес AS пара_адрес,
    e.скидка_до AS пара_скидка_до
"""

# Порядок магазинов в поле `магазины`.
_МАГАЗИНЫ = ("steam", "eshop")


def _с_парой(псевдоним: str) -> str:
    return (
        f"LEFT JOIN pairs pr ON pr.номер = {псевдоним}.номер "
        "LEFT JOIN games e ON e.номер = pr.пара"
    )


def _словарь(строка: sqlite3.Row) -> dict:
    """Строка в словарь: жанры — список, флаги — `bool`, ссылка и текст отзывов добавлены.

    `магазин` — «steam» или «eshop» по номеру; `магазины` — он и магазин пары, если она есть,
    в порядке `steam`, `eshop`. Ссылка игры eShop — её страница в eShop."""
    д = dict(строка)
    if "жанры" in д:
        д["жанры"] = json.loads(д["жанры"]) if д["жанры"] else []
    for флаг in ("вышла", "бесплатна"):
        if д.get(флаг) is not None:
            д[флаг] = bool(д[флаг])
    if "оценка" in д and д["оценка"] is not None:
        д["оценка"] = bool(д["оценка"])
    if д.get("отзывы_всего") is not None:
        д["отзывы"] = Отзывы(
            д["отзывы_сводка"] or "", д["отзывы_положительных"] or 0, д["отзывы_всего"]
        ).текст()
    д["магазин"] = _магазин(д["номер"])
    свои = {д["магазин"]}
    if д.get("пара") is not None:
        свои.add(_магазин(д["пара"]))
    д["магазины"] = [м for м in _МАГАЗИНЫ if м in свои]
    д["ссылка"] = д.get("адрес") or f"{МАГАЗИН}/app/{д['номер']}/"
    return д


def _магазин(номер: int) -> str:
    return "eshop" if это_eshop(номер) else "steam"


def игра(с: sqlite3.Connection, номер: int) -> dict | None:
    строка = с.execute(
        f"SELECT g.номер, {_ПОЛЯ_ИГРЫ}, {_ПОЛЯ_ПАРЫ} FROM games g {_с_парой('g')} "
        "WHERE g.номер = ?",
        (номер,),
    ).fetchone()
    return _словарь(строка) if строка else None


def предложения(с: sqlite3.Connection, только_без_оценки: bool = False) -> list[dict]:
    """Предложения с полями игры, новые первыми. Игр с минусом, купленных и «уже играл» нет."""
    условие = "AND s.оценка IS NULL" if только_без_оценки else ""
    строки = с.execute(
        f"""
        SELECT s.номер, s.причина, s.предложено, s.оценка, s.причина_оценки, s.оценено,
            {_ПОЛЯ_ИГРЫ}, {_ПОЛЯ_ПАРЫ}
        FROM suggestions s LEFT JOIN games g ON g.номер = s.номер {_с_парой("s")}
        WHERE s.номер NOT IN (SELECT номер FROM library UNION SELECT номер FROM played)
            AND s.оценка IS NOT 0 {условие}
        ORDER BY s.предложено DESC, s.rowid DESC
        """
    ).fetchall()
    return [_словарь(с_) for с_ in строки]


def календарь(с: sqlite3.Connection) -> list[dict]:
    """Игры календаря по дате выхода; без точной даты — в конце. Игр с минусом, купленных и
    «уже играл» нет."""
    строки = с.execute(
        f"""
        SELECT c.номер, c.причина, c.добавлено, {_ПОЛЯ_ИГРЫ}, {_ПОЛЯ_ПАРЫ}
        FROM calendar c LEFT JOIN games g ON g.номер = c.номер {_с_парой("c")}
        WHERE c.номер NOT IN ({_СКРЫТЫЕ})
        ORDER BY g.дата IS NULL, g.дата, c.добавлено, c.номер
        """
    ).fetchall()
    return [_словарь(с_) for с_ in строки]


def желаемое(с: sqlite3.Connection) -> list[dict]:
    """Игры с плюсом и список желаемого Steam, без купленных: поля игры и последняя известная
    цена из `отметить_цену`. Своё — первым, по времени оценки; затем Steam — по номеру.

    `источник` — «свой» либо «steam»; игра в обоих списках — «свой». Поля `цена` и `скидка` —
    из карточки игры; `последняя_цена`, `последняя_скидка` и `цена_отмечена` — из отметок цены,
    `None`, если цену ещё не отмечали. Игры списка Steam может не быть в `games` — имя `None`.
    Поля `пара_*` — цена той же игры в другом магазине, как у `предложения` и `календарь`."""
    строки = с.execute(
        f"""
        SELECT w.номер, w.источник, s.причина_оценки, s.оценено, {_ПОЛЯ_ИГРЫ},
            p.цена AS последняя_цена, p.скидка AS последняя_скидка, p.отмечено AS цена_отмечена,
            {_ПОЛЯ_ПАРЫ}
        FROM (
            SELECT номер, 'свой' AS источник FROM suggestions WHERE оценка = 1
            UNION ALL
            SELECT номер, 'steam' FROM steam_wishlist
            WHERE номер NOT IN (SELECT номер FROM suggestions WHERE оценка = 1)
        ) w
        LEFT JOIN suggestions s ON s.номер = w.номер AND s.оценка = 1
        LEFT JOIN games g ON g.номер = w.номер
        LEFT JOIN prices p ON p.номер = w.номер
        {_с_парой("w")}
        WHERE w.номер NOT IN (SELECT номер FROM library)
        ORDER BY w.источник = 'steam', s.оценено DESC, w.номер
        """
    ).fetchall()
    return [_словарь(с_) for с_ in строки]


def показанные(с: sqlite3.Connection) -> set[int]:
    """Номера, которые повторно не судить: предложения, календарь и «уже играл»."""
    строки = с.execute(
        "SELECT номер FROM suggestions UNION SELECT номер FROM calendar "
        "UNION SELECT номер FROM played"
    ).fetchall()
    return {с_[0] for с_ in строки}


def отказано(с: sqlite3.Connection, отпечаток: str) -> set[int]:
    """Номера, которым модель отказала при этом вкусе. Вкус другой — прежние отказы не в счёт."""
    строки = с.execute(
        "SELECT номер FROM refusals WHERE отпечаток = ?", (отпечаток,)
    ).fetchall()
    return {с_[0] for с_ in строки}


def открытых(с: sqlite3.Connection, без_eshop: bool = False) -> int:
    """Число открытых предложений: без оценки, не купленных, не «уже играл». `без_eshop` —
    без записей eShop (`это_eshop`): при выключенном eShop их не видно, мест они не занимают."""
    номера = с.execute(
        """
        SELECT номер FROM suggestions
        WHERE оценка IS NULL
            AND номер NOT IN (SELECT номер FROM library UNION SELECT номер FROM played)
        """
    ).fetchall()
    return sum(1 for (н,) in номера if not (без_eshop and это_eshop(н)))


def библиотека(с: sqlite3.Connection) -> list[dict]:
    """Библиотека Steam по убыванию часов игры."""
    строки = с.execute(
        "SELECT номер, имя, часы, часы_2нед, последний_запуск FROM library "
        "ORDER BY часы DESC, номер"
    ).fetchall()
    return [dict(с_) for с_ in строки]


def куплены(с: sqlite3.Connection) -> set[int]:
    return {с_[0] for с_ in с.execute("SELECT номер FROM library").fetchall()}


def список_steam(с: sqlite3.Connection) -> set[int]:
    return {с_[0] for с_ in с.execute("SELECT номер FROM steam_wishlist").fetchall()}


def пара(с: sqlite3.Connection, номер: int) -> int | None:
    """Номер той же игры в другом магазине; `None` — пары нет либо её ещё не искали."""
    строка = с.execute("SELECT пара FROM pairs WHERE номер = ?", (номер,)).fetchone()
    return строка[0] if строка else None


def пара_сверена(с: sqlite3.Connection, номер: int) -> bool:
    """Пару уже искали — найдена она или нет."""
    return с.execute("SELECT 1 FROM pairs WHERE номер = ?", (номер,)).fetchone() is not None


def минимум_наблюдений(с: sqlite3.Connection, номер: int) -> dict | None:
    """Наименьшая цена из своей истории: `цена`, `валюта`, `когда` — последний раз, когда цена
    опустилась до минимума (история хранит смены цены, а не дни), `с` — начало наблюдений.
    Исторического минимума eShop у IsThereAnyDeal нет, поэтому копим свой; он верен только
    с начала наблюдений. `None` — цену ещё не отмечали."""
    начало = с.execute(
        "SELECT min(отмечено) FROM price_history WHERE номер = ?", (номер,)
    ).fetchone()[0]
    if начало is None:
        return None
    строка = с.execute(
        "SELECT цена, валюта, отмечено FROM price_history WHERE номер = ? "
        "ORDER BY цена, отмечено DESC LIMIT 1",
        (номер,),
    ).fetchone()
    return {
        "цена": строка["цена"], "валюта": строка["валюта"], "когда": строка["отмечено"],
        "с": начало,
    }


def отметка(с: sqlite3.Connection, ключ: str) -> str | None:
    строка = с.execute("SELECT значение FROM marks WHERE ключ = ?", (ключ,)).fetchone()
    return строка[0] if строка else None


def лента(с: sqlite3.Connection, сколько: int | None = None) -> list[dict]:
    """Строки ленты, новые первыми. `None` — все."""
    запрос = "SELECT номер, время, род, текст FROM feed ORDER BY номер DESC"
    if сколько is None:
        строки = с.execute(запрос).fetchall()
    else:
        строки = с.execute(запрос + " LIMIT ?", (сколько,)).fetchall()
    return [dict(с_) for с_ in строки]


def страна_steam(с: sqlite3.Connection) -> str | None:
    """Страна магазина Steam (код ISO 3166-1); `None` — не выбрана."""
    return отметка(с, "страна_steam")


def страна_eshop(с: sqlite3.Connection) -> str | None:
    """Страна eShop (код ISO 3166-1); `None` — не выбрана."""
    return отметка(с, "страна_eshop")


def номера_в_работе(с: sqlite3.Connection) -> list[int]:
    """Номера, которые пересобирает смена региона: предложения (с любой оценкой), календарь и
    «уже играл», без купленных. По возрастанию номера."""
    строки = с.execute(
        """
        SELECT номер FROM suggestions UNION SELECT номер FROM calendar
        UNION SELECT номер FROM played
        EXCEPT SELECT номер FROM library
        ORDER BY номер
        """
    ).fetchall()
    return [с_[0] for с_ in строки]
