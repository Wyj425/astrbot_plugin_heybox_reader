"""Administrator-only login endpoints. Credentials never appear in responses."""
import asyncio
import base64
import io
import json
import os
import time
from dataclasses import asdict
from urllib.parse import urlsplit,parse_qs
import aiohttp

def store(service, credentials):
    path=service.root/'credentials.json';tmp=path.with_suffix('.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:json.dump(credentials,f)
    os.chmod(tmp,0o600);os.replace(tmp,path)
    service.cache.values.clear();service.cache.failures.clear()
    if hasattr(service.backend,'reader'):
        service.backend.reader.cache.clear();service.backend.reader.failed.clear()

def qr_image(value):
    import qrcode
    out=io.BytesIO();qrcode.make(value).save(out,format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()

async def begin(s):
    if s.source=='小红书':
        data=await s.backend.xhs('login/qrcode')
        if data.get('is_logged_in'):
            s.login_state={'state':'done'};return
        image=data.get('img','')
        if image and not image.startswith('data:'):image='data:image/png;base64,'+image
        s.login_state={'state':'scan','qr':image,'expires_in':180}
        s.login_task=asyncio.create_task(poll_xhs(s));return
    if s.source=='小黑盒':
        from .heybox_client.api_client import XiaoheiheApiClient
        client=XiaoheiheApiClient('reader-login',max_retries=0)
        try:qr=await client.request_qr()
        except BaseException:await client.close();raise
        s.login_state={'state':'scan','qr':qr_image(qr.qr_content),'expires_in':180}
        s.login_task=asyncio.create_task(poll_heybox(s,client,qr));return
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as c:
        async with c.get('https://passport.bilibili.com/x/passport-login/web/qrcode/generate') as r:
            payload=await r.json()
    if payload.get('code')!=0:raise ValueError('qr_generate_failed')
    data=payload['data']
    s.login_state={'state':'scan','qr':qr_image(data['url']),'expires_in':180}
    s.login_task=asyncio.create_task(poll_bili(s,data['qrcode_key']))

async def poll_heybox(s,client,qr):
    try:
        for _ in range(60):
            await asyncio.sleep(3)
            state,_,credentials=await client.check_qr(qr)
            value=state.value.lower()
            if credentials:
                store(s,asdict(credentials));s.login_state={'state':'done'};return
            if value in ('expired','failed'):
                s.login_state={'state':'expired'};return
        s.login_state={'state':'expired'}
    except asyncio.CancelledError:raise
    except Exception as e:s.login_state={'state':'error','error':type(e).__name__}
    finally:await client.close()

async def poll_xhs(s):
    try:
        for _ in range(45):
            await asyncio.sleep(4)
            result=await s.backend.xhs('login/status')
            if result.get('is_logged_in'):
                s.login_state={'state':'done'};s.cache.values.clear();return
        s.login_state={'state':'expired'}
    except asyncio.CancelledError:raise
    except Exception as e:s.login_state={'state':'error','error':type(e).__name__}

async def poll_bili(s,key):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/'}) as c:
            for _ in range(60):
                await asyncio.sleep(3)
                async with c.get('https://passport.bilibili.com/x/passport-login/web/qrcode/poll',params={'qrcode_key':key},allow_redirects=False) as r:
                    payload=await r.json()
                    cookies={k:v.value for k,v in r.cookies.items()}
                if payload.get('code')!=0:raise ValueError('qr_poll_failed')
                data=payload.get('data',{});code=data.get('code')
                if code==0:
                    fields=parse_qs(urlsplit(data.get('url','')).query)
                    values={k:cookies.get(k) or (fields.get(k) or [''])[0] for k in ('SESSDATA','bili_jct','DedeUserID')}
                    if not values['SESSDATA'] or not values['bili_jct']:raise ValueError('incomplete_credentials')
                    store(s,{'sessdata':values['SESSDATA'],'bili_jct':values['bili_jct'],'dedeuserid':values['DedeUserID']})
                    s.login_state={'state':'done'};return
                if code==86038:s.login_state={'state':'expired'};return
                if code==86090:s.login_state['state']='confirm'
            s.login_state={'state':'expired'}
    except asyncio.CancelledError:raise
    except Exception as e:s.login_state={'state':'error','error':type(e).__name__}

async def verify(s):
    if s.source=='小红书':
        data=await s.backend.xhs('login/status')
        return {'logged_in':bool(data.get('is_logged_in'))}
    if s.source=='小黑盒':
        client=s.backend.heybox_client()
        try:
            await client.check_credentials();return {'logged_in':True}
        finally:await client.close()
    path=s.root/'credentials.json'
    creds=json.loads(path.read_text()) if path.exists() else {}
    if not creds.get('sessdata'):return {'logged_in':False}
    from bilibili_api import user,Credential
    data=await asyncio.wait_for(user.get_self_info(Credential(sessdata=creds['sessdata'],bili_jct=creds.get('bili_jct') or None)),20)
    return {'logged_in':bool(data.get('mid'))}
