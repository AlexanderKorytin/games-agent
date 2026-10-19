"""Магазин Nintendo eShop: запросы и разбор ответов.

Открытого API у eShop нет. Модуль ходит туда же, куда сайт nintendo.com: каталог — индекс
Algolia сайта, цены со скидками — `api.ec.nintendo.com`, полное описание — страница товара
(`__NEXT_DATA__`). Сайт сменит устройство — разбор вернёт пусто или `ОшибкаМагазина`.

Страну выбирает пользователь (`регион`). Зона «америки» — этот модуль: у каждой страны свой индекс
Algolia (`store_game_en_us`, `store_game_es_mx`…) с ценой, жанрами и метками `availability` на
языке индекса и адресом страницы с локалью страны (`/es-mx/store/products/…`); номер игры общий
на зону. Зона «европа» — свой каталог (`eshop_eu`), здесь только её страны. Цены хранятся в
сотых долях валюты страны, как копейки у Steam. Algolia отдаёт не больше 1000 записей на запрос
(замер: страница 20 по 50 пуста) — это предел магазина, глубже источник eShop не идёт.

Ключ Algolia — открытый ключ поиска со страницы nintendo.com: правило ключей его не касается.

Состояния у модуля нет, на диск он не пишет.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

import httpx2

import steam_store as ss
from steam_store import Игра

САЙТ = "https://www.nintendo.com"
ALGOLIA = "https://U3B6GR4UA3-dsn.algolia.net/1/indexes"
# Открытый ключ поиска со страницы nintendo.com: не тайна, правило ключей его не касается.
ЗАГОЛОВКИ_КАТАЛОГА = {"X-Algolia-Application-Id": "U3B6GR4UA3",
                      "X-Algolia-API-Key": "a29c6927638bfd8cee23993e51e721c9"}
ЦЕНЫ = "https://api.ec.nintendo.com/v1/price"
ОБЛОЖКИ = "https://assets.nintendo.com/image/upload/ar_16:9,c_lpad,w_460/b_white/f_auto/q_auto/"
НАЧАЛО_НОМЕРОВ = 70_000_000_000_000
ДЛИНА_СТРАНИЦЫ = 50  # как у Steam
ЦЕН_ЗА_ЗАПРОС = 50  # замер: 51 номер — ошибка 4004 «Over ids limit number»
# Код категории не переводится, имя переводится («Juegos», «Jogos»): фильтр — по коду.
_ИГРЫ = "topLevelCategoryCode:GAMES"


@dataclass(frozen=True)
class Регион:
    страна: str  # "US", ISO 3166-1
    зона: str  # "америки" | "европа"
    каталог: str  # индекс Algolia Америк либо язык поиска Европы


_АМЕРИКИ = {"US": "en_us", "CA": "en_ca", "MX": "es_mx", "BR": "pt_br", "AR": "es_ar",
            "CL": "es_cl", "CO": "es_co", "PE": "es_pe"}
_ЕВРОПА = {
    **dict.fromkeys(("GB", "IE", "MT", "CY", "AU", "NZ"), "en"),
    **dict.fromkeys(("DE", "AT", "CH"), "de"),
    **dict.fromkeys(("FR", "BE", "LU"), "fr"),
    "IT": "it", "ES": "es", "NL": "nl", "PT": "pt", "ZA": "za",
    # Своего языка в поиске Европы у этих стран нет: каталог английский.
    **dict.fromkeys(("FI", "GR", "HU", "RO", "BG", "PL", "CZ", "SE", "NO", "DK", "SK", "SI", "HR",
                     "LT", "LV", "EE"), "en"),
}
# Страны, где у eShop есть цена в `api.ec` и открытый каталог (замер 2026-10-19). Россия отвечает
# ценой закрытого магазина, Япония — свой каталог без порядка популярности, Гонконг, Тайвань и
# Корея — без открытого каталога: их нет.
СТРАНЫ: dict[str, Регион] = {
    **{с: Регион(с, "америки", "store_game_" + к) for с, к in _АМЕРИКИ.items()},
    **{с: Регион(с, "европа", я) for с, я in _ЕВРОПА.items()},
}
# Валюта цены индекса страны Америк (`api.ec` отвечает той же).
ВАЛЮТЫ = {"US": "USD", "CA": "CAD", "MX": "MXN", "BR": "BRL", "AR": "ARS", "CL": "CLP",
          "CO": "COP", "PE": "PEN"}


def регион(страна: str) -> Регион | None:
    """Регион eShop страны; `None` — у eShop нет там магазина с открытым каталогом."""
    return СТРАНЫ.get(страна)


@dataclass(frozen=True)
class Метки:
    """Значения `availability` и `topLevelFilters` — на языке индекса (замер фасетов 2026-10-19)."""
    скоро: str
    предзаказ: str
    вышла: str
    обновление: str  # пакет обновления до издания Switch 2


_МЕТКИ = {
    "en": Метки("Coming soon", "Pre-order", "Available now", "Upgrade pack"),
    "es": Метки("Próximos lanzamientos", "Reserva", "Disponible ya", "Pase de mejora"),
    # «disponivel» без знака — так в индексе.
    "pt": Метки("Em breve", "Reserve", "Já disponivel", "Pacote de melhoria"),
}


def _америки(регион: Регион) -> None:
    """Каталог Algolia — только у Америк: регион иной зоны ушёл бы молча не в тот индекс."""
    if регион.зона != "америки":
        raise ValueError(f"регион {регион.страна} зоны «{регион.зона}» — не каталог Америк")


def метки(регион: Регион) -> Метки:
    """Метки индекса Америк по его языку: `store_game_es_mx` → испанские."""
    _америки(регион)
    return _МЕТКИ[регион.каталог.removeprefix("store_game_").split("_")[0]]


def _индекс(регион: Регион) -> str:
    _америки(регион)
    return f"{ALGOLIA}/{регион.каталог}"


@dataclass(frozen=True)
class Цена:
    цена: int  # сотые доли валюты страны
    цена_без_скидки: int
    скидка: int  # проценты
    валюта: str
    # Последний день скидки `ГГГГ-ММ-ДД` (по UTC); без скидки — "".
    до: str


_ДАННЫЕ_СТРАНИЦЫ = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_ЗНАКИ = re.compile(r"[™®©]")
# Хвост издания платформы: «Nintendo Switch 2 Edition» и переводы индексов Америк — «Edição»,
# «Edición», «Édition Nintendo Switch 2». Издание игры («Edição Completa») не трогается.
_ИЗДАНИЕ = re.compile(
    r"\s*[-–—:]?\s*(?:Nintendo Switch(?:\s*2)?\s*Edition|(?:Edição|Edición|Édition)\s+Nintendo Switch(?:\s*2)?)\s*$",
    re.IGNORECASE,
)


def это_eshop(номер: int) -> bool:
    """nsuid — 14 цифр с `7001…`, номер Steam — до 8: одна колонка `номер` держит оба."""
    return номер >= НАЧАЛО_НОМЕРОВ


def чистое_имя(название: str) -> str:
    """Имя без знаков марки и хвоста «– Nintendo Switch 2 Edition»: так его ищут в Steam."""
    return " ".join(_ИЗДАНИЕ.sub("", _ЗНАКИ.sub("", название)).split())


def сравнимое(название: str) -> str:
    """Ключ точного сравнения имён двух магазинов: без надстрочных знаков, регистра и пунктуации."""
    текст = unicodedata.normalize("NFKD", чистое_имя(название))
    текст = "".join(з for з in текст if unicodedata.category(з) != "Mn").casefold()
    return re.sub(r"[^a-z0-9]+", " ", текст).strip()


def _центы(сумма: object) -> int | None:
    return round(float(сумма) * 100) if isinstance(сумма, (int, float)) else None


def _проценты(доля: object) -> int:
    return round(доля) if isinstance(доля, (int, float)) else 0


def разобрать_запись(данные: object, регион: Регион) -> Игра | None:
    """Игра из записи индекса страны `регион`. `None` — не игра: дополнение, пакет обновления до
    издания Switch 2 либо запись без nsuid — их не купить отдельной игрой."""
    з = данные
    if not isinstance(з, dict):
        return None
    м = метки(регион)
    if з.get("dlcType") or м.обновление in (з.get("topLevelFilters") or []) or з.get("isUpgrade"):
        return None
    nsuid = з.get("nsuid")
    if not isinstance(nsuid, str) or not nsuid.isdigit():
        return None
    if з.get("releaseDateDisplay"):
        # Неточная дата («2027», «Spring 2027»): `releaseDate` тогда — заглушка конца срока.
        дата, дата_текст = None, str(з["releaseDateDisplay"])
    elif isinstance(з.get("releaseDate"), str) and з["releaseDate"]:
        try:
            дата_текст = з["releaseDate"][:10]
            дата = date.fromisoformat(дата_текст)
        except ValueError:
            # Заглушка вроде «TBD»: показать как пришла, точного дня нет.
            дата, дата_текст = None, з["releaseDate"]
    else:
        дата, дата_текст = None, ""
    цена = з.get("price") if isinstance(з.get("price"), dict) else {}
    итог = _центы(цена.get("finalPrice"))
    картинка = з.get("productImage")
    адрес = з.get("url")
    return Игра(
        номер=int(nsuid),
        имя=str(з.get("title") or nsuid),
        описание=str(з.get("description") or ""),
        жанры=tuple(str(ж) for ж in з.get("gameGenreLabels") or []),
        дата_текст=дата_текст,
        дата=дата,
        вышла=not ({м.скоро, м.предзаказ} & set(з.get("availability") or [])),
        цена=итог,
        цена_без_скидки=_центы(цена.get("regPrice")),
        скидка=_проценты(цена.get("percentOff")),
        валюта=ВАЛЮТЫ[регион.страна] if итог is not None else "",
        бесплатна=итог == 0,
        обложка=ОБЛОЖКИ + картинка if isinstance(картинка, str) and картинка else "",
        адрес=САЙТ + адрес if isinstance(адрес, str) and адрес else "",
        объект=str(з.get("objectID") or ""),
        платформы=tuple(str(п) for п in з.get("corePlatforms") or []),
    )


def разобрать_выдачу(данные: object, регион: Регион) -> list[Игра]:
    """Игры страницы каталога в порядке выдачи, без дополнений и пакетов обновления."""
    if not isinstance(данные, dict) or not isinstance(данные.get("hits"), list):
        raise ss.ОшибкаМагазина("ответ каталога eShop без hits")
    return [и for з in данные["hits"] if (и := разобрать_запись(з, регион)) is not None]


def _сумма(поле: object) -> int | None:
    if not isinstance(поле, dict):
        return None
    try:
        return round(float(поле["raw_value"]) * 100)
    except (KeyError, TypeError, ValueError):
        return None


def разобрать_цены(данные: object) -> dict[int, Цена]:
    """Цены `api.ec` по номерам. Номера без цены (`not_found`, `unreleased`) в словарь не идут."""
    if isinstance(данные, dict) and "error" in данные:
        ошибка = данные["error"]
        текст = f"{ошибка.get('code')} {ошибка.get('message')}" if isinstance(ошибка, dict) else ошибка
        raise ss.ОшибкаМагазина(f"цены eShop: {текст}")
    if not isinstance(данные, dict) or not isinstance(данные.get("prices"), list):
        raise ss.ОшибкаМагазина("ответ цен eShop без prices")
    итог: dict[int, Цена] = {}
    for з in данные["prices"]:
        if not isinstance(з, dict) or з.get("sales_status") not in ("onsale", "preorder"):
            continue
        номер = з.get("title_id")
        if isinstance(номер, str) and номер.isdigit():
            номер = int(номер)
        обычная = _сумма(з.get("regular_price"))
        if обычная is None or not isinstance(номер, int) or isinstance(номер, bool):
            continue
        валюта = str(з["regular_price"].get("currency") or "")
        скидочная = _сумма(з.get("discount_price"))
        if скидочная is None:
            итог[номер] = Цена(обычная, обычная, 0, валюта, "")
            continue
        скидка = round(100 * (1 - скидочная / обычная)) if обычная else 0
        до = str(з["discount_price"].get("end_datetime") or "")[:10]
        итог[номер] = Цена(скидочная, обычная, скидка, валюта, до)
    return итог


def разобрать_описание(html: str) -> str:
    """Полное описание из данных страницы товара; не нашлось — "". В каталоге описание в одну
    фразу, суду местной модели его мало."""
    найдено = _ДАННЫЕ_СТРАНИЦЫ.search(html)
    if not найдено:
        return ""
    try:
        данные = json.loads(найдено.group(1))
        состояние = данные["props"]["pageProps"]["initialApolloState"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return ""
    if not isinstance(состояние, dict):
        return ""
    for ключ, запись in состояние.items():
        if ключ.startswith("Product:") and isinstance(запись, dict) and isinstance(запись.get("description"), str):
            return ss._без_разметки(запись["description"])
    return ""


def клиент() -> httpx2.AsyncClient:
    # Без `base_url`: хозяев три — Algolia, `api.ec` и сайт.
    return httpx2.AsyncClient(timeout=ss.ОЖИДАНИЕ, headers={"User-Agent": ss.ПРЕДСТАВЛЕНИЕ})


async def _запрос(
    к: httpx2.AsyncClient,
    метод: str,
    адрес: str,
    *,
    доводы: dict[str, str] | None = None,
    тело: object = None,
    заголовки: dict[str, str] | None = None,
) -> httpx2.Response:
    """Ответ 200 или 404 — вызывающему. 429 и 5xx — повтор после паузы, политика та же, что у
    Steam (`ss._взять`); исчерпаны либо иной код — `ОшибкаМагазина`."""
    последняя = ""
    for пауза in (*ss.ПАУЗЫ, None):
        try:
            ответ = await к.request(метод, адрес, params=доводы, json=тело, headers=заголовки)
        except httpx2.HTTPError as exc:
            последняя = f"нет связи с магазином: {type(exc).__name__}"
        else:
            if ответ.status_code in (200, 404):
                return ответ
            последняя = f"магазин ответил HTTP {ответ.status_code}"
            if ответ.status_code != 429 and ответ.status_code < 500:
                # Повтора не будет: начало тела несёт причину отказа (`api.ec` кладёт её в `error`).
                последняя += f": {ответ.text[:300]}"
                break
            пауза = ss._ждать(ответ, пауза)
        if пауза is None:
            break
        await asyncio.sleep(пауза)
    raise ss.ОшибкаМагазина(f"{адрес}: {последняя}")


def _json(ответ: httpx2.Response) -> object:
    if ответ.status_code != 200:
        raise ss.ОшибкаМагазина(f"{ответ.request.url}: магазин ответил HTTP {ответ.status_code}")
    try:
        return ответ.json()
    except (json.JSONDecodeError, ValueError):
        raise ss.ОшибкаМагазина(f"{ответ.request.url}: магазин вернул не JSON") from None


async def _каталог(к: httpx2.AsyncClient, регион: Регион, тело: dict[str, object]) -> list[Игра]:
    ответ = await _запрос(к, "POST", f"{_индекс(регион)}/query", тело=тело, заголовки=ЗАГОЛОВКИ_КАТАЛОГА)
    return разобрать_выдачу(_json(ответ), регион)


async def витрина(к: httpx2.AsyncClient, регион: Регион, *, будущие: bool, страница: int) -> list[Игра]:
    """Одна страница каталога страны — до 50 игр в порядке Algolia. Пустой список — выдача
    кончилась (либо упёрлась в предел 1000 записей). Будущие — «скоро» и предзаказ."""
    м = метки(регион)
    отбор = (f'(availability:"{м.скоро}" OR availability:"{м.предзаказ}")' if будущие
             else f'availability:"{м.вышла}"')
    return await _каталог(к, регион, {
        "query": "", "filters": f"{_ИГРЫ} AND {отбор}",
        "hitsPerPage": ДЛИНА_СТРАНИЦЫ, "page": страница,
    })


async def найти_по_названию(к: httpx2.AsyncClient, регион: Регион, название: str) -> list[Игра]:
    """Поиск каталога страны по названию, до 10 записей; опечатки Algolia прощает сама."""
    return await _каталог(к, регион, {"query": название, "hitsPerPage": 10})


async def запись(к: httpx2.AsyncClient, регион: Регион, объект: str) -> Игра | None:
    """Запись каталога страны по коду `objectID`. `None` — кода нет в каталоге либо запись не
    игра."""
    ответ = await _запрос(к, "GET", f"{_индекс(регион)}/{объект}", заголовки=ЗАГОЛОВКИ_КАТАЛОГА)
    if ответ.status_code == 404:
        return None
    return разобрать_запись(_json(ответ), регион)


async def описание(к: httpx2.AsyncClient, регион: Регион, адрес: str) -> str:
    """Полное описание со страницы товара; страницы нет — "". Локаль страны уже в адресе из
    индекса (`/es-mx/…`), страница несёт описание на её языке; `регион` — общий договор зон."""
    ответ = await _запрос(к, "GET", адрес)
    return "" if ответ.status_code == 404 else разобрать_описание(ответ.text)


async def цены(к: httpx2.AsyncClient, регион: Регион, номера: list[int]) -> dict[int, Цена]:
    """Цены страны со скидками и сроком конца скидки, по 50 номеров за запрос."""
    итог: dict[int, Цена] = {}
    for начало in range(0, len(номера), ЦЕН_ЗА_ЗАПРОС):
        часть = номера[начало:начало + ЦЕН_ЗА_ЗАПРОС]
        ответ = await _запрос(к, "GET", ЦЕНЫ, доводы={
            # `lang` на цену не влияет (замер: MX с en и es — одно и то же).
            "country": регион.страна, "lang": "en", "ids": ",".join(str(н) for н in часть),
        })
        if ответ.status_code == 404:
            # `api.ec` отвечает 404 и на ошибку запроса (замер: страна KZ), причина — в `error`.
            try:
                данные = ответ.json()
            except (json.JSONDecodeError, ValueError):
                данные = None
            if isinstance(данные, dict) and "error" in данные:
                разобрать_цены(данные)
        итог.update(разобрать_цены(_json(ответ)))
    return итог


async def игра(к: httpx2.AsyncClient, регион: Регион, объект: str) -> Игра | None:
    """Запись каталога страны с полным описанием страницы товара вместо короткого."""
    найдена = await запись(к, регион, объект)
    if найдена is None or not найдена.адрес:
        return найдена
    полное = await описание(к, регион, найдена.адрес)
    return dataclasses.replace(найдена, описание=полное) if полное else найдена


async def найти_в_steam(к_steam: httpx2.AsyncClient, игра: Игра, *, страна_steam: str) -> int | None:
    """Номер той же игры в Steam страны `страна_steam`; `None` — точного совпадения имён нет.
    Поиск по-английски: русские имена Steam ломают сравнение с английскими именами eShop.
    Совпадение только точное: ложное слияние двух игр хуже двух плашек."""
    ключ = сравнимое(игра.имя)
    if not ключ:
        # Имя без латиницы и цифр: пустой ключ совпал бы с любой такой же игрой.
        return None
    for номер, имя in await ss.найти_по_названию(
        к_steam, чистое_имя(игра.имя), язык="english", страна=страна_steam
    ):
        if сравнимое(имя) == ключ:
            return номер
    return None


async def найти_в_eshop(к: httpx2.AsyncClient, регион: Регион, игра: Игра) -> Игра | None:
    """Та же игра в eShop страны — первое точное совпадение имён в выдаче каталога. Обычное издание и
    «Nintendo Switch 2 Edition» сравниваются одинаково; первое в выдаче — то, что Algolia
    ставит выше."""
    ключ = сравнимое(игра.имя)
    if not ключ:
        return None
    for найдена in await найти_по_названию(к, регион, чистое_имя(игра.имя)):
        if сравнимое(найдена.имя) == ключ:
            return найдена
    return None
