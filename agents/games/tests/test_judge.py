import pytest

import judge


def test_разбор_ответа():
    assert judge.разобрать('{"подходит": true, "причина": "  пошаговые  бои "}') == judge.Вердикт(True, "пошаговые бои")
    with pytest.raises(ValueError, match="не JSON"):
        judge.разобрать("да, подходит")
    with pytest.raises(ValueError, match="подходит"):
        judge.разобрать('{"причина": "x"}')


def test_запрос_подписывает_магазин_и_оценку(каталоги):
    from myharness.profiles import builtin_default
    from steam_store import Игра, Отзывы

    профиль = builtin_default()
    отзывы = Отзывы("Очень положительные", 90, 100)
    eshop = judge.запрос(каталоги, Игра(70010000003208, "Super Mario Odyssey", отзывы=отзывы), профиль)
    assert "Магазин: Nintendo eShop" in eshop
    assert f"Оценка игроков Metacritic: {отзывы.текст()}" in eshop
    assert "Steam" not in eshop
    steam = judge.запрос(каталоги, Игра(367520, "Hollow Knight", отзывы=отзывы), профиль)
    assert "Магазин: Steam" in steam
    assert f"Оценка пользователей Steam: {отзывы.текст()}" in steam
    assert "Metacritic" not in steam and "eShop" not in steam
