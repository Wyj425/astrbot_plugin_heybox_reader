"""Resolve incoming and delivered outgoing shares within one conversation."""
import asyncio
import copy
import inspect
import json
import re
import time
from datetime import datetime, timezone, timedelta

LEGACY_MARKER = '【本次内容读取结果】'
FOLLOWUP = re.compile(r'帖子|笔记|视频|配图|评论|正文|字幕|上面|刚才|那篇|这篇|B站|小红书|小黑盒|能看到哪些信息|有意思在|哪里有意思|哪儿有意思|为什么推荐|为啥推荐|不是我发的|不是你发的', re.I)


def actor(text):
    if re.search(r'不是我(?:发|转|分享)', text):
        return 'bot'
    if re.search(r'不是你(?:发|转|分享)', text):
        return 'user'
    bot = re.search(r'你(?:刚才|刚刚|之前|昨天|前面|刚|给我|给我的|转给我|发给我|给我发的|给我转的)*[的所]*(?:发|转发|分享|推荐|推送)', text)
    user = re.search(r'我(?:刚才|刚刚|之前|昨天|前面|刚|给你|给你的|发给你|给你发的|给你转的)*[的所]*(?:发|转发|分享|推荐)', text)
    return 'bot' if bot and not user else 'user' if user and not bot else None


def event_time(event):
    value = getattr(event.message_obj, 'timestamp', None)
    if value is None:
        raw = getattr(event.message_obj, 'raw_message', None)
        value = raw.get('time') if isinstance(raw, dict) else None
    if hasattr(value, 'timestamp'):
        value = value.timestamp()
    try:
        return float(value) if value is not None else time.time()
    except (TypeError, ValueError):
        return time.time()


def attach_temp(req, text):
    if getattr(req, 'extra_user_content_parts', None) is None:
        req.extra_user_content_parts = []
    try:
        from astrbot.core.agent.message import TextPart
        part = TextPart(text=text).mark_as_temp()
    except ImportError:  # Standalone tests; supported AstrBot versions use TextPart.
        part = {'type': 'text', 'text': text, '_no_save': True}
    req.extra_user_content_parts.append(part)


def clean_legacy_context(req):
    """Clean a request copy, never rewrite the user's saved conversations."""
    if not getattr(req, 'contexts', None):
        return
    cleaned = copy.deepcopy(req.contexts)
    for message in cleaned:
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str) and LEGACY_MARKER in content:
            message['content'] = content.split(LEGACY_MARKER, 1)[0].rstrip()
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get('text'), str) and LEGACY_MARKER in part['text']:
                    part['text'] = part['text'].split(LEGACY_MARKER, 1)[0].rstrip()
    req.contexts = cleaned


def share_service(reader):
    try:
        meta = reader.context.get_registered_star('community_digest')
        service = getattr(meta, 'star_cls', None) if meta else None
        if meta and getattr(meta, 'activated', False) and getattr(service, 'sharing_context_version', None) == 1:
            return service
    except (AttributeError, RuntimeError):
        pass
    return None


async def call_api(method, **kwargs):
    result = method(**kwargs)
    return await asyncio.wait_for(result, 3) if inspect.isawaitable(result) else result


async def outgoing(reader, event, before, message_id=None):
    service = share_service(reader)
    if service is None:
        return []
    try:
        if message_id is not None:
            return await call_api(service.shared_content_context, umo=event.unified_msg_origin,
                                  before=before, message_id=message_id, limit=8)
        if not event.get_extra('_shared_reference_snapshot_captured'):
            records = await call_api(service.shared_content_context, umo=event.unified_msg_origin,
                                     before=before, limit=8)
            event.set_extra('_shared_reference_snapshot', copy.deepcopy(records))
            event.set_extra('_shared_reference_snapshot_captured', True)
        return copy.deepcopy(event.get_extra('_shared_reference_snapshot') or [])
    except Exception:
        return []


def context_user_refs(req, before, parse):
    result = []
    for index, message in enumerate(getattr(req, 'contexts', None) or []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(x.get('text', '') for x in content if isinstance(x, dict))
        if not isinstance(content, str):
            continue
        content = content.split(LEGACY_MARKER, 1)[0]
        stamp = before - 7*86400 + index/1000 + 1
        match = re.search(r'Current datetime:\s*(\d{4}-\d\d-\d\d[ T]\d\d:\d\d)', content)
        if match:
            stamp = datetime.fromisoformat(match[1]).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
        for ref in parse([{'type': 'text', 'data': {'text': content}}]):
            result.append(dict(ref, origin='user', at=stamp))
    return result


def choose(candidates, text, wanted, owner, before):
    limit = 7*86400 if owner or wanted else 86400
    values = [x for x in candidates if x.get('source') and x.get('url')
              and 0 <= before-float(x.get('at', before)) <= limit
              and (not wanted or x.get('source') == wanted)
              and (not owner or x.get('origin', 'user') == owner)]
    # An explicit title fragment, such as Apex/T1, can select an earlier share.
    names = [x.lower() for x in re.findall(r'[A-Za-z][A-Za-z0-9]{1,}', text)
             if x.lower() not in ('bilibili',)]
    named = [x for x in values if any(name in str(x.get('title', '')).lower() for name in names)]
    return copy.deepcopy(max(named or values, key=lambda x: float(x.get('at', 0)))) if values else None


async def select_reference(reader, event, req, parse, source_name):
    raw = event.message_obj.raw_message
    segments = raw.get('message', []) if hasattr(raw, 'get') else []
    refs = parse(segments)
    if refs:
        return dict(refs[0], origin='user', at=event.get_extra('_platform_received_at') or event_time(event))
    if any(s.get('type') == 'json' for s in segments if isinstance(s, dict)):
        return None
    text = event.message_str or ''
    owner = actor(text)
    quoted = any(s.get('type') == 'reply' for s in segments if isinstance(s, dict))
    if not quoted and not owner and not FOLLOWUP.search(text):
        return None
    before = event.get_extra('_platform_received_at') or event_time(event)
    wanted = source_name(text)
    for segment in segments:
        if segment.get('type') != 'reply':
            continue
        mid = str((segment.get('data') or {}).get('id', ''))
        known = await outgoing(reader, event, before, mid)
        if known:
            return known[0]
        try:
            message = await asyncio.wait_for(event.bot.call_action('get_msg', message_id=int(mid)), 6)
            refs = parse(message.get('message', []))
            if refs:
                sender = str(message.get('user_id', (message.get('sender') or {}).get('user_id', '')))
                return dict(refs[0], origin='bot' if sender == str(event.get_self_id()) else 'user', at=before)
        except Exception:
            pass
        return {'source': None, 'needs_clarification': True, 'reason': '无法确认被引用卡片的内容，请简短确认，不沿用其他帖子。'}
    candidates = await outgoing(reader, event, before)
    entry = (event.get_extra('_platform_reference_snapshot') if event.get_extra('_platform_reference_snapshot_captured')
             else reader.recent.get(event.unified_msg_origin))
    if entry:
        candidates.append(dict(entry[1], at=entry[0], origin=entry[1].get('origin', 'user')))
    candidates += context_user_refs(req, before, parse) if req is not None else []
    selected = choose(candidates, text, wanted, owner, before)
    if selected:
        return selected
    if hasattr(event, 'bot'):
        group = event.get_group_id()
        try:
            result = await asyncio.wait_for(event.bot.call_action('get_group_msg_history' if group else 'get_friend_msg_history',
                **({'group_id': int(group)} if group else {'user_id': int(event.get_sender_id())}), count=60), 8)
            messages = sorted(result.get('messages', []), key=lambda x: float(x.get('time') or 0))
            current = str(getattr(event.message_obj, 'message_id', ''))
            index = next((i for i, m in enumerate(messages) if current and str(m.get('message_id')) == current), None)
            if index is not None:
                messages = messages[:index]
            for message in messages:
                sender = str(message.get('user_id', (message.get('sender') or {}).get('user_id', '')))
                for ref in parse(message.get('message', [])):
                    candidates.append(dict(ref, at=float(message.get('time') or 0),
                        origin='bot' if sender == str(event.get_self_id()) else 'user'))
            selected = choose(candidates, text, wanted, owner, before)
            if selected:
                return selected
        except Exception:
            pass
    return {'source': None, 'needs_clarification': True,
            'reason': '没有找到当前会话中符合这次指代的帖子。请简短确认链接或标题，不把用户的旧分享当作你转发的内容。'}


async def selected_evidence(reader, event, ref):
    service = share_service(reader)
    if service and ref.get('origin') == 'bot' and ref.get('card_id'):
        try:
            result = await call_api(service.shared_content_evidence, umo=event.unified_msg_origin,
                                    card_id=ref['card_id'], before=event.get_extra('_platform_received_at') or event_time(event))
            if result and result.get('source') == reader.source and (result.get('body') or result.get('evidence')):
                if not re.search(r'配图|图片|看图', event.message_str or ''):
                    if not result.get('body'):
                        result['body'] = result.get('evidence', '')
                    return result
        except Exception:
            pass
    return await reader.read(ref['url'])


def selection_note(ref):
    return ('\n【本轮选中的帖子】' + json.dumps({k: ref.get(k) for k in
            ('source', 'title', 'url', 'origin', 'at', 'comment')}, ensure_ascii=False)
            + '\norigin=bot 表示你转发给当前会话，origin=user 表示对方分享给你；转发者不是原帖作者。'
              '按本次选中资料回答；历史回答若认错对象，应直接纠正，不坚持旧帖。\n')
