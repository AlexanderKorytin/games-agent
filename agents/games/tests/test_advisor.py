import json

import advisor


def test_каталог_агента_разрешения_и_сервер(tmp_path):
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
