import gc
import aiohttp
import aiohttp.web
from aiohttp.test_utils import TestServer
import logging
import asyncio
import socket
import pytest
from aioapp.app import Application


@pytest.fixture(scope='session')
def event_loop():
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    gc.collect()
    loop.close()


@pytest.fixture(scope='session')
def loop(event_loop):
    return event_loop


def get_free_port(protocol='tcp'):
    family = socket.AF_INET
    if protocol == 'tcp':
        type = socket.SOCK_STREAM
    elif protocol == 'udp':
        type = socket.SOCK_DGRAM
    else:
        raise UserWarning()

    sock = socket.socket(family, type)
    try:
        sock.bind(('', 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


@pytest.fixture(scope='session')
def tracer_server(loop):
    """Factory to create a TestServer instance, given an app.
    test_server(app, **kwargs)
    """

    servers = []
    requests = []

    async def go(**kwargs):
        async def tracer_handle(request):
            requests.append(await request.json())
            return aiohttp.web.Response(text='', status=201)

        app = aiohttp.web.Application()
        app.router.add_post('/api/v2/spans', tracer_handle)
        server = TestServer(app, host='127.0.0.1', port=None)
        await server.start_server(loop=loop, **kwargs)
        servers.append(server)
        return server

    srv = loop.run_until_complete(go())

    yield ('127.0.0.1', srv.port, requests)

    async def finalize():
        while servers:
            await servers.pop().close()

    loop.run_until_complete(finalize())


@pytest.fixture(scope='session')
def metrics_server(loop):
    requests = []

    class TelegrafProtocol:
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            d = data.decode().split(' ')
            n = d[0].split(',')
            requests.append({
                'name': n[0],
                'tags': {t.split('=')[0]: t.split('=')[1] for t in n[1:]},
                'duration': d[1],
                'time': d[2]
            })
            logging.info('TELEGRAF received %s from %s', data, addr)
            pass

        def connection_lost(self, err):
            logging.error(err)

    scheme = 'udp'
    host = '127.0.0.1'
    port = get_free_port(scheme)

    listen = loop.create_datagram_endpoint(
        TelegrafProtocol, local_addr=(host, port))
    transport, protocol = loop.run_until_complete(listen)

    yield (scheme, host, port, requests)

    transport.close()


@pytest.fixture(params=["with_tracer", "without_tracer"])
async def app(request, tracer_server, metrics_server, loop):
    app = Application(loop=loop)

    if request.param == 'with_tracer':
        tracer_addr = 'http://%s:%s/' % (tracer_server[0],
                                         tracer_server[1])
        metrics_addr = '%s://%s:%s' % (metrics_server[0],
                                       metrics_server[1],
                                       metrics_server[2])
        app.setup_logging(tracer_driver='zipkin',
                          tracer_addr=tracer_addr,
                          tracer_name='test',
                          metrics_driver='telegraf-influx',
                          metrics_name='test_',
                          metrics_addr=metrics_addr
                          )
    yield app
    await app.run_shutdown()
