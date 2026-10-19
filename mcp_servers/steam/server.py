"""Сервер MCP `steam` игрового агента: магазины Steam и Nintendo eShop страны пользователя и
хранилище агента — только чтение.

Писать модели не дано ничего. Желаемое, оценки и календарь пишут кнопки человека в вебе и
задания по расписанию: плюс и минус остаются словом человека, а не модели.

Способ передачи — stdio: сервер поднимает `myharness` процессом из `.mcp.json` каталога агента.
Хранилище — `games_db` (`$GAMES_DATA_DIR/games.sqlite`), открывается только на чтение. Из него
же — страны магазинов, выбранные человеком: инструменты Steam ходят в магазин страны Steam,
`eshop_find` и `eshop_game` — в eShop страны eShop. Страны нет — ошибка инструмента, а не
магазин наугад.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

import eshop
import eshop_store as es
import games_db
import steam_store as ss

srv = MCPServer(
    "steam",
    instructions=(
        "Магазины Steam и Nintendo eShop страны пользователя и хранилище игрового агента. "
        "steam_tags — метки магазина с номерами; "
        "steam_search — номера игр по метке; steam_find — номер игры по названию; "
        "steam_game — сведения об игре с ценой и оценкой "
        "пользователей; eshop_find — игра в Nintendo eShop страны пользователя по названию; "
        "eshop_game — сведения об игре eShop по её номеру из eshop_find или списков агента. "
        "list_* — предложения, желаемое, календарь выпусков, библиотека Steam "
        "с часами игры и лента агента. "
        "Все инструменты только читают."
    ),
)


def _прочесть(чтение, нет: Any = None) -> Any:
    """Чтение хранилища. Файла ещё нет — `нет` (по умолчанию пустой список): агент не запускал
    заданий, это не сбой."""
    try:
        с = games_db.открыть(только_чтение=True)
    except FileNotFoundError:
        return [] if нет is None else нет
    try:
        return чтение(с)
    finally:
        с.close()


def _страна_steam() -> str:
    страна = _прочесть(games_db.страна_steam, нет="")
    if not страна:
        raise ToolError("страна Steam не выбрана — укажите в настройках агента")
    return страна


def _регион_eshop() -> eshop.Регион:
    страна = _прочесть(games_db.страна_eshop, нет="")
    if not страна:
        raise ToolError("страна eShop не выбрана — укажите в настройках агента")
    регион = eshop.регион(страна)
    if регион is None:
        raise ToolError(f"eShop страны {страна} не поддержан — выберите другую в настройках агента")
    return регион


def _список(чтение) -> list[dict]:
    """Список из хранилища с учётом выключателя eShop: выключатель и список читаются одним
    открытием."""
    записи, включён = _прочесть(
        lambda с: (чтение(с), games_db.eshop_включён(с)), нет=([], True)
    )
    return записи if включён else games_db.без_eshop(записи)


def _цена(игра: ss.Игра) -> float | None:
    """Цена в валюте страны магазина (`валюта`): магазин отдаёт её в сотых долях."""
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
        "цена": _цена(игра),
        "валюта": игра.валюта,
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
    страна = _страна_steam()
    try:
        async with ss.клиент() as к:
            номера = await ss.найти(к, tag, будущие=upcoming, страна=страна)
    except ss.ОшибкаМагазина as exc:
        raise ToolError(str(exc)) from exc
    return {"номера": номера}


@srv.tool()
async def steam_find(
    name: Annotated[str, Field(description="Название игры, как его написал человек")],
) -> dict[str, Any]:
    """Поиск магазина по названию: номера и точные имена в порядке выдачи. Лёгкие опечатки
    магазин прощает, не все: пусто — исправь написание и ищи снова."""
    страна = _страна_steam()
    try:
        async with ss.клиент() as к:
            найдено = await ss.найти_по_названию(к, name, страна=страна)
    except ss.ОшибкаМагазина as exc:
        raise ToolError(str(exc)) from exc
    return {"игры": [{"номер": н, "имя": и} for н, и in найдено]}


@srv.tool()
async def steam_game(
    appid: Annotated[int, Field(description="Номер игры Steam")],
) -> dict[str, Any]:
    """Сведения об игре: жанры, дата выхода, цена и скидка в валюте страны, оценка пользователей
    Steam."""
    if es.это_eshop(appid):
        raise ToolError(f"номер {appid} — eShop: сведения — eshop_game")
    страна = _страна_steam()
    try:
        async with ss.клиент() as к:
            игра = await ss.игра(к, appid, страна=страна)
    except ss.ОшибкаМагазина as exc:
        raise ToolError(str(exc)) from exc
    if игра is None:
        raise ToolError(f"магазин не знает игры {appid}")
    return _игра_словарём(игра)


@srv.tool()
async def eshop_find(
    name: Annotated[str, Field(description="Название игры, как его написал человек")],
) -> dict[str, Any]:
    """Поиск в Nintendo eShop страны пользователя по названию: номер, точное имя, платформы, дата,
    цена в валюте страны. Номер — номер eShop, не Steam: в steam_game его не передавать. Пусто —
    исправь написание и ищи снова."""
    if not _прочесть(games_db.eshop_включён, нет=True):
        raise ToolError("eShop выключен в настройках агента")
    регион = _регион_eshop()
    try:
        async with eshop.клиент() as к:
            игры = await eshop.найти_по_названию(к, регион, name)
    except ss.ОшибкаМагазина as exc:
        raise ToolError(str(exc)) from exc
    return {"игры": [
        {
            "номер": и.номер,
            "имя": и.имя,
            "платформы": list(и.платформы),
            "выход": и.дата_текст,
            "вышла": и.вышла,
            "цена": _цена(и),
            "валюта": и.валюта,
            "скидка_процентов": и.скидка,
            "жанры": list(и.жанры),
            "описание": и.описание,
            "ссылка": и.ссылка,
        }
        for и in игры
    ]}


@srv.tool()
async def eshop_game(
    nsuid: Annotated[int, Field(description="Номер игры Nintendo eShop (nsuid)")],
) -> dict[str, Any]:
    """Сведения об игре Nintendo eShop по номеру (eShop страны пользователя): жанры, дата, цена,
    описание, оценка игроков Metacritic, если агент её знает. Номер — eShop, не Steam: Steam —
    steam_game."""
    if not es.это_eshop(nsuid):
        raise ToolError(f"номер {nsuid} — не eShop: сведения об игре Steam — steam_game")
    запись, включён = _прочесть(
        lambda с: (games_db.игра(с, nsuid), games_db.eshop_включён(с)), нет=(None, True)
    )
    if not включён:
        raise ToolError("eShop выключен в настройках агента")
    if запись is None:
        raise ToolError(f"игра eShop {nsuid} неизвестна агенту")
    объект = запись.get("объект")
    if not объект:
        raise ToolError(f"у игры eShop {nsuid} нет записи каталога — сведения взять неоткуда")
    регион = _регион_eshop()
    try:
        async with eshop.клиент() as к:
            игра = await eshop.игра(к, регион, объект)
    except ss.ОшибкаМагазина as exc:
        raise ToolError(str(exc)) from exc
    if игра is None:
        raise ToolError(f"игры eShop {nsuid} нет в каталоге eShop страны {регион.страна}")
    # У игр eShop оценка — игроков Metacritic: свежая запись каталога её не несёт, она
    # в хранилище, куда её кладут задания.
    отзывы = игра.отзывы if игра.отзывы.всего > 0 else ss.Отзывы(
        запись.get("отзывы_сводка") or "", запись.get("отзывы_положительных") or 0,
        запись.get("отзывы_всего") or 0,
    )
    return {
        **_игра_словарём(игра),
        "платформы": list(игра.платформы),
        "оценка_пользователей": f"игроки Metacritic: {отзывы.текст()}",
    }


@srv.tool()
def list_suggestions() -> dict[str, Any]:
    """Предложения агента и оценки человека: плюс, минус или ещё не оценено."""
    return {"предложения": _список(games_db.предложения)}


@srv.tool()
def list_wishlist() -> dict[str, Any]:
    """Список желаемого с последней известной ценой и скидкой."""
    return {"желаемое": _список(games_db.желаемое)}


@srv.tool()
def list_calendar() -> dict[str, Any]:
    """Календарь будущих выпусков, отобранных под вкус человека."""
    return {"календарь": _список(games_db.календарь)}


@srv.tool()
def list_library() -> dict[str, Any]:
    """Библиотека человека. `библиотека` — игры Steam по убыванию часов: всего, за две недели,
    последний запуск. `switch` — игры eShop, отмеченные кнопкой «Есть на Switch» (куплены на
    Switch): имя, ссылка и когда отмечено, по имени; часов у них нет. eShop выключен — `switch`
    пуст."""
    библиотека, switch, включён = _прочесть(
        lambda с: (games_db.библиотека(с), games_db.библиотека_switch(с), games_db.eshop_включён(с)),
        нет=([], [], True),
    )
    return {
        "библиотека": библиотека,
        "switch": [
            {"имя": з["имя"], "ссылка": з["ссылка"], "отмечено": з["отмечено"]} for з in switch
        ] if включён else [],
    }


@srv.tool()
def list_feed() -> dict[str, Any]:
    """Лента агента: скидки, итоги заданий и сбои, новые первыми."""
    return {"лента": _прочесть(games_db.лента)}


if __name__ == "__main__":
    srv.run()
