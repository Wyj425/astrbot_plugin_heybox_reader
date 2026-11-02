"""Replay outgoing-card references without any model, account or QQ traffic."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest
import test_followup_routing as fixtures
from test_followup_routing import Event, URLS, card


class SharedFollowups(unittest.IsolatedAsyncioTestCase):
    observe = fixtures.Followups.observe
    enrich = fixtures.Followups.enrich
    seed = fixtures.Followups.seed
    history = fixtures.Followups.history

    def setUp(self):
        fixtures.Followups.setUp(self)
        self.records = []
        self.contents = {}
        def lookup(umo, before=None, message_id=None, limit=8):
            records = [copy.deepcopy(r) for r in self.records if r['umo'] == umo and r['at'] <= before]
            if message_id is not None:
                records = [r for r in records if str(message_id) in r['message_ids']]
            return sorted(records, key=lambda r: r['at'], reverse=True)[:limit]
        def body(umo, card_id, before=None):
            refs = lookup(umo, before, card_id)
            return copy.deepcopy(self.contents.get(refs[0]['card_id'])) if refs else None
        self.provider = SimpleNamespace(sharing_context_version=1, shared_content_context=lookup,
                                        shared_content_evidence=body)
        context = SimpleNamespace(get_registered_star=lambda name: SimpleNamespace(
            activated=True, star_cls=self.provider) if name == 'community_digest' else None)
        for reader in self.readers.values(): reader.context = context

    def pushed(self, title='Apex 密客无人机', mid='-77', at=None, scope='qq:FriendMessage:123'):
        record = {'source': '小黑盒', 'url': URLS['小黑盒'], 'title': title, 'origin': 'bot',
            'at': self.now-.1 if at is None else at, 'card_id': mid, 'message_ids': [mid, mid+'-comment'], 'umo': scope,
            'comment': '这个有点意思，发给你看看。'}
        self.records.append(record)
        self.contents[mid] = {'source': '小黑盒', 'title': title, 'body': title+' 的真实正文',
                             'url': URLS['小黑盒'], 'images': [], 'comments': [], 'evidence_kind': '帖子正文'}
        return record

    async def test_you_sent_selects_bot_share_instead_of_old_user_xhs(self):
        await self.seed('小红书')
        self.pushed()
        e = Event('我说总结一下你发的这个帖子')
        await self.observe(e)
        req = await self.enrich(e)
        self.assertEqual(e.get_extra('_platform_reference_selection')['origin'], 'bot')
        self.assertEqual(e.get_extra('_platform_reference_selection')['source'], '小黑盒')
        for reader in self.readers.values(): reader.read.assert_not_awaited()
        self.assertEqual(req.prompt, e.message_str)
        text = ''.join(p.get('text', '') if isinstance(p, dict) else p.text for p in req.extra_user_content_parts)
        self.assertIn('密客', text)
        self.assertNotIn('小红书 body', text)

    async def test_original_typo_still_follows_latest_delivered_share(self):
        await self.seed('小红书')
        self.pushed()
        e = Event('给我总结一下呢发的这个帖子')
        await self.observe(e); await self.enrich(e)
        self.assertEqual(e.get_extra('_platform_reference_selection')['card_id'], '-77')

    async def test_cross_midnight_why_interesting_uses_new_t1_not_old_xhs(self):
        await self.seed('小红书')
        self.pushed(title='T1 世界赛和 Faker', at=self.now-3*3600)
        # The incoming share is older still; snapshots must compare actual times.
        for reader in self.readers.values():
            stamp, ref = reader.recent['qq:FriendMessage:123']
            reader.recent['qq:FriendMessage:123'] = (self.now-7*3600, ref)
        e = Event('有意思在哪里')
        await self.observe(e); req = await self.enrich(e)
        self.assertEqual(e.get_extra('_platform_reference_selection')['source'], '小黑盒')
        text = ''.join(p.get('text', '') if isinstance(p, dict) else p.text for p in req.extra_user_content_parts)
        self.assertIn('T1 世界赛', text)
        self.assertEqual(e.actions, [])

    async def test_explicit_user_xhs_selects_user_even_after_bot_push(self):
        await self.seed('小红书'); self.pushed()
        e = Event('总结我发的小红书帖子')
        await self.observe(e); await self.enrich(e)
        self.readers['小红书'].read.assert_awaited_once()
        self.assertEqual(e.get_extra('_platform_reference_selection')['origin'], 'user')

    async def test_correction_not_mine_switches_to_bot_card(self):
        await self.seed('小红书'); self.pushed()
        e = Event('不是我发的')
        await self.observe(e); await self.enrich(e)
        self.assertEqual(e.get_extra('_platform_reference_selection')['origin'], 'bot')

    async def test_quoted_outgoing_comment_selects_its_card(self):
        await self.seed('小红书'); self.pushed(mid='-77'); self.pushed('T1 新帖', mid='88', at=self.now-.01)
        e = Event('总结这篇', [{'type': 'reply', 'data': {'id': '-77-comment'}}])
        await self.observe(e); await self.enrich(e)
        self.assertEqual(e.get_extra('_platform_reference_selection')['card_id'], '-77')
        self.assertEqual(e.actions, [])

    async def test_current_link_overrides_even_explicit_you_sent_language(self):
        self.pushed()
        e = Event('你发的这个链接再看看', [{'type':'text','data':{'text':URLS['B站']}}])
        await self.observe(e); await self.enrich(e)
        self.assertEqual(e.get_extra('_platform_reference_selection')['source'], 'B站')

    async def test_future_push_cannot_retarget_queued_question(self):
        self.pushed(at=self.now-.1)
        e = Event('你发的这个帖子')
        e.message_obj.timestamp = self.now
        await self.observe(e)
        self.pushed('T1 后到的新帖', mid='88', at=self.now+1)
        await self.enrich(e)
        self.assertEqual(e.get_extra('_platform_reference_selection')['card_id'], '-77')

    async def test_other_conversation_cannot_access_bot_push(self):
        self.pushed()
        e = Event('你发的这个帖子', scope='qq:FriendMessage:456')
        await self.observe(e); await self.enrich(e)
        self.assertIsNone(e.get_extra('_platform_reference_selection')['source'])
        for reader in self.readers.values(): reader.read.assert_not_awaited()

    async def test_you_sent_does_not_silently_fall_back_to_user_post(self):
        await self.seed('小红书')
        e = Event('你发的这个帖子')
        await self.observe(e); req = await self.enrich(e)
        self.assertTrue(e.get_extra('_platform_reference_selection')['needs_clarification'])
        for reader in self.readers.values(): reader.read.assert_not_awaited()

    async def test_legacy_force_current_evidence_cleaned_only_in_request_copy(self):
        self.pushed()
        e = Event('总结你发的这个帖子'); await self.observe(e)
        saved = [{'role': 'user', 'content': '旧小红书链接\n【本次内容读取结果】本次必须回答旧小红书'}]
        req = SimpleNamespace(prompt=e.message_str, image_urls=[], contexts=saved)
        for reader in self.readers.values(): await reader.enrich(e, req)
        self.assertNotIn('本次必须', req.contexts[0]['content'])
        self.assertIn('本次必须', saved[0]['content'])
        self.assertEqual(req.prompt, e.message_str)


if __name__ == '__main__':
    import unittest
    unittest.main()
