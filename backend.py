import json, re, time
from urllib.parse import urlsplit
def clean(text, limit=4000):
    import html
    return re.sub(r'<[^>]*>', '', html.unescape(str(text or '')))[:limit].strip()

def matches(text, words):
    text = text.casefold()
    for word in words:
        aliases = [word]
        if word.lower() in ['apex','apex英雄']: aliases += ['Apex英雄','Apex Legends']
        if word.lower() in ['lol','英雄联盟']: aliases += ['LOL','英雄联盟']
        if word in ['游戏趣事','有趣日常','二游']: aliases += {'游戏趣事':['游戏'], '有趣日常':['趣事','搞笑','日常'], '二游':['原神','鸣潮','崩坏','绝区零','明日方舟']}[word]
        if any(w.casefold() in text for w in aliases): return True
    return False

from .reader import HeyboxReader, post_ref, extract_refs
class Backend:
    def __init__(self, root, config):
        self.root, self.config = root, config
        self.reader = HeyboxReader(config, root)
    async def close(self): await self.reader.close()
    async def search(self, words, max_age=7): return await self.heybox_search(words)
    async def read(self, value, images=True):
        ref = post_ref(value)
        if not ref: raise ValueError('invalid_heybox_post_url')
        result = await self.reader.read(ref)
        return {**result, 'source':'小黑盒', 'id':'heybox:'+ref['id'],
            'cover':next(iter(result['image_urls']),''), 'evidence':result['body'],
            'evidence_kind':'帖子正文', '_detail_loaded':True,
            'hot_comment':next((x['text'] for x in result['comments'] if x.get('is_floor')),'')}
    def refs(self, segments): return extract_refs(segments)
    def heybox_client(self):
        from .heybox_client.api_client import XiaoheiheApiClient
        from .heybox_client.models import Credentials
        return XiaoheiheApiClient('digest', credentials=Credentials(**json.loads(
            (self.root/'credentials.json').read_text())), max_retries=0)
    async def heybox_search(self, words):
        from .heybox_client.endpoints import EndpointName
        from .heybox_client.parsers import parse_feed
        client=self.heybox_client()
        try:
            catalog_file=self.root/'heybox-topics.json'
            topics=(json.loads(catalog_file.read_text()) if catalog_file.exists()
                    and time.time()-catalog_file.stat().st_mtime<86400 else [])
            if not topics:
                response=await client._request(EndpointName.TOPIC_INDEX,params={'type':'list'})
                topics=[]
                def walk(node):
                    if isinstance(node,list):
                        for x in node: walk(x)
                    elif isinstance(node,dict):
                        tid=str(node.get('topic_id') or node.get('id') or '')
                        name=str(node.get('name') or node.get('topic_name') or node.get('title') or '')
                        if tid.isdigit() and name: topics.append({'id':tid,'name':name})
                        for x in node.values():
                            if isinstance(x,(list,dict)): walk(x)
                walk(response.payload.get('result',response.payload.get('data',response.payload)))
                if topics:
                    catalog_file.write_text(json.dumps(topics,ensure_ascii=False));catalog_file.chmod(0o600)
            chosen=[t for t in topics if matches(t['name'],words)][:2]
            posts=[]
            for topic in chosen:
                response=await client._request(EndpointName.TOPIC_FEED,params={
                    'topic_id':topic['id'],'offset':0,'limit':10,'lastval':'','dw':720})
                posts.extend((x,True) for x in parse_feed(response.payload,offset=0).items)
            if not posts:
                posts=[(x,False) for x in (await client.fetch_feed(offset=0)).items]
            out=[]
            for x,topic_match in posts:
                if not topic_match and not matches(x.get('title','')+' '+x.get('content',''),words): continue
                pid=str(x.get('post_id',''));url=str(x.get('share_url',''))
                if not re.fullmatch(r'[A-Za-z0-9]{1,40}',pid) or urlsplit(url).hostname not in ['api.xiaoheihe.cn','www.xiaoheihe.cn','xiaoheihe.cn']: continue
                stamp=float(x.get('created_at') or 0)
                out.append(dict(id='heybox:'+pid,source='小黑盒',title=clean(x.get('title',''),120),
                    description=clean(x.get('content',''),400),url=url,
                    cover=next(iter(x.get('image_urls') or []),''),published_at=stamp,
                    published=stamp,queries=words))
            return out
        finally: await client.close()
