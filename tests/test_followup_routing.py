"""Offline cross-platform follow-up regression suite, copied into each reader."""
import copy
import importlib.util
import json
import logging
import pathlib
import sys
import types
import unittest
from collections import OrderedDict
from unittest.mock import AsyncMock, patch

# In a standalone plugin this file lives in tests/.
ROOT = pathlib.Path(__file__).resolve().parents[1]
URLS = {
    'B站': 'https://b23.tv/current-video',
    '小红书': 'https://www.xiaohongshu.com/explore/' + 'a' * 24,
    '小黑盒': 'https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=abc123',
}

def card(source):
    return [{'type':'json','data':{'data':json.dumps({'meta':{'news':{'jumpUrl':URLS[source]}}})}}]

class Event:
    def __init__(self,text='',segments=None,scope='qq:FriendMessage:123',ident='question'):
        self.message_str=text;self.unified_msg_origin=scope
        self.message_obj=types.SimpleNamespace(message_id=ident,raw_message={'message':segments or [],'message_id':ident})
        self.extra={};self.is_at_or_wake_command=False
        self.history=[];self.quoted={};self.actions=[]
        self.bot=types.SimpleNamespace(call_action=self.action)
    def is_stopped(self):return False
    def get_extra(self,key,default=None):return self.extra.get(key,default)
    def set_extra(self,key,value):self.extra[key]=value
    def get_platform_id(self):return 'qq'
    def get_sender_id(self):return self.unified_msg_origin.split(':')[-1]
    def get_self_id(self):return '999'
    def get_group_id(self):return ''
    async def action(self,name,**kwargs):
        self.actions.append(name)
        if name=='get_msg':return self.quoted[kwargs['message_id']]
        return {'messages':self.history}

class Followups(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        api=types.ModuleType('astrbot.api');api.logger=logging.getLogger('followup-test')
        spec=importlib.util.spec_from_file_location('_reader_followup_test',ROOT/'runtime.py')
        self.runtime=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'aiohttp':types.ModuleType('aiohttp'),'astrbot':types.ModuleType('astrbot'),'astrbot.api':api}):
            spec.loader.exec_module(self.runtime)
        self.now=10000.0
        timer=patch.object(self.runtime.time,'time',side_effect=lambda:self.now)
        timer.start();self.addCleanup(timer.stop)
        self.readers={}
        for source in ('小红书','小黑盒','B站'):  # Bili reloaded last, as in the real incident.
            p=object.__new__(self.runtime.PlatformService)
            p.name=source+'_reader';p.source=source;p.config={};p.recent=OrderedDict()
            p.read=AsyncMock(return_value={'source':source,'url':URLS[source],'title':source+' title',
                'body':source+' body','images':[],'comments':[],'evidence_kind':'正文'})
            self.readers[source]=p
    async def observe(self,event):
        for p in self.readers.values():await p.watch(event)
    async def enrich(self,event):
        req=types.SimpleNamespace(prompt=event.message_str,image_urls=[])
        for p in self.readers.values():await p.enrich(event,req)
        return req
    async def seed(self,source,scope='qq:FriendMessage:123'):
        await self.observe(Event(segments=card(source),scope=scope,ident='card'))
        self.now+=.2
    def history(self):
        return [
            {'time':9998,'message_id':'old-xhs','user_id':'123','message':card('小红书')},
            {'time':9999,'message_id':'new-bili','user_id':'123','message':card('B站')},
            {'time':9999,'message_id':'bot','user_id':'999','message':card('小黑盒')},
            {'time':10000,'message_id':'question','user_id':'123','message':[]},
            {'time':10001,'message_id':'future','user_id':'123','message':card('小红书')},
        ]

    async def test_new_bili_followup_cannot_be_claimed_by_old_xhs_hook(self):
        await self.seed('小红书');await self.seed('B站')
        event=Event('这个视频你能看到哪些信息');await self.observe(event)
        req=await self.enrich(event)
        self.readers['B站'].read.assert_awaited_once()
        for name in ('小红书','小黑盒'):self.readers[name].read.assert_not_awaited()
        self.assertIn('B站 body',req.prompt);self.assertNotIn('小红书 body',req.prompt)
        self.assertEqual(event.actions,[])

    async def test_reference_is_frozen_before_later_share_arrives(self):
        await self.seed('B站')
        event=Event('这个视频的评论呢');await self.observe(event)
        await self.seed('小红书')
        await self.enrich(event)
        self.readers['B站'].read.assert_awaited_once()
        self.readers['小红书'].read.assert_not_awaited()

    async def test_empty_cache_history_chooses_latest_across_all_platforms_once(self):
        event=Event('这个视频你能看到哪些信息');event.history=self.history()
        await self.observe(event);await self.enrich(event)
        self.readers['B站'].read.assert_awaited_once()
        self.readers['小红书'].read.assert_not_awaited()
        self.assertEqual(event.actions,['get_friend_msg_history'])

    async def test_quoted_old_xhs_card_overrides_recent_bili(self):
        await self.seed('B站')
        event=Event('读一下',[{'type':'reply','data':{'id':'7'}}])
        event.quoted[7]={'message':card('小红书')}
        await self.observe(event);await self.enrich(event)
        self.readers['小红书'].read.assert_awaited_once()
        self.readers['B站'].read.assert_not_awaited()
        self.assertEqual(event.actions,['get_msg'])

    async def test_explicit_source_can_find_older_matching_share(self):
        await self.seed('B站')
        event=Event('刚才小红书那篇的正文是什么');event.history=self.history()
        await self.observe(event);await self.enrich(event)
        self.readers['小红书'].read.assert_awaited_once()
        self.readers['B站'].read.assert_not_awaited()

    async def test_text_link_takes_precedence_over_every_cached_source(self):
        await self.seed('小红书')
        event=Event('看下这个视频',[{'type':'text','data':{'text':URLS['B站']}}])
        await self.observe(event);await self.enrich(event)
        self.readers['B站'].read.assert_awaited_once()
        self.readers['小红书'].read.assert_not_awaited()

    async def test_unknown_quoted_json_does_not_fall_back_to_stale_content(self):
        await self.seed('小红书')
        event=Event('这篇内容呢',[{'type':'reply','data':{'id':'7'}}])
        event.quoted[7]={'message':[{'type':'json','data':{'data':'{}'}}]}
        await self.observe(event);await self.enrich(event)
        for p in self.readers.values():p.read.assert_not_awaited()
        self.assertEqual(event.actions,['get_msg'])

    async def test_other_conversation_never_inherits_reference(self):
        await self.seed('B站')
        event=Event('这个视频呢',scope='qq:FriendMessage:456')
        await self.observe(event);await self.enrich(event)
        for p in self.readers.values():p.read.assert_not_awaited()

    async def test_probability_block_prevents_reading_and_history(self):
        await self.seed('B站')
        event=Event('这个视频呢');event.extra['group_media_probability_blocked']=True
        await self.observe(event);await self.enrich(event)
        for p in self.readers.values():p.read.assert_not_awaited()
        self.assertEqual(event.actions,[])

    async def test_normal_chat_does_not_fetch_old_content(self):
        await self.seed('B站')
        event=Event('晚上好');await self.observe(event);await self.enrich(event)
        for p in self.readers.values():p.read.assert_not_awaited()
        self.assertEqual(event.actions,[])

    async def test_followup_does_not_extend_old_share_lifetime(self):
        await self.seed('B站')
        before=copy.deepcopy(self.readers['B站'].recent)
        event=Event('这个视频呢');await self.observe(event);await self.enrich(event)
        self.assertEqual(self.readers['B站'].recent,before)

    def test_bili_evidence_explicitly_marks_missing_subtitles_and_frames(self):
        text=self.runtime.evidence({'source':'B站','body':'','description':'','evidence_kind':'标题与简介'})
        self.assertIn('"subtitles_available": false',text)
        self.assertIn('"video_frames_available": false',text)
        self.assertIn('"description_available": false',text)

if __name__=='__main__':unittest.main()
