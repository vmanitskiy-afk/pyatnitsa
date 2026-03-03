"""
Навык shortener — сокращение наименований организаций по корпоративному стандарту.
Портирован из OpenClaw shortener.mjs.

Алгоритм:
1. Извлечь и сократить ОПФ (организационно-правовую форму)
2. Очистить «тело» названия от кавычек и пунктуации
3. ИП → формат Фамилия И.О.
4. Применить словарь сокращений (longest-first)
5. Коммерческие ОПФ → обернуть в «кавычки»
6. Добавить МО (если не содержится в названии)
7. Добавить регион-префикс
8. Собрать: [Регион] [Название] [МО], ОПФ

Требует JSON-словари: abbreviations, opf, regions.
Встроены inline, но можно подгрузить из data/*.json.
"""
from __future__ import annotations

import json
import os
import re
import structlog

from pyatnitsa.skills.base import BaseSkill, LLMTool

logger = structlog.get_logger("skill.shortener")

# ── Embedded dictionaries ────────────────────────────────
# Эти словари портированы из OpenClaw data/*.json.
# Можно заменить на загрузку из файлов через SHORTENER_DATA_DIR.

OPF_DICT: dict[str, str] = {
    "Общество с ограниченной ответственностью": "ООО",
    "Акционерное общество": "АО",
    "Закрытое акционерное общество": "ЗАО",
    "Публичное акционерное общество": "ПАО",
    "Открытое акционерное общество": "ОАО",
    "Индивидуальный предприниматель": "ИП",
    "Государственное бюджетное учреждение": "ГБУ",
    "Муниципальное бюджетное учреждение": "МБУ",
    "Государственное казённое учреждение": "ГКУ",
    "Муниципальное казённое учреждение": "МКУ",
    "Государственное автономное учреждение": "ГАУ",
    "Муниципальное автономное учреждение": "МАУ",
    "Государственное унитарное предприятие": "ГУП",
    "Муниципальное унитарное предприятие": "МУП",
    "Федеральное государственное бюджетное учреждение": "ФГБУ",
    "Федеральное государственное казённое учреждение": "ФГКУ",
    "Федеральное государственное автономное учреждение": "ФГАУ",
    "Федеральное государственное унитарное предприятие": "ФГУП",
    "Федеральное казённое учреждение": "ФКУ",
    "Некоммерческая организация": "НКО",
    "Автономная некоммерческая организация": "АНО",
    "Частное учреждение": "ЧУ",
    "Товарищество собственников жилья": "ТСЖ",
    "Товарищество собственников недвижимости": "ТСН",
    "Садоводческое некоммерческое товарищество": "СНТ",
    "Общественная организация": "ОО",
    "Государственное бюджетное образовательное учреждение": "ГБОУ",
    "Муниципальное бюджетное образовательное учреждение": "МБОУ",
    "Муниципальное бюджетное дошкольное образовательное учреждение": "МБДОУ",
    "Государственное бюджетное учреждение здравоохранения": "ГБУЗ",
    "Муниципальное бюджетное учреждение здравоохранения": "МБУЗ",
    "Администрация муниципального образования": "Администрация МО",
    "Администрация": "Администрация",
}

# Regions → short prefix (Краснодарский край → КК, etc.)
REGIONS: dict[str, str] = {
    "Краснодарский край": "КК",
    "Ростовская область": "РО",
    "Ставропольский край": "СК",
    "Республика Адыгея": "АД",
    "Республика Крым": "РК",
    "Волгоградская область": "ВО",
    "Астраханская область": "АО",
    "Республика Калмыкия": "КЛМ",
    "Москва": "МСК",
    "Московская область": "МО",
    "Санкт-Петербург": "СПб",
    "Ленинградская область": "ЛО",
    "Свердловская область": "СО",
    "Нижегородская область": "НО",
    "Новосибирская область": "НСО",
    "Республика Татарстан": "РТ",
    "Республика Башкортостан": "РБ",
    "Самарская область": "СМО",
    "Красноярский край": "КрК",
    "Пермский край": "ПК",
    "Воронежская область": "ВОО",
    "Челябинская область": "ЧО",
    "Тюменская область": "ТО",
    "Иркутская область": "ИО",
    "Хабаровский край": "ХК",
    "Приморский край": "ПрК",
}

# Common abbreviations for org names
ABBREVIATIONS: dict[str, str] = {
    "информационных технологий": "ИТ",
    "информационные технологии": "ИТ",
    "информационной безопасности": "ИБ",
    "информационная безопасность": "ИБ",
    "здравоохранения": "здрав.",
    "образования": "образ.",
    "культуры": "культ.",
    "социального обслуживания": "соц. обсл.",
    "социальной защиты": "соц. защиты",
    "физической культуры и спорта": "физ. культ. и спорта",
    "дополнительного образования": "доп. образ.",
    "дошкольного образования": "дошк. образ.",
    "общеобразовательная школа": "СОШ",
    "средняя общеобразовательная школа": "СОШ",
    "детский сад": "д/с",
    "район": "р-н",
    "городской округ": "ГО",
    "муниципальный район": "МР",
    "сельское поселение": "с/п",
    "городское поселение": "г/п",
    "города": "г.",
    "город": "г.",
    "посёлок": "пос.",
    "поселок": "пос.",
    "станица": "ст.",
    "село": "с.",
    "хутор": "х.",
    "деревня": "д.",
}

# Commercial OPF abbreviations → name in quotes
COMMERCIAL_OPF = {"ООО", "АО", "ЗАО", "ПАО", "ОАО", "ЧУ", "ИП"}


class ShortenerSkill(BaseSkill):
    name = "shortener"
    description = "Сокращение наименований организаций по корпоративному стандарту"
    version = "1.0.0"

    def __init__(self):
        super().__init__()
        self._opf: dict[str, str] = {}
        self._opf_keys: list[str] = []
        self._regions: dict[str, str] = {}
        self._abbreviations: dict[str, str] = {}
        self._abbr_keys: list[str] = []

    async def on_load(self):
        data_dir = os.getenv("SHORTENER_DATA_DIR", "")

        # Load from files if available, otherwise use embedded
        self._opf = _load_json(data_dir, "opf.json", OPF_DICT)
        self._regions = _load_json(data_dir, "regions.json", REGIONS)
        self._abbreviations = _load_json(data_dir, "abbreviations.json", ABBREVIATIONS)

        # Sort keys longest-first for greedy matching
        self._opf_keys = sorted(self._opf.keys(), key=len, reverse=True)
        self._abbr_keys = sorted(self._abbreviations.keys(), key=len, reverse=True)
        logger.info("shortener_loaded", opf=len(self._opf), abbr=len(self._abbreviations), regions=len(self._regions))

    async def on_unload(self):
        pass

    def get_tools(self) -> list[LLMTool]:
        return [
            LLMTool("shortener.shorten", "Сократить полное наименование организации до корпоративного СН", {
                "type": "object", "properties": {
                    "full_name": {"type": "string", "description": "Полное наименование (ЕГРЮЛ/устав)"},
                    "region": {"type": "string", "description": "Субъект РФ (Краснодарский край)"},
                    "municipality": {"type": "string", "description": "Муниципальное образование"},
                    "inn": {"type": "string", "description": "ИНН (для контекста)"},
                }, "required": ["full_name"],
            }),
        ]

    async def execute(self, tool_name: str, params: dict) -> str:
        cmd = tool_name.split(".")[-1]
        if cmd == "shorten":
            return self._shorten(params)
        return json.dumps({"error": f"unknown: {tool_name}"})

    def _shorten(self, p: dict) -> str:
        full_name = (p.get("full_name") or "").strip()
        if not full_name:
            return json.dumps({"error": "full_name is required"}, ensure_ascii=False)

        result = self._shorten_org_name(
            full_name=full_name,
            region=p.get("region"),
            municipality=p.get("municipality"),
        )
        return json.dumps({
            "input": full_name,
            "result": result,
            "region": p.get("region"),
            "municipality": p.get("municipality"),
        }, ensure_ascii=False)

    def _shorten_org_name(self, full_name: str, region: str | None = None,
                          municipality: str | None = None) -> str:
        inp = full_name.strip()
        opf_short = None

        # Step 1: Extract and shorten OPF
        for key in self._opf_keys:
            pattern = re.compile(r"^" + re.escape(key) + r"\s*", re.IGNORECASE)
            if pattern.search(inp):
                opf_short = self._opf[key]
                inp = pattern.sub("", inp).strip()
                break

        if not opf_short:
            for key in self._opf_keys:
                pattern = re.compile(re.escape(key), re.IGNORECASE)
                if pattern.search(inp):
                    opf_short = self._opf[key]
                    inp = pattern.sub("", inp).strip()
                    break

        # Step 2: Clean quotes and punctuation
        inp = re.sub(r'^[«"„]', "", inp)
        inp = re.sub(r'[»""\"]$', "", inp).strip()
        inp = re.sub(r'^[\s,.\-—–]+', "", inp)
        inp = re.sub(r'[\s,.\-—–]+$', "", inp).strip()

        raw_name = inp

        # Step 3: IP → Фамилия И.О.
        if opf_short == "ИП":
            short_name = _format_ip(raw_name)
            region_prefix = self._resolve_region(region)
            parts = []
            if region_prefix:
                parts.append(region_prefix)
            parts.append(short_name)
            return f"{' '.join(parts)}, ИП".strip()

        # Step 4: Apply abbreviations (longest-first)
        short_name = raw_name
        for full_text in self._abbr_keys:
            pattern = re.compile(re.escape(full_text), re.IGNORECASE)
            if pattern.search(short_name):
                short_name = pattern.sub(self._abbreviations[full_text], short_name)

        short_name = re.sub(r"\s{2,}", " ", short_name).strip()

        # Step 5: Wrap commercial in quotes
        if opf_short and opf_short in COMMERCIAL_OPF:
            if short_name and not short_name.startswith("«") and not short_name.startswith('"'):
                short_name = f"«{short_name}»"

        # Step 6: Municipality
        muni_part = ""
        if municipality:
            if municipality.lower() not in short_name.lower():
                muni_part = municipality

        # Step 7: Region prefix
        region_prefix = self._resolve_region(region)

        # Step 8: Assemble
        left_parts = []
        if region_prefix:
            left_parts.append(region_prefix)
        if short_name:
            left_parts.append(short_name)
        if muni_part:
            left_parts.append(muni_part)

        left = " ".join(left_parts).strip()
        result = f"{left}, {opf_short}".strip() if opf_short else left
        return result or full_name

    def _resolve_region(self, region: str | None) -> str | None:
        if not region:
            return None
        if region in self._regions:
            return self._regions[region]
        lower = region.lower()
        for key, val in self._regions.items():
            if key.lower() == lower:
                return val
        for key, val in self._regions.items():
            if key.lower() in lower or lower in key.lower():
                return val
        return None


# ── Helpers ──────────────────────────────────────────────

def _format_ip(name: str) -> str:
    """Иванов Иван Иванович → Иванов И.И."""
    parts = re.sub(r'[«»""]', "", name).strip().split()
    if len(parts) >= 3:
        return f"{parts[0]} {parts[1][0]}.{parts[2][0]}."
    if len(parts) == 2:
        return f"{parts[0]} {parts[1][0]}."
    return name


def _load_json(data_dir: str, filename: str, default: dict) -> dict:
    """Загрузить JSON из data_dir если есть, иначе вернуть default."""
    if data_dir:
        path = os.path.join(data_dir, filename)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("shortener_load_failed", file=path, error=str(e))
    return default
