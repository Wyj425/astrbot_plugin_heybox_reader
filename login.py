"""Xiaoheihe QR login and verification for the administrator page."""
import asyncio
import base64
import io
import json
import os
from dataclasses import asdict


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
    from .heybox_client.api_client import XiaoheiheApiClient
    client=XiaoheiheApiClient('reader-login',max_retries=0)
    try:qr=await client.request_qr()
    except BaseException:await client.close();raise
    s.login_state={'state':'scan','qr':qr_image(qr.qr_content),'expires_in':180}
    s.login_task=asyncio.create_task(poll_heybox(s,client,qr));return
    s.login_task=asyncio.create_task(poll_heybox(s,client,qr));return


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


async def verify(s):
    client=s.backend.heybox_client()
    try:
        await client.check_credentials();return {'logged_in':True}
    finally:await client.close()
