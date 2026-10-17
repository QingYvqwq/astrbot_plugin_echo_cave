"""Message templates for cave rendering."""

from __future__ import annotations

import random
from datetime import datetime

FORWARD_TEMPLATES: tuple[str, ...] = (
    "🌀 回声洞 #{id}\n\n一则回声从时光中飘来——\n📅 {date} · 来自 @{originName}  \n📮 由 @{userName} 投递",
    "🌀 回声洞 #{id}\n\n【记录编号】{id}  \n【创建时间】{date}  \n【原始作者】@{originName}  \n【投递者】@{userName}",
    "🌀 回声洞 #{id}\n\n这声回响最初来自 @{originName}，  \n由 @{userName} 在 {date} 留下。",
    "🌀 回声洞 #{id}\n\n时间的回音在此汇聚。  \n📅 {date}  \n📤 出处：@{originName}  \n📮 封印者：@{userName}",
    "🌀 回声洞 #{id}\n\n📅 {date}  \n👤 来自 @{originName}  \n📮 投递者 @{userName}",
)

MSG_TEMPLATES: tuple[tuple[str, str], ...] = (
    (
        "🌀 回声洞 #{id}\n\n—— 有人留下了声音 ——{textStartNl}",
        "{textEndNl}📅 {date} · 来自 @{originName}\n📮 由 @{userName} 投递",
    ),
    (
        "🌀 回声洞 #{id}\n───────────────{textStartNl}",
        "{textEndNl}───────────────\n📅 {date}\n👤 {originName} · 由 @{userName} 投递",
    ),
    (
        "🌀 回声洞 #{id}\n\n💭「有人在洞壁留下印记」{textStartNl}",
        "{textEndNl}📅 {date}\n📮 {userName} 记录",
    ),
    (
        "🌀 回声洞 #{id}\n\n— 回声开始 —{textStartNl}",
        "{textEndNl}— 回声结束 —\n📅 {date} · @{originName}\n📮 @{userName}",
    ),
    (
        "🌀 回声洞 #{id}{textStartNl}",
        "{textEndNl}📅 {date}\n来自 @{originName}\n由 @{userName} 投递",
    ),
)

PLACEHOLDERS = (
    "id",
    "date",
    "originName",
    "userName",
    "relatedUsers",
    "nl",
    "textStartNl",
    "textEndNl",
)


def render(template: str, data: dict[str, str]) -> str:
    """Replace all supported placeholders without touching unrelated braces."""
    result = template
    for key in PLACEHOLDERS:
        result = result.replace("{" + key + "}", data.get(key, ""))
    return result


def format_date(value: datetime) -> str:
    """Format a date as YYYY/MM/DD in the local timezone."""
    local = value.astimezone()
    return local.strftime("%Y/%m/%d")


def template_data(
    *,
    cave_id: int,
    create_time: datetime,
    origin_name: str,
    user_name: str,
    related_users: str,
    starts_with_text: bool,
    ends_with_text: bool,
) -> dict[str, str]:
    return {
        "id": str(cave_id),
        "date": format_date(create_time),
        "originName": origin_name,
        "userName": user_name,
        "relatedUsers": related_users,
        "nl": "\n",
        "textStartNl": "\n" if starts_with_text else "",
        "textEndNl": "\n" if ends_with_text else "",
    }


def pick_forward_template(rng: random.Random | None = None) -> str:
    generator = rng or random
    return generator.choice(FORWARD_TEMPLATES)


def pick_msg_template(rng: random.Random | None = None) -> tuple[str, str]:
    generator = rng or random
    return generator.choice(MSG_TEMPLATES)
