"""AstrBot platform adaptation helpers.

Public AstrBot APIs are preferred.  OneBot-only capabilities (message IDs for
recall, forward lookup, poke targets) are used through the adapter's ``bot``
object with capability checks and graceful fallbacks.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.core.message.message_event_result import MessageChain

ONE_BOT_PLATFORMS = {"aiocqhttp", "onebot", "napcat"}
SPECIAL_FORWARD_USER_ID = "1094950020"
MAX_FORWARD_DEPTH = 5
MAX_FORWARD_NODES = 300

_CQ_AT_RE = re.compile(r'<at\s+id="([^"]+)"\s*/>')
_AT_TEXT_RE = re.compile(r"@[^\s()]*\((\d+)\)")
_DIGITS_RE = re.compile(r"^\d+$")


def platform_name(event: Any) -> str:
    try:
        return str(event.get_platform_name())
    except Exception:
        return ""


def is_onebot(event: Any) -> bool:
    return platform_name(event) in ONE_BOT_PLATFORMS and getattr(event, "bot", None) is not None


def group_id(event: Any) -> str:
    try:
        value = event.get_group_id()
    except Exception:
        value = ""
    return str(value or "")


def is_group(event: Any) -> bool:
    return bool(group_id(event))


def sender_id(event: Any) -> str:
    try:
        return str(event.get_sender_id())
    except Exception:
        return ""


def sender_name(event: Any) -> str:
    try:
        return str(event.get_sender_name())
    except Exception:
        return ""


def self_id(event: Any) -> str:
    try:
        return str(event.get_self_id())
    except Exception:
        return ""


def platform_id(event: Any) -> str:
    try:
        return str(event.get_platform_id())
    except Exception:
        return platform_name(event)


def message_id(event: Any) -> str:
    obj = getattr(event, "message_obj", None)
    return str(getattr(obj, "message_id", "") or "")


def event_messages(event: Any) -> list[Any]:
    try:
        messages = event.get_messages()
    except Exception:
        messages = getattr(getattr(event, "message_obj", None), "message", []) or []
    return list(messages or [])


def get_reply(event: Any) -> Any | None:
    for component in event_messages(event):
        if isinstance(component, Comp.Reply):
            return component
    return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _component_name(component: Any) -> str:
    type_value = getattr(component, "type", "")
    return str(getattr(type_value, "value", type_value) or "").lower()


def _media_data(component: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    file_value = getattr(component, "file_", None)
    if file_value is None:
        file_value = getattr(component, "file", None)
    if file_value:
        data["file"] = str(file_value)
    url_value = getattr(component, "url", None)
    if url_value:
        data["url"] = str(url_value)
    path_value = getattr(component, "path", None)
    if path_value:
        data["path"] = str(path_value)
    if isinstance(component, Comp.File):
        data["name"] = component.name or "file"
        data.pop("file", None)
        if component.file_:
            data["file"] = component.file_
        if component.url:
            data["url"] = component.url
    return data


async def component_to_cq(component: Any) -> dict[str, Any]:
    """Convert one AstrBot message component to a CQ-style segment dict."""
    if isinstance(component, Comp.Plain):
        return {"type": "text", "data": {"text": component.text}}
    if isinstance(component, Comp.AtAll):
        return {"type": "at", "data": {"qq": "all"}}
    if isinstance(component, Comp.At):
        data = {"qq": str(component.qq)}
        if component.name:
            data["name"] = component.name
        return {"type": "at", "data": data}
    if isinstance(component, Comp.Face):
        return {"type": "face", "data": {"id": str(component.id)}}
    if isinstance(component, Comp.Image):
        return {"type": "image", "data": _media_data(component)}
    if isinstance(component, Comp.Record):
        return {"type": "record", "data": _media_data(component)}
    if isinstance(component, Comp.Video):
        return {"type": "video", "data": _media_data(component)}
    if isinstance(component, Comp.File):
        return {"type": "file", "data": _media_data(component)}
    if isinstance(component, Comp.Reply):
        return {"type": "reply", "data": {"id": str(component.id)}}
    if isinstance(component, Comp.Node):
        children = []
        for child in component.content:
            children.extend(await components_to_cq([child]))
        return {
            "type": "node",
            "data": {
                "user_id": str(component.uin or "0"),
                "nickname": component.name or "",
                "content": children,
            },
        }
    if isinstance(component, Comp.Nodes):
        # Represented by the caller as multiple nodes; cannot fit in one dict.
        raise TypeError("Nodes must be expanded by components_to_cq")
    if isinstance(component, Comp.Forward):
        return {"type": "forward", "data": {"id": str(component.id)}}
    if isinstance(component, Comp.Poke):
        target = component.target_id() or ""
        return {"type": "poke", "data": {"id": str(target)}}
    if isinstance(component, Comp.Json):
        return {"type": "json", "data": {"data": _json_safe(component.data)}}
    component_type = _component_name(component)
    data = {}
    for attr in ("url", "title", "content", "id", "qq", "text"):
        value = getattr(component, attr, None)
        if value not in (None, ""):
            data[attr] = _json_safe(value)
    return {"type": component_type or "unknown", "data": data}


async def components_to_cq(components: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for component in components:
        if isinstance(component, Comp.Nodes):
            for node in component.nodes:
                result.extend(await components_to_cq([node]))
            continue
        result.append(await component_to_cq(component))
    return result


def raw_segment_to_cq(segment: dict[str, Any]) -> dict[str, Any]:
    """Normalize an OneBot raw segment; preserve extra fields for fidelity."""
    segment_type = str(segment.get("type") or "unknown")
    data = segment.get("data")
    if not isinstance(data, dict):
        data = {}
    data = _json_safe(data)
    if segment_type == "node":
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [{"type": "text", "data": {"text": content}}]
    if segment_type == "at" and "qq" not in data:
        data["qq"] = data.get("id", "")
    return {"type": segment_type, "data": data}


async def get_quoted_segments(event: Any, reply: Any) -> list[dict[str, Any]]:
    """Obtain the quoted message as CQ segments."""
    chain = getattr(reply, "chain", None) or []
    if chain:
        return await components_to_cq(list(chain))

    bot = getattr(event, "bot", None)
    reply_id = getattr(reply, "id", None)
    if bot is None or reply_id in (None, ""):
        return []
    routing = {"self_id": self_id(event)} if self_id(event) else {}
    try:
        message_id_value = int(reply_id) if str(reply_id).isdigit() else str(reply_id)
        result = await bot.call_action("get_msg", message_id=message_id_value, **routing)
    except Exception:
        return []
    return await raw_message_to_cq(result)


async def raw_message_to_cq(raw: Any) -> list[dict[str, Any]]:
    """Convert an OneBot message payload (string or segment array) to CQ."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        if "message" in raw:
            raw = raw["message"]
        elif "messages" in raw:
            raw = raw["messages"]
        elif "content" in raw:
            raw = raw["content"]
    if isinstance(raw, str):
        return [{"type": "text", "data": {"text": raw}}] if raw else []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for segment in raw:
        if isinstance(segment, dict):
            result.append(raw_segment_to_cq(segment))
    return result


def _extract_forward_messages(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("messages", "message", "content"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _extract_forward_messages(value)
                if nested:
                    return nested
        data = payload.get("data")
        if isinstance(data, (dict, list)):
            return _extract_forward_messages(data)
    return []


async def fetch_forward_messages(event: Any, forward_id: str) -> list[Any]:
    bot = getattr(event, "bot", None)
    if bot is None:
        return []
    routing = {"self_id": self_id(event)} if self_id(event) else {}
    id_value = int(forward_id) if str(forward_id).isdigit() else str(forward_id)
    attempts = (
        {"id": id_value},
        {"message_id": id_value},
        {"id": str(id_value)},
        {"message_id": str(id_value)},
    )
    for params in attempts:
        try:
            result = await bot.call_action("get_forward_msg", **params, **routing)
        except Exception:
            continue
        messages = _extract_forward_messages(result)
        if messages:
            return messages
    return []


def _forward_sender(message: Any) -> tuple[str, str]:
    if not isinstance(message, dict):
        return "", ""
    sender = message.get("sender")
    if isinstance(sender, dict):
        user_id = sender.get("user_id") or sender.get("uin") or sender.get("id") or ""
        nickname = sender.get("nickname") or sender.get("card") or ""
        if user_id:
            return str(user_id), str(nickname)
    user_id = message.get("user_id") or message.get("uin") or ""
    nickname = message.get("nickname") or message.get("name") or ""
    return str(user_id), str(nickname)


def _forward_content(message: Any) -> Any:
    if not isinstance(message, dict):
        return message
    for key in ("message", "content"):
        value = message.get(key)
        if value is not None:
            return value
    return []


async def _lookup_member_id_by_nickname(event: Any, nickname: str) -> str | None:
    members = await get_group_members(event)
    if not members:
        return None
    matches = [
        member
        for member in members
        if (member.get("nickname") or member.get("card") or "") == nickname
    ]
    if len(matches) == 1:
        return str(matches[0]["user_id"])
    return None


async def normalize_forward_messages(
    event: Any,
    raw_messages: list[Any],
    *,
    depth: int = 0,
    node_counter: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Convert ``get_forward_msg`` payloads to recursively stored node segments."""
    if depth > MAX_FORWARD_DEPTH:
        return []
    counter = node_counter if node_counter is not None else [0]
    nodes: list[dict[str, Any]] = []
    for message in raw_messages:
        if counter[0] >= MAX_FORWARD_NODES:
            break
        user_id, nickname = _forward_sender(message)
        if user_id == SPECIAL_FORWARD_USER_ID and nickname:
            replacement = await _lookup_member_id_by_nickname(event, nickname)
            if replacement:
                user_id = replacement

        content = _forward_content(message)
        if isinstance(content, str):
            children = [{"type": "text", "data": {"text": content}}] if content else []
        elif isinstance(content, list):
            children = []
            for segment in content:
                if not isinstance(segment, dict):
                    continue
                segment_type = segment.get("type")
                data = segment.get("data") or {}
                if segment_type == "forward":
                    nested_raw = data.get("content")
                    if isinstance(nested_raw, list):
                        children.extend(
                            await normalize_forward_messages(
                                event,
                                nested_raw,
                                depth=depth + 1,
                                node_counter=counter,
                            )
                        )
                    elif data.get("id") not in (None, ""):
                        nested_raw = await fetch_forward_messages(event, str(data["id"]))
                        children.extend(
                            await normalize_forward_messages(
                                event,
                                nested_raw,
                                depth=depth + 1,
                                node_counter=counter,
                            )
                        )
                else:
                    children.append(raw_segment_to_cq(segment))
        else:
            children = []

        nodes.append(
            {
                "type": "node",
                "data": {
                    "user_id": user_id or "0",
                    "nickname": nickname or user_id or "",
                    "content": children,
                },
            }
        )
        counter[0] += 1
    return nodes


async def expand_forward_segments(
    event: Any, segments: list[dict[str, Any]], *, depth: int = 0
) -> list[dict[str, Any]]:
    """Replace ``forward`` placeholder segments with reconstructed nodes."""
    result: list[dict[str, Any]] = []
    for segment in segments:
        segment_type = segment.get("type")
        if segment_type == "forward":
            data = segment.get("data") or {}
            nested = data.get("content")
            if isinstance(nested, list) and nested:
                result.extend(await normalize_forward_messages(event, nested, depth=depth))
            elif data.get("id") not in (None, ""):
                raw = await fetch_forward_messages(event, str(data["id"]))
                result.extend(await normalize_forward_messages(event, raw, depth=depth))
            continue
        if segment_type == "node":
            data = dict(segment.get("data") or {})
            children = data.get("content")
            if isinstance(children, list):
                data["content"] = await expand_forward_segments(event, children, depth=depth + 1)
            result.append({"type": "node", "data": data})
            continue
        result.append(segment)
    return result


def contains_special_user(
    nodes: list[dict[str, Any]], user_id: str = SPECIAL_FORWARD_USER_ID
) -> bool:
    for node in nodes:
        if node.get("type") != "node":
            continue
        data = node.get("data") or {}
        if str(data.get("user_id")) == str(user_id):
            return True
        children = data.get("content")
        if isinstance(children, list) and contains_special_user(children, user_id):
            return True
    return False


def parse_user_ids(event: Any, tokens: list[str]) -> tuple[list[str], str | None]:
    """Parse numeric IDs and @ mentions from command arguments/components.

    Returns:
        ``(ids, error)`` where error is ``"invalid_all_mention"`` for @all.
    """
    ids: list[str] = []
    for component in event_messages(event):
        if isinstance(component, Comp.AtAll):
            return [], "invalid_all_mention"
        if isinstance(component, Comp.At) and str(component.qq) != self_id(event):
            value = str(component.qq)
            if value == "all":
                return [], "invalid_all_mention"
            if value and value not in ids:
                ids.append(value)

    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if token in ("@全体成员", "@all"):
            return [], "invalid_all_mention"
        if token.isdigit():
            if token not in ids:
                ids.append(token)
            continue
        match = _CQ_AT_RE.search(token) or _AT_TEXT_RE.search(token)
        if match:
            value = match.group(1)
            if value == "all":
                return [], "invalid_all_mention"
            if value not in ids:
                ids.append(value)
    return ids, None


def command_arguments(event: Any, command: str) -> list[str]:
    """Return the raw argument tokens after a command name."""
    text = str(getattr(event, "message_str", "") or "")
    pattern = re.compile(r"(?:^|\s)" + re.escape(command) + r"(?=\s|$)")
    match = pattern.search(text)
    if match:
        return text[match.end() :].strip().split()
    # Fallback: message_str may only contain arguments on some adapters.
    pieces = text.strip().split()
    if pieces and pieces[0] == command:
        return pieces[1:]
    return pieces


async def get_group_members(event: Any) -> list[dict[str, Any]] | None:
    """Fetch group members with card/nickname information (cached per event)."""
    if not is_group(event):
        return None
    cached = getattr(event, "_echo_cave_members_cache", None)
    if isinstance(cached, list):
        return cached
    try:
        group = await event.get_group()
    except Exception:
        group = None
    members = getattr(group, "members", None)
    if members:
        result = [
            {
                "user_id": str(member.user_id),
                "nickname": member.nickname or "",
                "card": member.nickname or "",
            }
            for member in members
        ]
        event._echo_cave_members_cache = result
        return result

    bot = getattr(event, "bot", None)
    if bot is None:
        return None
    routing = {"self_id": self_id(event)} if self_id(event) else {}
    gid = group_id(event)
    api_gid: int | str = int(gid) if gid.isdigit() else gid
    try:
        result = await bot.call_action("get_group_member_list", group_id=api_gid, **routing)
    except Exception:
        return None
    if not isinstance(result, list):
        return None
    members = [
        {
            "user_id": str(member.get("user_id")),
            "nickname": str(member.get("nickname") or ""),
            "card": str(member.get("card") or ""),
        }
        for member in result
        if isinstance(member, dict) and member.get("user_id") is not None
    ]
    event._echo_cave_members_cache = members
    return members


async def check_users_in_group(event: Any, user_ids: list[str]) -> bool:
    if not user_ids:
        return True
    members = await get_group_members(event)
    if members is None:
        return False
    member_ids = {member["user_id"] for member in members}
    return all(str(user_id) in member_ids for user_id in user_ids)


async def get_user_name(event: Any, user_id: str) -> str:
    if not user_id:
        return ""
    members = await get_group_members(event)
    if members:
        for member in members:
            if member["user_id"] == str(user_id):
                return member.get("card") or member.get("nickname") or str(user_id)
    bot = getattr(event, "bot", None)
    if bot is not None and is_group(event):
        routing = {"self_id": self_id(event)} if self_id(event) else {}
        gid = group_id(event)
        api_gid: int | str = int(gid) if gid.isdigit() else gid
        try:
            info = await bot.call_action(
                "get_group_member_info",
                group_id=api_gid,
                user_id=int(user_id) if str(user_id).isdigit() else user_id,
                no_cache=False,
                **routing,
            )
        except Exception:
            info = None
        if isinstance(info, dict):
            return str(info.get("card") or info.get("nickname") or info.get("nick") or user_id)
    return str(user_id)


async def send_text(event: Any, text: str) -> None:
    await event.send(event.plain_result(text))


async def send_text_with_id(event: Any, text: str) -> str | None:
    """Send a text message and return its platform message ID when possible."""
    bot = getattr(event, "bot", None)
    if bot is not None and is_onebot(event):
        payload = [{"type": "text", "data": {"text": text}}]
        gid = group_id(event)
        routing = {"self_id": self_id(event)} if self_id(event) else {}
        try:
            if gid:
                api_gid: int | str = int(gid) if gid.isdigit() else gid
                result = await bot.call_action(
                    "send_group_msg",
                    group_id=api_gid,
                    message=payload,
                    **routing,
                )
            else:
                uid = sender_id(event)
                api_uid: int | str = int(uid) if uid.isdigit() else uid
                result = await bot.call_action(
                    "send_private_msg", user_id=api_uid, message=payload, **routing
                )
        except Exception:
            result = None
        if isinstance(result, dict) and result.get("message_id") is not None:
            return str(result["message_id"])
    await send_text(event, text)
    return None


async def delete_messages(event: Any, message_ids: list[str | None]) -> None:
    bot = getattr(event, "bot", None)
    if bot is None or not is_onebot(event):
        return
    routing = {"self_id": self_id(event)} if self_id(event) else {}
    for message_id_value in message_ids:
        if not message_id_value:
            continue
        try:
            await bot.call_action("delete_msg", message_id=message_id_value, **routing)
        except Exception:
            continue


def schedule_message_delete(
    event: Any,
    message_ids: list[str | None],
    delay: float,
    tasks: set[asyncio.Task],
) -> None:
    if not message_ids:
        return

    async def _later() -> None:
        try:
            await asyncio.sleep(delay)
            await delete_messages(event, message_ids)
        except asyncio.CancelledError:
            return

    task = asyncio.create_task(_later())
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def send_chain(event: Any, components: list[Any]) -> None:
    await event.send(MessageChain(components))


async def send_forward_nodes(event: Any, nodes: list[Comp.Node]) -> bool:
    """Send reconstructed nodes as a merged forward message.

    Returns:
        True when a native forward was sent, False when the caller should fall
        back to plain text on platforms without forward support.
    """
    if not nodes:
        return True
    if not is_onebot(event):
        return False
    await event.send(MessageChain([Comp.Nodes(nodes)]))
    return True


async def send_capture_result(event: Any, result: Any) -> None:
    await event.send(result)


CreateComponent = Callable[[dict[str, Any]], Awaitable[Any]]


async def cq_to_components(
    event: Any,
    segments: list[dict[str, Any]],
    resolve_media: Callable[[str, str], Awaitable[dict[str, Any]]],
) -> list[Any]:
    """Convert stored CQ segments into AstrBot components for sending."""
    components: list[Any] = []
    for segment in segments:
        segment_type = segment.get("type")
        data = segment.get("data") or {}
        if segment_type == "text":
            components.append(Comp.Plain(str(data.get("text", ""))))
        elif segment_type == "at":
            qq = str(data.get("qq", ""))
            components.append(
                Comp.AtAll() if qq == "all" else Comp.At(qq=qq, name=data.get("name", ""))
            )
        elif segment_type == "face":
            try:
                components.append(Comp.Face(id=int(data.get("id", 0))))
            except (TypeError, ValueError):
                components.append(Comp.Plain("[表情]"))
        elif segment_type in ("image", "record", "video", "file"):
            reference = data.get("file") or data.get("url")
            if not reference:
                continue
            resolved = await resolve_media(str(reference), segment_type)
            kind = resolved.get("kind", "path")
            value = resolved.get("value", "")
            if segment_type == "image":
                if kind == "base64":
                    components.append(Comp.Image.fromBase64(value))
                elif kind == "url":
                    components.append(Comp.Image.fromURL(value))
                else:
                    components.append(Comp.Image(file=value))
            elif segment_type == "record":
                if kind == "base64":
                    components.append(Comp.Record.fromBase64(value))
                else:
                    components.append(Comp.Record(file=value))
            elif segment_type == "video":
                if kind == "base64":
                    components.append(Comp.Video.fromBase64(value))
                elif kind == "url":
                    components.append(Comp.Video.fromURL(value))
                else:
                    components.append(Comp.Video(file=value))
            else:
                components.append(
                    Comp.File(
                        name=str(data.get("name") or "file"),
                        file=value if kind == "path" else "",
                        url=value if kind == "url" else "",
                    )
                )
        elif segment_type == "node":
            children = data.get("content") or []
            if isinstance(children, str):
                try:
                    children = json.loads(children)
                except json.JSONDecodeError:
                    children = [{"type": "text", "data": {"text": children}}]
            child_components = await cq_to_components(event, children, resolve_media)
            components.append(
                Comp.Node(
                    content=child_components,
                    uin=str(data.get("user_id") or "0"),
                    name=str(data.get("nickname") or ""),
                )
            )
        elif segment_type == "reply":
            value = data.get("id")
            if value not in (None, ""):
                components.append(Comp.Reply(id=value))
        elif segment_type == "poke":
            components.append(Comp.Poke(id=data.get("id", 0)))
        elif segment_type == "forward":
            # Forward placeholders should have been expanded before sending.
            continue
        else:
            text = data.get("text")
            if text:
                components.append(Comp.Plain(str(text)))
    return components
