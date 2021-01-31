#!/usr/bin/env python3

import aiohttp
import asyncio
import asyncio_dgram
import os
import signal
import socket
import time

from contextlib import closing
from sanic import Sanic, response
from sanic.log import logger as log


HOME = os.environ.get('HOME') or '/home/'
SANIC_HOST = os.environ.get('SANIC_HOST') or '127.0.0.1'
SANIC_PORT = int(os.environ.get('SANIC_PORT')) or 8888
SANIC_EPG_HOST = os.environ.get('SANIC_EPG_HOST') or '127.0.0.1'
SANIC_EPG_PORT = int(os.environ.get('SANIC_EPG_PORT')) or 8889
UDPXY = os.environ.get('UDPXY') or 'http://192.168.137.1:4022/rtp/'

MIME = 'video/MP2T'
GUIDE = os.path.join(HOME, 'guide.xml')
CHANNELS = os.path.join(HOME, 'MovistarTV.m3u')
SESSION = None

app = Sanic('Movistar_u7d')
app.config.update({'KEEP_ALIVE': False})


@app.listener('after_server_stop')
async def notify_server_stop(app, loop):
    log.debug('after_server_stop killing u7d.py')
    p = await asyncio.create_subprocess_exec('/usr/bin/pkill', '-INT', '-f', '/app/u7d.py .+ -p ')
    await p.wait()

@app.get('/channels.m3u')
@app.get('/MovistarTV.m3u')
async def handle_channels(request):
    if not os.path.exists(CHANNELS):
        return response.json({}, 404)
    return await response.file(CHANNELS)

@app.get('/guide.xml')
async def handle_guide(request):
    if not os.path.exists(GUIDE):
        return response.json({}, 404)
    return await response.file(GUIDE)

@app.get('/rtp/<channel_id>/<channel_key>/<url>')
async def handle_rtp(request, channel_id, channel_key, url):
    log.info(f'Request: {request.method} {request.raw_url.decode()} [{request.ip}]')

    if url.startswith('239'):
        log.info(f'Redirect: {UDPXY + url}')
        return response.redirect(UDPXY + url)

    elif url.startswith('video-'):
        global SESSION
        if not SESSION:
            SESSION = aiohttp.ClientSession()

        try:
            program_id = None
            epg_url = f'http://{SANIC_EPG_HOST}:{SANIC_EPG_PORT}/get_program_id/{channel_id}/{channel_key}/{url}'
            async with SESSION.get(epg_url, timeout=aiohttp.ClientTimeout(connect=2)) as r:
                if r.status != 200:
                    return response.json({'status': f'{url} not found'}, 404)
                r = await r.json()
                channel_id = r['channel_id']
                program_id = r['program_id']
                offset = r['offset']
        except Exception as ex:
            log.debug(f"aiohttp.ClientSession().get('{epg_url}') {repr(ex)} [{request.ip}]")

        if not program_id:
            return response.json({'status': f'{channel_id}/{channel_key}/{url} not found'}, 404)

        with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
            s.bind(('', 0))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            client_port = str(s.getsockname()[1])
        u7d_msg = f'/app/u7d.py {channel_id} {program_id} -s {offset} -p {client_port} [{request.ip}]'
        log.info(f'Starting: {u7d_msg}')
        u7d = await asyncio.create_subprocess_exec('/app/u7d.py', channel_id, program_id, '-s', offset,
                                                   '-p', client_port, '-i', request.ip)
        try:
            r = await asyncio.wait_for(u7d.wait(), 0.3)
            msg = f'NOT AVAILABLE: {u7d_msg}'
            log.info(msg)
            return response.json({'status': msg}, 404)
        except asyncio.exceptions.TimeoutError:
            pass

        async def udp_streaming(response):
            host = socket.gethostbyname(socket.gethostname())
            log.info(f'Stream: {channel_id}/{channel_key}/{url} => @{host}:{client_port} [{request.ip}]')
            try:
                with closing(await asyncio_dgram.bind((host, int(client_port)))) as stream:
                    while True:
                        data, remote_addr = await stream.recv()
                        await response.write(data)
                log.info(f'Stream loop ended [{request.ip}]')
            except Exception as ex:
                msg = f'Stream loop excepted: {repr(ex)}'
                log.debug(msg)
                return response.json({'status': msg}, 500)
            finally:
                log.debug(f'Finally {u7d_msg}')
                try:
                    u7d.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass

        return response.stream(udp_streaming, content_type=MIME)

    else:
        return response.json({'status': 'URL not understood'}, 404)


if __name__ == '__main__':
    app.run(host=SANIC_HOST, port=SANIC_PORT, access_log=False, auto_reload=True, debug=True, workers=3)
