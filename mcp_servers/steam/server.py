"""Сервер MCP `steam` игрового агента: магазины Steam и Nintendo eShop (США) и хранилище
агента — только чтение.

Писать модели не дано ничего. Желаемое, оценки и календарь пишут кнопки человека в вебе и
задания по расписанию: плюс и минус остаются словом человека, а не модели.

Способ передачи — stdio: сервер поднимает `myharness` процессом из `.mcp.json` каталога агента.
Хранилище — `games_db` (`$GAMES_DATA_DIR/games.sqlite`), открывается только на чтение.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

import eshop_store as es
import games_db
import steam_store as ss

srv = MCPServer(
    "steam",
    instructions=(
        "Магазины Steam и Nintendo eShop (США) и хранилище игрового агента. "
        "steam_tags — метки магазина с номерами; "
        "steam_search — номера игр по метке; steam_find — номер игры по названию; "
        "steam_game — сведения об игре с ценой и оценкой "
        "пользователей; eshop_find — игра Nintendo eShop (США) по названию. "
        "list_* — предложения, желаемое, календарь выпусков, библиотека Steam "
        "с часами игры и лента агента. "
        "Все инструменты только читают."
    ),
)


def _прочесть(чтение) -> Any:
    """Чтение хранилища. Файла ещё нет — пустой итог: агент не запускал заданий, это не сбой."""
    try:
        с = games_db.открыть(только_чтение=True)
    except FileNotFoundError:
        return []
    try:
        return чтение(с)
    finally:
        с.close()


def _цена(игра: ss.Игра) -> float | None:
    """Цена в рублях (Steam) или долларах (eShop): магазин отдаёт её в копейках и центах."""
    return None if игра.цена is None else игра.цена / 100


def _игра_словарём(игра: ss.Игра) -> dict[str, Any]:
    eshop = es.это_eshop(игра.номер)
    return {
        "магазин": "eshop" if eshop else "steam",
        "номер": игра.номер,
        "имя": игра.имя,
        "род": игра.род,
        "жанры": list(игра.жанры),
        "выход": игра.дата_текст,
        "вышла": игра.вышла,
        "цена_usd" if eshop else "цена_руб": _цена(игра),
        "скидка_процентов": игра.скидка,
        "бесплатна": игра.бесплатна,
        "оценка_пользователей": игра.отзывы.текст(),
        "описание": игра.описание,
        "ссылка": игра.ссылка,
    }


@srv.tool()
async def steam_tags() -> dict[str, Any]:
    """Метки магазина Steam с номерами. Критерии вкуса записываются этими номерами."""
    try:
        async with ss.клиент() as к:
            метки = await ss.метки(к)
    except ss.ОшибкаМагазина as exc:
        raise ToolError(str(exc)) from exc
    return {"метки": [{"номер": м.номер, "имя": м.имя} for м in метки]}


@srv.tool()
async def steam_search(
    tag: Annotated[int, Field(description="Номер метки Steam из steam_tags")],
    upcoming: Annotated[bool, Field(description="true — будущие выпуски, false — вышедшие")] = False,
) -> dict[str, Any]:
    """Номера игр первой страницы выдачи магазина по метке (50 игр — умолчание магазина).
    Вышедшие — в порядке соответствия метке, будущие — самые желанные."""
    try:
        async with ss.клиент() as к:
            номера = await ss.найти(к, tag, будущие=upcoming)
    except ss.ОшибкаМагазина as exc:
        raise ToolError(str(exc)) from exc
    return {"номера": номера}


@srv.tool()
async def steam_find(
    name: Annotated[str, Field(description="Название игры, как его написал человек")],
) -> dict[str, Any]:
    """Поиск магазина по названию: номера и точные имена в порядке выдачи. Лёгкие опечатки
    магазин прощает, не все: пусто — исправь написание и ищи снова."""
    try:
        async with ss.клиент() as к:
            найдено = await ss.найти_по_названию(к, name)
    except ss.ОшибкаМагазина as exc:
        raise ToolError(str(exc)) from exc
    return {"игры": [{"номер": н, "имя": и} for н, и in найдено]}


@srv.tool()
async def steam_game(
    appid: Annotated[int, Field(description="Номер игры Steam")],
) -> dict[str, Any]:
    """Сведения об игре: жанры, дата выхода, цена и скидка в рублях, оценка пользователей Steam."""
    try:
        async with ss.клиент() as к:
            игра = await ss.игра(к, appid)
    except ss.ОшибкаМагазина as exc:
        raise ToolError(str(exc)) from exc
    if игра is None:
        raise ToolError(f"магазин не знает игры {appid}")
    return _игра_словарём(игра)


@srv.tool()
async def eshop_find(
    name: Annotated[str, Field(description="Название игры, как его написал человек")],
) -> dict[str, Any]:
    """Поиск Nintendo eShop (США) по названию: номер, точное имя, платформы, дата, цена в
    долларах. Номер — номер eShop, не Steam: в steam_game его не передавать. Пусто — исправь
    написание и ищи снова."""
    try:
        async with es.клиент() as к:
            игры = await es.найти_по_названию(к, name)
    except ss.ОшибкаМагазина as exc:
        raise ToolError(str(exc)) from exc
    return {"игры": [
        {
            "номер": и.номер,
            "имя": и.имя,
            "платформы": list(и.платформы),
            "выход": и.дата_текст,
            "вышла": и.вышла,
            "цена_usd": _цена(и),
            "скидка_процентов": и.скидка,
            "жанры": list(и.жанры),
            "описание": и.описание,
            "ссылка": и.ссылка,
        }
        for и in игры
    ]}


@srv.tool()
def list_suggestions() -> dict[str, Any]:
    """Предложения агента и оценки человека: плюс, минус или ещё не оценено."""
    return {"предложения": _прочесть(games_db.предложения)}


@srv.tool()
def list_wishlist() -> dict[str, Any]:
    """Список желаемого с последней известной ценой и скидкой."""
    return {"желаемое": _прочесть(games_db.желаемое)}


@srv.tool()
def list_calendar() -> dict[str, Any]:
    """Календарь будущих выпусков, отобранных под вкус человека."""
    return {"календарь": _прочесть(games_db.календарь)}


@srv.tool()
def list_library() -> dict[str, Any]:
    """Библиотека Steam человека по убыванию часов игры: всего, за две недели, последний запуск."""
    return {"библиотека": _прочесть(games_db.библиотека)}


@srv.tool()
def list_feed() -> dict[str, Any]:
    """Лента агента: скидки, итоги заданий и сбои, новые первыми."""
    return {"лента": _прочесть(games_db.лента)}


if __name__ == "__main__":
    srv.run()
