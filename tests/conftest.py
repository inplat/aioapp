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
import aioredis
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


async def _docker_run(image, tag, image_port, check_fn):
    host = '127.0.0.1'
    unused_tcp_port = get_free_port()

    client = DockerClient(version='auto', **kwargs_from_env())
    print('Pulling image %s:%s' % (image, tag))
    client.images.pull(image, tag=tag)
    print('Stating %s:%s on %s:%s' % (image, tag, host, unused_tcp_port))
    cont = client.containers.run('%s:%s' % (image, tag), detach=True,
                                 ports={'%s/tcp' % image_port: (
                                     '0.0.0.0', unused_tcp_port)})
    try:
        await check_fn(host, unused_tcp_port)
        yield (host, unused_tcp_port)
    finally:
        cont.kill()
        cont.remove()


@pytest.fixture(scope='session')
async def postgres(loop):
    timeout = 60

    async def check_fn(host, port):

        start_time = time.time()
        conn = None
        while conn is None:
            if start_time + timeout < time.time():
                raise Exception("Initialization timeout, failed to "
                                "initialize postgres container")
            try:
                conn = await asyncpg.connect(
                    'postgresql://postgres@%s:%s/postgres'
                    '' % (host, port),
                    loop=loop)
            except Exception as e:
                time.sleep(.1)
        await conn.close()

    async for cred in _docker_run('postgres', 'latest', 5432, check_fn):
        yield cred


@pytest.fixture(scope='session')
async def redis(loop):
    timeout = 60

    async def check_fn(host, port):
        start_time = time.time()
        conn = None
        while conn is None:
            if start_time + timeout < time.time():
                raise Exception("Initialization timeout, failed to "
                                "initialize redis container")
            try:
                url = 'redis://%s:%s/0' % (host, port)
                conn = await aioredis.create_connection(url, loop=loop)
            except Exception as e:
                time.sleep(.1)
        conn.close()
        await conn.wait_closed()

    async for cred in _docker_run('redis', 'latest', 6379, check_fn):
        yield cred


@pytest.fixture(scope='session')
async def rabbit(loop):
    timeout = 60

    async def check_fn(host, port):
        start_time = time.time()
        conn = transport = None
        while conn is None:
            if start_time + timeout < time.time():
                raise Exception("Initialization timeout, failed t   o "
                                "initialize rabbitmq container")
            try:
                transport, conn = await aioamqp.connect(host, port, loop=loop)
            except Exception:
                time.sleep(.1)
        await conn.close()
        transport.close()

    async for cred in _docker_run('rabbitmq', 'latest', 5672, check_fn):
        yield cred


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


@pytest.fixture(params=["with_tracer", "without_tracer"])
async def app(request, tracer_server, loop):
    app = Application(loop=loop)

    if request.param == 'with_tracer':
        tracer_host = '127.0.0.1'
        tracer_port = (await tracer_server()).port
        tracer_addr = 'http://%s:%s/' % (tracer_host, tracer_port)
        app.setup_logging(tracer_driver='zipkin', tracer_addr=tracer_addr,
                          tracer_name='test')
    yield app
    await app.run_shutdown()
