"""Хранилище игр на временном файле. Каталог данных уводится в `tmp_path`:
настоящий `~/.local/share` проверки не трогают."""

import sqlite3
import threading
from datetime import date

import pytest

import games_db as db
from steam_store import Игра, Отзывы


@pytest.fixture(autouse=True)
def каталог(tmp_path, monkeypatch):
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path / "данные"))
    return tmp_path / "данные"


@pytest.fixture
def с():
    соединение = db.открыть()
    yield соединение
    соединение.close()


def test_путь_из_окружения(каталог):
    assert db.путь() == каталог / "games.sqlite"


def test_файл_и_режим(с, каталог):
    assert (каталог / "games.sqlite").exists()
    assert с.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert с.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert с.execute(
        "SELECT значение FROM meta WHERE ключ = 'версия_схемы'"
    ).fetchone()[0] == str(db.ВЕРСИЯ_СХЕМЫ)


def test_пара_скидки(с):
    номер = 10
    assert db.отметить_цену(с, номер, 50000, 50) is True
    assert db.отметить_цену(с, номер, 50000, 50) is False
    assert db.отметить_цену(с, номер, 40000, 60) is True
    assert db.отметить_цену(с, номер, 100000, 0) is False
    assert db.отметить_цену(с, номер, 100000, 0) is False
    # После цены без скидки та же скидка снова новость.
    assert db.отметить_цену(с, номер, 40000, 60) is True
    assert db.отметить_цену(с, номер, 40000, 60) is False


def test_показанные_и_повтор(с):
    assert db.предложить(с, 1, "любит стратегии") is True
    assert db.предложить(с, 1, "ещё раз") is False
    assert db.в_календарь(с, 2, "ждёт выхода") is True
    assert db.в_календарь(с, 2, "ещё раз") is False
    assert db.показанные(с) == {1, 2}


def test_оценки(с):
    db.предложить(с, 1, "первая")
    db.предложить(с, 2, "вторая")
    db.оценить(с, 1, True, "нравится")
    db.оценить(с, 2, False, "не моё")
    assert [и["номер"] for и in db.желаемое(с)] == [1]
    assert 2 in db.показанные(с)
    db.снять_оценку(с, 2)
    assert 2 not in db.показанные(с)
    assert db.предложить(с, 2, "снова") is True


def test_оценка_игры_календаря(с):
    db.в_календарь(с, 3, "ждёт выхода")
    db.оценить(с, 3, True, "жду")
    assert [и["номер"] for и in db.желаемое(с)] == [3]
    assert db.предложить(с, 3, "повтор") is False


def test_желаемое_с_ценой(с):
    db.запомнить_игру(с, Игра(номер=5, имя="Игра", цена=100000, скидка=0, валюта="RUB"))
    db.оценить(с, 5, True, "нравится")
    db.отметить_цену(с, 5, 50000, 50)
    [и] = db.желаемое(с)
    assert и["имя"] == "Игра"
    assert и["последняя_цена"] == 50000 and и["последняя_скидка"] == 50


def test_игра_повтор_обновляет(с):
    игра = Игра(
        номер=7, имя="Старое имя", жанры=("Стратегии", "Инди"), дата_текст="3 авг. 2023 г.",
        дата=date(2023, 8, 3), цена=199900, отзывы=Отзывы("Очень положительные", 90, 100),
    )
    db.запомнить_игру(с, игра)
    db.запомнить_игру(с, Игра(**{**игра.__dict__, "имя": "Новое имя"}))
    запись = db.игра(с, 7)
    assert запись["имя"] == "Новое имя"
    assert запись["жанры"] == ["Стратегии", "Инди"]
    assert запись["дата"] == "2023-08-03" and запись["дата_текст"] == "3 авг. 2023 г."
    assert запись["отзывы"].startswith("Очень положительные, 90 %")
    assert запись["ссылка"].endswith("/app/7/")
    assert db.игра(с, 8) is None


def test_календарь_по_дате(с):
    db.запомнить_игру(с, Игра(номер=1, имя="Поздняя", дата=date(2027, 5, 1), вышла=False))
    db.запомнить_игру(с, Игра(номер=2, имя="Без даты", дата_текст="2027", вышла=False))
    db.запомнить_игру(с, Игра(номер=3, имя="Ранняя", дата=date(2026, 11, 1), вышла=False))
    for номер in (1, 2, 3):
        db.в_календарь(с, номер, "ждёт")
    assert [и["номер"] for и in db.календарь(с)] == [3, 1, 2]


def test_предложения_без_оценки(с):
    db.предложить(с, 1, "а")
    db.предложить(с, 2, "б")
    db.оценить(с, 1, True, "да")
    assert [и["номер"] for и in db.предложения(с)] == [2, 1]
    assert [и["номер"] for и in db.предложения(с, только_без_оценки=True)] == [2]


def test_лента(с):
    db.в_ленту(с, "скидка", "первая")
    db.в_ленту(с, "итог", "вторая")
    assert [з["текст"] for з in db.лента(с)] == ["вторая", "первая"]
    assert [з["текст"] for з in db.лента(с, 1)] == ["вторая"]
    with pytest.raises(ValueError):
        db.в_ленту(с, "прочее", "текст")


def test_два_соединения_пишут_поочерёдно(с):
    ошибки: list[BaseException] = []

    def писать(метка: str) -> None:
        соединение = db.открыть()
        try:
            for номер in range(50):
                db.в_ленту(соединение, "итог", f"{метка} {номер}")
                db.отметить_цену(соединение, номер, 1000 + номер, 10)
        except BaseException as exc:  # noqa: BLE001 — ошибку потока несём в проверку
            ошибки.append(exc)
        finally:
            соединение.close()

    потоки = [threading.Thread(target=писать, args=(м,)) for м in ("а", "б")]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join()
    assert ошибки == []
    assert len(db.лента(с)) == 100


def test_чтение_видит_запись(с):
    db.в_ленту(с, "сбой", "магазин молчит")
    читатель = db.открыть(только_чтение=True)
    try:
        assert [з["текст"] for з in db.лента(читатель)] == ["магазин молчит"]
        with pytest.raises(sqlite3.OperationalError):
            читатель.execute("DELETE FROM feed")
    finally:
        читатель.close()


def test_новая_схема_не_трогается(с):
    с.execute("UPDATE meta SET значение = '99' WHERE ключ = 'версия_схемы'")
    с.close()
    with pytest.raises(db.НоваяСхема, match="99"):
        db.открыть()
    with pytest.raises(db.НоваяСхема, match="99"):
        db.открыть(только_чтение=True)
    # Файл не пересоздан: версия осталась прежней.
    сырое = sqlite3.connect(db.путь())
    assert сырое.execute("SELECT значение FROM meta").fetchone()[0] == "99"
    сырое.close()


def test_чтение_без_файла(каталог):
    with pytest.raises(FileNotFoundError, match="games.sqlite"):
        db.открыть(только_чтение=True)
    assert not каталог.exists()


# ---- схема 2 ----

# Схема версии 1 — дословно как её писал код шагов 1–3. Копия, а не `db.СХЕМА`: правка
# модуля не должна менять старый файл, на котором проверяется переход.
СХЕМА_1 = """
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


def _файл_версии_1(файл) -> None:
    файл.parent.mkdir(parents=True, exist_ok=True)
    сырое = sqlite3.connect(файл)
    сырое.executescript(СХЕМА_1)
    сырое.execute("INSERT INTO meta VALUES ('версия_схемы', '1')")
    сырое.execute(
        "INSERT INTO suggestions (номер, причина, предложено) VALUES (42, 'старое', '2026-10-01')"
    )
    сырое.commit()
    сырое.close()


def test_переход_с_версии_1(каталог):
    _файл_версии_1(db.путь())
    # Читатель переходить не вправе: файл версии 1 он отвергает и не трогает.
    with pytest.raises(db.НоваяСхема, match="версия схемы 1"):
        db.открыть(только_чтение=True)
    с = db.открыть()
    try:
        таблицы = {с_[0] for с_ in с.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"refusals", "library", "steam_wishlist", "played", "marks"} <= таблицы
        assert [и["номер"] for и in db.предложения(с)] == [42]
        assert с.execute(
            "SELECT значение FROM meta WHERE ключ = 'версия_схемы'"
        ).fetchone()[0] == str(db.ВЕРСИЯ_СХЕМЫ)
    finally:
        с.close()
    # Повторное открытие перехода не повторяет.
    db.открыть().close()


def test_незнакомая_старая_версия(каталог):
    _файл_версии_1(db.путь())
    сырое = sqlite3.connect(db.путь())
    сырое.execute("UPDATE meta SET значение = '0'")
    сырое.commit()
    сырое.close()
    with pytest.raises(db.НоваяСхема, match="перехода нет"):
        db.открыть()


def test_отказы_по_отпечатку(с):
    db.запомнить_отказ(с, 1, "вкус-а")
    db.запомнить_отказ(с, 1, "вкус-а")
    db.запомнить_отказ(с, 2, "вкус-а")
    assert db.отказано(с, "вкус-а") == {1, 2}
    assert db.отказано(с, "вкус-б") == set()


class _Своя:
    def __init__(self, номер, имя, часы, часы_2нед=0.0, последний_запуск=None):
        self.номер, self.имя, self.часы = номер, имя, часы
        self.часы_2нед, self.последний_запуск = часы_2нед, последний_запуск


def test_купленная_пропадает_из_выдачи(с):
    db.предложить(с, 1, "предложение")
    db.в_календарь(с, 1, "календарь")
    db.оценить(с, 1, True, "хочу")
    db.обновить_список_steam(с, [1])
    assert [и["номер"] for и in db.желаемое(с)] == [1]
    db.обновить_библиотеку(с, [_Своя(1, "Купленная", 3.5)])
    assert db.куплены(с) == {1}
    assert db.предложения(с) == []
    assert db.календарь(с) == []
    assert db.желаемое(с) == []


def test_минус_убирает_из_календаря(с):
    db.в_календарь(с, 1, "ждёт")
    db.в_календарь(с, 2, "ждёт")
    db.оценить(с, 1, False, "не моё")
    assert [и["номер"] for и in db.календарь(с)] == [2]
    assert [и["номер"] for и in db.предложения(с)] == []
    db.убрать_из_календаря(с, 2)
    assert db.календарь(с) == []


def test_открытых(с):
    for номер in (1, 2, 3, 4, 5):
        db.предложить(с, номер, "п")
    db.оценить(с, 1, True, "да")
    db.оценить(с, 2, False, "нет")
    db.обновить_библиотеку(с, [_Своя(3, "Купил", 1.0)])
    db.уже_играл(с, 4, понравилась=True)
    assert db.открытых(с) == 1
    assert [и["номер"] for и in db.предложения(с, только_без_оценки=True)] == [5]


def test_открытых_без_eshop(с):
    for номер in (5, 70010000000001, 70010000000002):
        db.предложить(с, номер, "п")
    assert db.открытых(с) == 3
    assert db.открытых(с, без_eshop=True) == 1


def test_уже_играл_в_показанных(с):
    db.уже_играл(с, 9, понравилась=False)
    assert 9 in db.показанные(с)
    db.в_календарь(с, 9, "ждёт")
    assert db.календарь(с) == []


def test_желаемое_со_списком_steam(с):
    db.запомнить_игру(с, Игра(номер=5, имя="Своя"))
    db.оценить(с, 5, True, "нравится")
    db.обновить_список_steam(с, [5, 6])
    желаемое = db.желаемое(с)
    assert [(и["номер"], и["источник"]) for и in желаемое] == [(5, "свой"), (6, "steam")]
    # Игры списка Steam ещё нет в `games` — имя неизвестно, запись всё равно есть.
    assert желаемое[1]["имя"] is None
    assert db.список_steam(с) == {5, 6}
    db.обновить_список_steam(с, [7])
    assert db.список_steam(с) == {7}


def test_библиотека_по_часам_и_замена(с):
    db.обновить_библиотеку(с, [_Своя(1, "Мало", 2.0), _Своя(2, "Много", 120.0, 5.5, 1760000000)])
    assert [(и["номер"], и["часы"]) for и in db.библиотека(с)] == [(2, 120.0), (1, 2.0)]
    assert db.библиотека(с)[0]["последний_запуск"] == 1760000000
    db.обновить_библиотеку(с, [_Своя(3, "Новая", 1.0)])
    assert db.куплены(с) == {3}


def test_отметки(с):
    assert db.отметка(с, "последняя_сверка") is None
    db.поставить_отметку(с, "последняя_сверка", "2026-10-16")
    db.поставить_отметку(с, "последняя_сверка", "2027-04-16")
    assert db.отметка(с, "последняя_сверка") == "2027-04-16"


def test_переход_2_в_3_и_обложка(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path))
    с = db.открыть()
    db.запомнить_игру(с, Игра(номер=7, имя="семь"))
    # Откатываем файл к версии 2: без столбца обложки и без добавок версии 4, с данными.
    с.execute("BEGIN IMMEDIATE"); с.execute("ALTER TABLE games DROP COLUMN обложка")
    for колонка in ("адрес", "объект", "скидка_до"):
        с.execute(f"ALTER TABLE games DROP COLUMN {колонка}")
    с.execute("DROP TABLE pairs"); с.execute("DROP TABLE price_history")
    с.execute("UPDATE meta SET значение = '2' WHERE ключ = 'версия_схемы'"); с.execute("COMMIT"); с.close()
    с = db.открыть()
    assert db.игра(с, 7)["имя"] == "семь" and db.игра(с, 7)["обложка"] is None
    db.запомнить_игру(с, Игра(номер=7, имя="семь", обложка="https://x/header.jpg"))
    db.запомнить_игру(с, Игра(номер=7, имя="семь"))  # без адреса — прежний не стирается
    assert db.игра(с, 7)["обложка"] == "https://x/header.jpg"


# ---- схема 4 ----


# Добавки версий 2 и 3 — дословно как их писал код версии 3; копии по той же причине, что `СХЕМА_1`.
СХЕМА_2_КОПИЯ = """
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

СХЕМА_3_КОПИЯ = """
ALTER TABLE games ADD COLUMN обложка TEXT
"""


def _файл_версии_3(файл) -> None:
    """Файл ровно таким, каким его оставлял код версии 3: три части схемы, версия 3, данные."""
    файл.parent.mkdir(parents=True, exist_ok=True)
    сырое = sqlite3.connect(файл)
    сырое.executescript(СХЕМА_1)
    сырое.executescript(СХЕМА_2_КОПИЯ)
    сырое.executescript(СХЕМА_3_КОПИЯ)
    сырое.execute("INSERT INTO meta VALUES ('версия_схемы', '3')")
    сырое.execute(
        "INSERT INTO games (номер, имя, цена, валюта, обновлено, обложка) "
        "VALUES (367520, 'Hollow Knight', 46500, 'RUB', '2026-10-01', 'https://x/h.jpg')"
    )
    сырое.execute(
        "INSERT INTO suggestions (номер, причина, предложено) VALUES (367520, 'метроидвания', '2026-10-01')"
    )
    сырое.commit()
    сырое.close()


def test_переход_3_в_4(каталог):
    _файл_версии_3(db.путь())
    with pytest.raises(db.НоваяСхема, match="версия схемы 3"):
        db.открыть(только_чтение=True)
    с = db.открыть()
    try:
        assert с.execute(
            "SELECT значение FROM meta WHERE ключ = 'версия_схемы'"
        ).fetchone()[0] == "4"
        игра = db.игра(с, 367520)
        assert игра["имя"] == "Hollow Knight" and игра["цена"] == 46500
        assert игра["обложка"] == "https://x/h.jpg"
        [предложение] = db.предложения(с)
        assert предложение["номер"] == 367520 and предложение["причина"] == "метроидвания"
        колонки = {с_[1] for с_ in с.execute("PRAGMA table_info(games)")}
        assert {"адрес", "объект", "скидка_до"} <= колонки
        таблицы = {с_[0] for с_ in с.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"pairs", "price_history"} <= таблицы
    finally:
        с.close()
    db.открыть().close()


def test_новый_файл_сразу_версии_4(с):
    assert с.execute(
        "SELECT значение FROM meta WHERE ключ = 'версия_схемы'"
    ).fetchone()[0] == "4"
    колонки = {с_[1] for с_ in с.execute("PRAGMA table_info(games)")}
    assert {"обложка", "адрес", "объект", "скидка_до"} <= колонки
    таблицы = {с_[0] for с_ in с.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"pairs", "price_history"} <= таблицы


def test_файл_версии_5_отвергнут(с):
    с.execute("UPDATE meta SET значение = '5' WHERE ключ = 'версия_схемы'")
    с.close()
    with pytest.raises(db.НоваяСхема, match="версия схемы 5, этот код знает только 4"):
        db.открыть()


def test_игра_eshop_поля(с):
    номер = 70010000000001
    игра = Игра(
        номер=номер, имя="Hollow Knight", цена=1499, валюта="USD",
        адрес="https://www.nintendo.com/us/store/products/hollow-knight-switch/",
        объект="70010000000001", скидка_до="2026-11-05",
    )
    db.запомнить_игру(с, игра)
    запись = db.игра(с, номер)
    assert запись["адрес"] == игра.адрес
    assert запись["объект"] == "70010000000001"
    assert запись["скидка_до"] == "2026-11-05"
    # Повтор без адреса и кода каталога их не стирает; срок скидки — как пришёл.
    db.запомнить_игру(с, Игра(номер=номер, имя="Hollow Knight", цена=1499, валюта="USD"))
    запись = db.игра(с, номер)
    assert запись["адрес"] == игра.адрес and запись["объект"] == "70010000000001"
    assert запись["скидка_до"] is None


def test_пары(с):
    db.записать_пару(с, 70010000000001, 367520)
    assert db.пара(с, 70010000000001) == 367520
    assert db.пара(с, 367520) == 70010000000001
    assert db.пара_сверена(с, 367520)
    db.записать_пару(с, 70010000000002, None)
    assert db.пара_сверена(с, 70010000000002)
    assert db.пара(с, 70010000000002) is None
    assert not db.пара_сверена(с, 70010000000003)
    assert db.пара(с, 70010000000003) is None


def test_история_цен_и_минимум(с, monkeypatch):
    номер = 70010000000001
    assert db.минимум_наблюдений(с, номер) is None
    дни = iter(["2026-10-01T10:00:00+00:00", "2026-10-02T10:00:00+00:00", "2026-10-03T10:00:00+00:00"])
    часы = {"сейчас": ""}

    def сейчас() -> str:
        return часы["сейчас"]

    monkeypatch.setattr(db, "_сейчас", сейчас)
    for цена, скидка in ((3999, 0), (3999, 0), (1999, 50)):
        часы["сейчас"] = next(дни)
        db.отметить_цену(с, номер, цена, скидка, валюта="USD")
    строки = с.execute(
        "SELECT цена, валюта, отмечено FROM price_history WHERE номер = ? ORDER BY отмечено", (номер,)
    ).fetchall()
    assert [(с_["цена"], с_["валюта"]) for с_ in строки] == [(3999, "USD"), (1999, "USD")]
    assert db.минимум_наблюдений(с, номер) == {
        "цена": 1999, "валюта": "USD",
        "когда": "2026-10-03T10:00:00+00:00", "с": "2026-10-01T10:00:00+00:00",
    }


def test_обновить_цену(с):
    номер = 70010000000001
    db.запомнить_игру(с, Игра(номер=номер, имя="Hollow Knight", цена=3999, валюта="USD"))
    с.execute("UPDATE games SET обновлено = '2000-01-01' WHERE номер = ?", (номер,))
    db.обновить_цену(
        с, номер, цена=1999, цена_без_скидки=3999, скидка=50, валюта="USD", скидка_до="2026-11-05"
    )
    запись = db.игра(с, номер)
    assert (запись["цена"], запись["цена_без_скидки"], запись["скидка"]) == (1999, 3999, 50)
    assert запись["валюта"] == "USD" and запись["скидка_до"] == "2026-11-05"
    assert запись["обновлено"] != "2000-01-01"
    # Строки нет — ничего не делает.
    db.обновить_цену(
        с, 70010000000009, цена=1, цена_без_скидки=1, скидка=0, валюта="USD", скидка_до=""
    )
    assert db.игра(с, 70010000000009) is None
    # Пустой срок скидки на существующей строке — `NULL`.
    db.обновить_цену(с, номер, цена=3999, цена_без_скидки=3999, скидка=0, валюта="USD", скидка_до="")
    assert db.игра(с, номер)["скидка_до"] is None


АДРЕС_ESHOP = "https://www.nintendo.com/us/store/products/hollow-knight-switch/"


def _пара_hollow_knight(с) -> None:
    db.запомнить_игру(с, Игра(номер=367520, имя="Hollow Knight", цена=46500, валюта="RUB"))
    db.запомнить_игру(с, Игра(
        номер=70010000000001, имя="Hollow Knight", цена=1999, цена_без_скидки=3999, скидка=50,
        валюта="USD", адрес=АДРЕС_ESHOP, скидка_до="2026-11-05",
    ))
    db.записать_пару(с, 70010000000001, 367520)
    db.запомнить_игру(с, Игра(
        номер=70010000000002, имя="Pikmin 4", цена=5999, валюта="USD",
        адрес="https://www.nintendo.com/us/store/products/pikmin-4-switch/",
    ))
    db.записать_пару(с, 70010000000002, None)


def _проверить_выдачу(записи: list[dict]) -> None:
    assert len(записи) == 2
    по_номеру = {з["номер"]: з for з in записи}
    steam = по_номеру[367520]
    assert steam["магазин"] == "steam" and steam["магазины"] == ["steam", "eshop"]
    assert steam["ссылка"].endswith("/app/367520/")
    assert steam["пара"] == 70010000000001
    assert (steam["пара_цена"], steam["пара_цена_без_скидки"], steam["пара_скидка"]) == (1999, 3999, 50)
    assert steam["пара_валюта"] == "USD" and steam["пара_адрес"] == АДРЕС_ESHOP
    assert steam["пара_скидка_до"] == "2026-11-05"
    eshop = по_номеру[70010000000002]
    assert eshop["магазин"] == "eshop" and eshop["магазины"] == ["eshop"]
    assert eshop["пара"] is None and eshop["пара_цена"] is None
    assert eshop["ссылка"] == "https://www.nintendo.com/us/store/products/pikmin-4-switch/"


def test_выдача_с_парой_предложения(с):
    _пара_hollow_knight(с)
    db.предложить(с, 367520, "метроидвания")
    db.предложить(с, 70010000000002, "игры Nintendo")
    _проверить_выдачу(db.предложения(с))


def test_выдача_с_парой_календарь(с):
    _пара_hollow_knight(с)
    db.в_календарь(с, 367520, "ждёт")
    db.в_календарь(с, 70010000000002, "ждёт")
    _проверить_выдачу(db.календарь(с))


def test_выдача_с_парой_желаемое(с):
    _пара_hollow_knight(с)
    db.оценить(с, 367520, True, "хочу")
    db.оценить(с, 70010000000002, True, "хочу")
    _проверить_выдачу(db.желаемое(с))


def test_игра_eshop_с_парой_знает_оба_магазина(с):
    _пара_hollow_knight(с)
    db.предложить(с, 70010000000001, "та же игра")
    [запись] = db.предложения(с)
    assert запись["магазин"] == "eshop" and запись["магазины"] == ["steam", "eshop"]
    assert запись["ссылка"] == АДРЕС_ESHOP and запись["пара"] == 367520


def test_пара_не_перетирается_второй_записью(с):
    # У игры Steam бывают две записи eShop (Switch и Switch 2 Edition): найденная пара остаётся.
    db.записать_пару(с, 70010000000001, 367520)
    db.записать_пару(с, 70010000000002, 367520)
    assert db.пара(с, 367520) == 70010000000001
    assert db.пара(с, 70010000000002) == 367520
    assert db.пара(с, 70010000000001) == 367520


def test_пара_не_найдена_трогает_только_свою_строку(с):
    db.записать_пару(с, 70010000000001, 367520)
    db.записать_пару(с, 70010000000001, None)
    assert db.пара(с, 367520) == 70010000000001
    assert db.пара(с, 70010000000001) is None
    assert db.пара_сверена(с, 70010000000001)


def test_обратная_строка_дописывается_к_ненайденной(с):
    db.записать_пару(с, 367520, None)
    db.записать_пару(с, 70010000000001, 367520)
    assert db.пара(с, 367520) == 70010000000001


def test_история_начинается_с_цены_до_перехода(с):
    # Цена, отмеченная до версии 4: строка в `prices` есть, в `price_history` — нет.
    с.execute("INSERT INTO prices VALUES (367520, 46500, 0, '2026-10-01T10:00:00+00:00')")
    db.отметить_цену(с, 367520, 46500, 0, валюта="RUB")
    минимум = db.минимум_наблюдений(с, 367520)
    assert минимум is not None and (минимум["цена"], минимум["валюта"]) == (46500, "RUB")


def test_минимум_когда_последний_раз(с, monkeypatch):
    номер = 70010000000001
    дни = iter(["2026-10-01T10:00:00+00:00", "2026-10-02T10:00:00+00:00", "2026-10-03T10:00:00+00:00"])
    часы = {"сейчас": ""}
    monkeypatch.setattr(db, "_сейчас", lambda: часы["сейчас"])
    for цена in (1999, 3999, 1999):
        часы["сейчас"] = next(дни)
        db.отметить_цену(с, номер, цена, 0, валюта="USD")
    минимум = db.минимум_наблюдений(с, номер)
    assert минимум["когда"] == "2026-10-03T10:00:00+00:00"
    assert минимум["с"] == "2026-10-01T10:00:00+00:00"


def test_игра_с_парой(с):
    _пара_hollow_knight(с)
    запись = db.игра(с, 367520)
    assert запись["магазины"] == ["steam", "eshop"] and запись["пара"] == 70010000000001
    assert запись["пара_цена"] == 1999 and запись["пара_адрес"] == АДРЕС_ESHOP
    запись = db.игра(с, 70010000000002)
    assert запись["магазины"] == ["eshop"] and запись["пара"] is None


def test_выключатель_eshop(с):
    assert db.eshop_включён(с) is True
    db.включить_eshop(с, False)
    assert db.eshop_включён(с) is False
    assert db.отметка(с, "eshop") == "выкл"
    db.включить_eshop(с, True)
    assert db.eshop_включён(с) is True
    assert db.отметка(с, "eshop") == "вкл"


# ---- регионы магазинов: страны и пересборка ----

ESHOP_США = 70010000003208
ESHOP_ЕВРОПА = 70010000003207


def test_страны_по_умолчанию_нет(с):
    assert db.страна_steam(с) is None
    assert db.страна_eshop(с) is None


def test_задать_страны(с):
    db.задать_страны(с, steam="KZ", eshop="US")
    assert db.страна_steam(с) == "KZ" and db.страна_eshop(с) == "US"
    assert db.отметка(с, "страна_steam") == "KZ" and db.отметка(с, "страна_eshop") == "US"
    # `None` — страну этого магазина не трогать.
    db.задать_страны(с, eshop="DE")
    assert db.страна_steam(с) == "KZ" and db.страна_eshop(с) == "DE"
    db.задать_страны(с, steam="TR")
    assert db.страна_steam(с) == "TR" and db.страна_eshop(с) == "DE"


@pytest.mark.parametrize("код", ["kz", "K", "KAZ", "", "K1", "ЖК", " KZ", "Kz"])
def test_страна_не_код_iso(с, код):
    db.задать_страны(с, steam="KZ", eshop="US")
    with pytest.raises(ValueError):
        db.задать_страны(с, steam=код)
    with pytest.raises(ValueError):
        db.задать_страны(с, eshop=код)
    # Неверный код одного магазина не записывает и верный код другого.
    with pytest.raises(ValueError):
        db.задать_страны(с, steam="DE", eshop=код)
    assert db.страна_steam(с) == "KZ" and db.страна_eshop(с) == "US"


def _игра_во_всех_таблицах(с, номер: int, имя: str) -> None:
    db.запомнить_игру(с, Игра(номер=номер, имя=имя, цена=1000, валюта="USD"))
    db.предложить(с, номер, f"причина {имя}")
    db.в_календарь(с, номер, f"ждёт {имя}")
    db.отметить_цену(с, номер, 1000, 0, валюта="USD")
    db.запомнить_отказ(с, номер, "отпечаток")
    db.уже_играл(с, номер, True)
    db.поставить_отметку(с, f"выход_сообщён:{номер}", "2026-10-19")


def _строки(с, таблица: str, номер: int) -> int:
    return с.execute(f"SELECT count(*) FROM {таблица} WHERE номер = ?", (номер,)).fetchone()[0]


ТАБЛИЦЫ_ИГРЫ = (
    "games", "suggestions", "calendar", "prices", "price_history", "pairs", "refusals", "played",
)


def test_удалить_игру(с):
    _игра_во_всех_таблицах(с, 367520, "Hollow Knight")
    _игра_во_всех_таблицах(с, 400, "Portal")
    db.записать_пару(с, ESHOP_США, 367520)
    db.обновить_библиотеку(с, [])
    db.обновить_список_steam(с, [367520])
    db.удалить_игру(с, 367520)
    for таблица in ТАБЛИЦЫ_ИГРЫ:
        assert _строки(с, таблица, 367520) == 0, таблица
    # Пара — с обеих сторон: строки игры eShop, указывающей на удалённую, тоже нет.
    assert not db.пара_сверена(с, ESHOP_США)
    assert db.отметка(с, "выход_сообщён:367520") is None
    # Список желаемого Steam — данные Steam, не трогается.
    assert db.список_steam(с) == {367520}
    # Другая игра цела.
    for таблица in ТАБЛИЦЫ_ИГРЫ:
        if таблица != "pairs":
            assert _строки(с, таблица, 400) == 1, таблица
    assert db.игра(с, 400)["имя"] == "Portal"
    assert db.отметка(с, "выход_сообщён:400") == "2026-10-19"


def test_удалить_игру_одной_транзакцией(с):
    _игра_во_всех_таблицах(с, 367520, "Hollow Knight")
    с.execute("CREATE TEMP TRIGGER сбой BEFORE DELETE ON played BEGIN SELECT RAISE(ABORT, 'сбой'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.удалить_игру(с, 367520)
    for таблица in ("games", "suggestions", "calendar", "prices", "price_history", "refusals"):
        assert _строки(с, таблица, 367520) == 1, таблица


def test_заменить_номер(с):
    _игра_во_всех_таблицах(с, ESHOP_США, "Hollow Knight")
    db.оценить(с, ESHOP_США, True, "нравятся метроидвании")
    db.записать_пару(с, ESHOP_США, 367520)
    db.заменить_номер(с, ESHOP_США, ESHOP_ЕВРОПА)
    # Переносятся: предложение с оценкой, календарь, «уже играл», отметка о выходе.
    предложение = с.execute(
        "SELECT причина, оценка, причина_оценки FROM suggestions WHERE номер = ?", (ESHOP_ЕВРОПА,)
    ).fetchone()
    assert tuple(предложение) == ("причина Hollow Knight", 1, "нравятся метроидвании")
    assert с.execute(
        "SELECT причина FROM calendar WHERE номер = ?", (ESHOP_ЕВРОПА,)
    ).fetchone()[0] == "ждёт Hollow Knight"
    assert _строки(с, "played", ESHOP_ЕВРОПА) == 1
    assert db.отметка(с, f"выход_сообщён:{ESHOP_ЕВРОПА}") == "2026-10-19"
    assert db.отметка(с, f"выход_сообщён:{ESHOP_США}") is None
    # Прежний номер исчез отовсюду.
    for таблица in ТАБЛИЦЫ_ИГРЫ:
        assert _строки(с, таблица, ESHOP_США) == 0, таблица
    # Карточка, цены, история, пары и отказы — данные зоны: на новый номер не переносятся.
    for таблица in ("games", "prices", "price_history", "pairs", "refusals"):
        assert _строки(с, таблица, ESHOP_ЕВРОПА) == 0, таблица
    # Пара с обеих сторон: игра Steam снова ищет пару.
    assert not db.пара_сверена(с, 367520)


def test_заменить_номер_не_затирает_строки_нового(с):
    db.предложить(с, ESHOP_США, "старое предложение")
    db.в_календарь(с, ESHOP_США, "старый календарь")
    db.уже_играл(с, ESHOP_США, False)
    db.поставить_отметку(с, f"выход_сообщён:{ESHOP_США}", "старая")
    db.предложить(с, ESHOP_ЕВРОПА, "новое предложение")
    db.оценить(с, ESHOP_ЕВРОПА, True, "новая оценка")
    db.в_календарь(с, ESHOP_ЕВРОПА, "новый календарь")
    db.уже_играл(с, ESHOP_ЕВРОПА, True)
    db.поставить_отметку(с, f"выход_сообщён:{ESHOP_ЕВРОПА}", "новая")
    db.заменить_номер(с, ESHOP_США, ESHOP_ЕВРОПА)
    assert tuple(с.execute(
        "SELECT причина, оценка FROM suggestions WHERE номер = ?", (ESHOP_ЕВРОПА,)
    ).fetchone()) == ("новое предложение", 1)
    assert с.execute(
        "SELECT причина FROM calendar WHERE номер = ?", (ESHOP_ЕВРОПА,)
    ).fetchone()[0] == "новый календарь"
    assert с.execute(
        "SELECT понравилась FROM played WHERE номер = ?", (ESHOP_ЕВРОПА,)
    ).fetchone()[0] == 1
    assert db.отметка(с, f"выход_сообщён:{ESHOP_ЕВРОПА}") == "новая"
    for таблица in ("suggestions", "calendar", "played"):
        assert _строки(с, таблица, ESHOP_США) == 0, таблица
    assert db.отметка(с, f"выход_сообщён:{ESHOP_США}") is None


def test_заменить_номер_на_себя_ничего_не_меняет(с):
    _игра_во_всех_таблицах(с, ESHOP_США, "Hollow Knight")
    db.заменить_номер(с, ESHOP_США, ESHOP_США)
    for таблица in ("games", "suggestions", "calendar", "prices", "price_history", "refusals", "played"):
        assert _строки(с, таблица, ESHOP_США) == 1, таблица


def test_заменить_номер_одной_транзакцией(с):
    _игра_во_всех_таблицах(с, ESHOP_США, "Hollow Knight")
    с.execute("CREATE TEMP TRIGGER сбой BEFORE DELETE ON refusals BEGIN SELECT RAISE(ABORT, 'сбой'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.заменить_номер(с, ESHOP_США, ESHOP_ЕВРОПА)
    for таблица in ("suggestions", "calendar", "played", "games", "prices"):
        assert _строки(с, таблица, ESHOP_США) == 1, таблица
        assert _строки(с, таблица, ESHOP_ЕВРОПА) == 0, таблица


def test_сбросить_цены(с):
    for номер in (367520, ESHOP_США):
        db.отметить_цену(с, номер, 1000, 0, валюта="USD")
    db.сбросить_цены(с, eshop=False)
    assert _строки(с, "prices", 367520) == 0 and _строки(с, "price_history", 367520) == 0
    assert _строки(с, "prices", ESHOP_США) == 1 and _строки(с, "price_history", ESHOP_США) == 1
    db.отметить_цену(с, 367520, 1000, 0, валюта="KZT")
    db.сбросить_цены(с, eshop=True)
    assert _строки(с, "prices", ESHOP_США) == 0 and _строки(с, "price_history", ESHOP_США) == 0
    assert _строки(с, "prices", 367520) == 1 and _строки(с, "price_history", 367520) == 1


def test_номера_в_работе(с):
    db.предложить(с, 10, "предложение")
    db.оценить(с, 20, False, "минус")
    db.в_календарь(с, 30, "календарь")
    db.в_календарь(с, 10, "и там, и там")
    db.предложить(с, 40, "купленная")
    db.в_календарь(с, ESHOP_США, "eShop")

    class Своя:
        номер, имя, часы, часы_2нед, последний_запуск = 40, "Купленная", 1, 0, None

    db.обновить_библиотеку(с, [Своя()])
    db.обновить_список_steam(с, [50])
    db.запомнить_игру(с, Игра(номер=60, имя="Только карточка"))
    # «Уже играл» — решение человека: игра тоже переезжает при смене зоны.
    db.уже_играл(с, 70, False)
    assert db.номера_в_работе(с) == [10, 20, 30, 70, ESHOP_США]


def test_сбросить_пары(с):
    db.записать_пару(с, 367520, ESHOP_США)
    db.записать_пару(с, 400, None)
    db.отметить_цену(с, 367520, 1000, 0, валюта="USD")
    db.сбросить_пары(с)
    assert с.execute("SELECT count(*) FROM pairs").fetchone()[0] == 0
    assert _строки(с, "prices", 367520) == 1


def _снимок(с) -> dict:
    return {
        т: sorted(tuple(р) for р in с.execute(f"SELECT * FROM {т}").fetchall())
        for т in ("games", "suggestions", "calendar", "played", "prices", "price_history",
                  "pairs", "refusals", "marks", "feed")
    }


def _пересборка(т) -> None:
    т.запомнить_игру(Игра(номер=ESHOP_ЕВРОПА, имя="Hollow Knight", цена=900, валюта="EUR"))
    т.заменить_номер(ESHOP_США, ESHOP_ЕВРОПА)
    т.удалить_игру(400)
    т.сбросить_цены(eshop=False)
    т.сбросить_пары()
    т.записать_пару(367520, None)
    т.задать_страны(steam="KZ", eshop="DE")
    т.в_ленту("итог", "Регион сменён")


def test_транзакция_применяет_всё(с):
    _игра_во_всех_таблицах(с, ESHOP_США, "Hollow Knight")
    _игра_во_всех_таблицах(с, 400, "Portal")
    db.запомнить_игру(с, Игра(номер=367520, имя="Hollow Knight"))
    db.отметить_цену(с, 367520, 1000, 0, валюта="RUB")
    db.записать_пару(с, 367520, ESHOP_США)
    with db.транзакция(с) as т:
        _пересборка(т)
    assert db.игра(с, ESHOP_ЕВРОПА)["валюта"] == "EUR" and db.игра(с, ESHOP_США) is None
    assert _строки(с, "suggestions", ESHOP_ЕВРОПА) == 1 and db.игра(с, 400) is None
    assert _строки(с, "prices", 367520) == 0
    assert db.пара_сверена(с, 367520) and db.пара(с, 367520) is None
    assert (db.страна_steam(с), db.страна_eshop(с)) == ("KZ", "DE")
    assert db.лента(с, 1)[0]["текст"] == "Регион сменён"


def test_транзакция_откатывается_целиком(с):
    _игра_во_всех_таблицах(с, ESHOP_США, "Hollow Knight")
    _игра_во_всех_таблицах(с, 400, "Portal")
    db.запомнить_игру(с, Игра(номер=367520, имя="Hollow Knight"))
    db.записать_пару(с, 367520, ESHOP_США)
    db.задать_страны(с, steam="RU", eshop="US")
    до = _снимок(с)
    with pytest.raises(RuntimeError, match="обрыв"):
        with db.транзакция(с) as т:
            _пересборка(т)
            raise RuntimeError("обрыв")
    assert _снимок(с) == до
    assert not с.in_transaction


def test_транзакция_не_пишет_после_выхода(с):
    with db.транзакция(с) as т:
        pass
    with pytest.raises(RuntimeError):
        т.удалить_игру(400)
