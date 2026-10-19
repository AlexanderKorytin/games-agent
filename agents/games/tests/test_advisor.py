import json

import pytest

import advisor


def test_каталог_агента_разрешения_и_сервер(tmp_path):
    import games_db

    с = games_db.открыть()
    games_db.задать_страны(с, steam="KZ", eshop="US")
    с.close()
    advisor.подготовить_каталог(tmp_path)
    mcp = json.loads((tmp_path / ".mcp.json").read_text("utf-8"))
    assert mcp["mcpServers"]["steam"]["env"]["GAMES_DATA_DIR"] == str(tmp_path)
    разрешено = json.loads((tmp_path / ".claude" / "settings.json").read_text("utf-8"))["permissions"]["allow"]
    assert "mcp__steam__steam_find" in разрешено and "mcp__steam__list_library" in разрешено
    # Игры Switch в «Любимых играх»: не нашлась в Steam — поиск в каталоге eShop.
    assert "mcp__steam__eshop_find" in разрешено
    # Писать модели не дано ничего: в разрешениях только чтение.
    assert all(р.split("__")[-1].startswith(("steam_", "eshop_find", "list_")) for р in разрешено)


def test_текст_пересмотра_по_магазину():
    from steam_store import Игра, Отзывы

    отзывы = Отзывы("Очень положительные", 90, 100)
    eshop = advisor.текст_пересмотра(Игра(70010000003208, "Super Mario Odyssey", отзывы=отзывы), "", "минус")
    assert "номер eShop 70010000003208" in eshop
    assert f"оценка игроков Metacritic: {отзывы.текст()}" in eshop
    assert "Switch" in eshop and "без номера" in eshop
    assert "номер Steam" not in eshop and "пользователей Steam" not in eshop
    steam = advisor.текст_пересмотра(Игра(367520, "Hollow Knight", отзывы=отзывы), "надоели метроидвании", "минус")
    assert f"(номер Steam 367520; жанры: не указаны; оценка пользователей Steam: {отзывы.текст()})" in steam
    assert "Причина: надоели метроидвании." in steam
    assert "eShop" not in steam and "Metacritic" not in steam and "Switch" not in steam


def test_выключенный_eshop_без_eshop_find(tmp_path):
    import games_db

    с = games_db.открыть()
    games_db.включить_eshop(с, False)
    с.close()
    advisor.подготовить_каталог(tmp_path)
    разрешено = json.loads((tmp_path / ".claude" / "settings.json").read_text("utf-8"))["permissions"]["allow"]
    assert "mcp__steam__eshop_find" not in разрешено and "mcp__steam__steam_find" in разрешено


def _разрешено(каталог):
    return json.loads((каталог / ".claude" / "settings.json").read_text("utf-8"))["permissions"]["allow"]


@pytest.mark.parametrize("вкл, страна, есть", [
    (True, "US", True), (True, "DE", True), (True, None, False), (True, "RU", False), (False, "US", False),
])
def test_eshop_find_только_с_регионом_eshop(tmp_path, вкл, страна, есть):
    """`eshop_find` разрешён, только если eShop включён, страна выбрана и в ней eShop есть:
    иначе инструмент всё равно ответит ошибкой, а модель тратила бы на него ход."""
    import games_db

    с = games_db.открыть()
    games_db.включить_eshop(с, вкл)
    if страна:
        games_db.задать_страны(с, eshop=страна)
    с.close()
    advisor.подготовить_каталог(tmp_path)
    assert ("mcp__steam__eshop_find" in _разрешено(tmp_path)) is есть


def test_хранилища_нет_без_eshop_find(tmp_path):
    """Хранилища нет — страны eShop нет: `eshop_find` не разрешён."""
    import games_db

    advisor.подготовить_каталог(tmp_path)
    assert "mcp__steam__eshop_find" not in _разрешено(tmp_path)
    assert "mcp__steam__steam_find" in _разрешено(tmp_path)
    assert not games_db.путь().exists()  # разрешения хранилища не создают


@pytest.mark.parametrize("файл", ["версия 3", "пустой"])
def test_старая_схема_без_eshop_find(tmp_path, файл):
    import games_db

    if файл == "версия 3":
        файл_версии_3(games_db.путь())
    else:
        games_db.путь().write_bytes(b"")
    advisor.подготовить_каталог(tmp_path)
    assert "mcp__steam__eshop_find" not in _разрешено(tmp_path)


def файл_версии_3(путь):
    """Файл, каким его оставлял код версии 3: три части схемы и отметка версии 3."""
    import sqlite3

    import games_db

    сырое = sqlite3.connect(путь)
    for часть in (games_db.СХЕМА, games_db.СХЕМА_2, games_db.СХЕМА_3):
        сырое.executescript(часть)
    сырое.execute("INSERT INTO meta (ключ, значение) VALUES ('версия_схемы', '3')")
    сырое.commit()
    сырое.close()
