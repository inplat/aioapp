import gc
import time
import logging
import aiohttp
import asyncio
import socket
import pytest
from aiohttp.test_utils import TestServer
import aioamqp
import aioamqp.channel
import aioamqp.protocol
import aiohttp.web
import asyncpg
from docker.client import DockerClient
from docker.utils import kwargs_from_env
from async_generator import yield_, async_generator
from aioapp.app import Application

# отключаем логи ошибок, чтоб не засирать вывод
# logging.basicConfig(level=logging.CRITICAL)
logging.basicConfig(
    format='%(asctime)-15s %(message)s %(filename)s %(lineno)s %(funcName)s')
aioamqp.channel.logger.level = logging.CRITICAL
aioamqp.protocol.logger.level = logging.CRITICAL


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


def get_free_port():
    sock = socket.socket()
    try:
        sock.bind(('', 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


@pytest.fixture(scope='session')
async def postgres(loop):
    tag = 'latest'
    image = 'postgres'

    host = '127.0.0.1'
    timeout = 60

    unused_tcp_port = get_free_port()

    client = DockerClient(version='auto', **kwargs_from_env())
    client.images.pull(image, tag=tag)
    print('Stating %s:%s on %s:%s' % (image, tag, host, unused_tcp_port))
    cont = client.containers.run('%s:%s' % (image, tag), detach=True,
                                 ports={'5432/tcp': ('0.0.0.0',
                                                     unused_tcp_port)})
    try:
        start_time = time.time()
        conn = None
        while conn is None:
            if start_time + timeout < time.time():
                raise Exception("Initialization timeout, failed to "
                                "initialize postgresql container")
            try:
                conn = await asyncpg.connect(
                    'postgresql://postgres@%s:%s/postgres'
                    '' % (host, unused_tcp_port),
                    loop=loop)
            except Exception as e:
                time.sleep(.1)
        await conn.close()
        yield (host, unused_tcp_port)
    finally:
        cont.kill()
        cont.remove()


@pytest.fixture(scope='session')
async def rabbit(loop, rabbit_override_addr):
    if rabbit_override_addr:
        yield rabbit_override_addr.split(':')
        return
    tag = '3.7.1'
    image = 'rabbitmq:{}'.format(tag)

    host = '0.0.0.0'
    timeout = 60

    unused_tcp_port = get_free_port()

    client = DockerClient(version='auto', **kwargs_from_env())
    print('Stating rabbitmq %s on %s:%s' % (image, host, unused_tcp_port))
    cont = client.containers.run(image, detach=True,
                                 ports={'5672/tcp': ('0.0.0.0',
                                                     unused_tcp_port)})
    try:
        start_time = time.time()
        conn = transport = None
        while conn is None:
            if start_time + timeout < time.time():
                raise Exception("Initialization timeout, failed t   o "
                                "initialize rabbitmq container")
            try:
                transport, conn = await aioamqp.connect(host, unused_tcp_port,
                                                        loop=loop)
            except Exception:
                time.sleep(.1)
        await conn.close()
        transport.close()

        yield (host, unused_tcp_port)
    finally:
        cont.kill()
        cont.remove()


@pytest.fixture
@async_generator
async def client(loop):
    async with aiohttp.ClientSession(loop=loop) as client:
        await yield_(client)


@pytest.fixture(scope='session')
def tracer_server(loop):
    """Factory to create a TestServer instance, given an app.
    test_server(app, **kwargs)
    """
    servers = []

    async def go(**kwargs):
        def tracer_handle(request):
            return aiohttp.web.Response(text='', status=201)

        app = aiohttp.web.Application()
        app.router.add_post('/api/v2/spans', tracer_handle)
        server = TestServer(app, port=None)
        await server.start_server(loop=loop, **kwargs)
        servers.append(server)
        return server

    yield go

    async def finalize():
        while servers:
            await servers.pop().close()

    loop.run_until_complete(finalize())


@pytest.fixture
async def app(tracer_server, loop):
    tracer_host = '127.0.0.1'
    tracer_port = (await tracer_server()).port
    tracer_addr = 'http://%s:%s/' % (tracer_host, tracer_port)

    app = Application(loop=loop)
    app.setup_logging(tracer_driver='zipkin', tracer_addr=tracer_addr,
                      tracer_name='test')
    yield app
    await app.run_shutdown()
