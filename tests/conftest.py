import gc
from yarl import URL
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
from compose.service import ImageType
from compose.project import Project
from async_generator import yield_, async_generator
from aioapp.app import Application

# отключаем логи ошибок, чтоб не засирать вывод
# logging.basicConfig(level=logging.CRITICAL)
logging.basicConfig(
    format='%(asctime)-15s %(message)s %(filename)s %(lineno)s %(funcName)s')
aioamqp.channel.logger.level = logging.CRITICAL
aioamqp.protocol.logger.level = logging.CRITICAL

COMPOSE_POSTGRES_PORT = 19801
COMPOSE_REDIS_PORT = 19802
COMPOSE_RABBITMQ_PORT = 19803


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


def pytest_addoption(parser):
    parser.addoption("--tracer-addr", dest="tracer_addr",
                     help="Use this tracer instead of emulator if specified",
                     metavar="host:port")
    parser.addoption("--metrics-addr", dest="metrics_addr",
                     help="Use this metrics collector instead of emulator if "
                          "specified",
                     metavar="scheme://host:port")
    parser.addoption("--postgres-addr", dest="postgres_addr",
                     help="Use this postgres instead of docker image "
                          "if specified",
                     metavar="postgres://user:passwd@host:port/dbname")
    parser.addoption("--redis-addr", dest="redis_addr",
                     help="Use this redis instead of docker image "
                          "if specified",
                     metavar="redis://host:port/db")
    parser.addoption("--rabbitmq-addr", dest="rabbitmq_addr",
                     help="Use this rabbitmq instead of docker image "
                          "if specified",
                     metavar="amqp://user:passwd@host:port/")
    parser.addoption('--show-docker-logs', dest="show_docker_logs",
                     action='store_true', default=False,
                     help='Show docker logs after test')


@pytest.fixture(scope='session')
def metrics_override_addr(request):
    return request.config.getoption('metrics_addr')


@pytest.fixture(scope='session')
def tracer_override_addr(request):
    return request.config.getoption('tracer_addr')


@pytest.fixture(scope='session')
def postgres_override_addr(request):
    return request.config.getoption('postgres_addr')


@pytest.fixture(scope='session')
def redis_override_addr(request):
    return request.config.getoption('redis_addr')


@pytest.fixture(scope='session')
def rabbitmq_override_addr(request):
    return request.config.getoption('rabbitmq_addr')


@pytest.fixture(scope='session')
async def docker_compose(loop, request,
                         docker_project: Project,
                         postgres_override_addr,
                         redis_override_addr,
                         rabbitmq_override_addr):
    async def check_postgres(url):
        conn = await asyncpg.connect(url, loop=loop)
        await conn.close()

    async def check_redis(url):
        conn = await aioredis.create_connection(url, loop=loop)
        conn.close()
        await conn.wait_closed()

    async def check_rabbitmq(url):
        transport, protocol = await aioamqp.from_url(url, loop=loop)
        await protocol.close()

    checks = {
        (
            'postgres',
            'POSTGRES_DSN',
            postgres_override_addr,
            'postgresql://postgres@127.0.0.1:%d/postgres'
            '' % COMPOSE_POSTGRES_PORT,
            check_postgres
        ),
        (
            'redis',
            'REDIS_DSN',
            redis_override_addr,
            'redis://127.0.0.1:%d/1?encoding=utf-8' % COMPOSE_REDIS_PORT,
            check_redis
        ),
        (
            'rabbitmq',
            'RABBITMQ_DSN',
            rabbitmq_override_addr,
            'amqp://guest:guest@127.0.0.1:%d/' % COMPOSE_RABBITMQ_PORT,
            check_rabbitmq
        ),
    }

    result = {}

    fns = []
    to_start = []
    for svc, name, override, url, fn in checks:
        if override:
            result[name] = override
        else:
            to_start.append(svc)
            fns.append((fn, url))
            result[name] = url

    if not to_start:
        yield result
    else:
        containers = docker_project.up(to_start)

        if not containers:
            raise ValueError("`docker-compose` didn't launch any containers!")

        try:
            timeout = 60
            start_time = time.time()
            print()
            print('Waiting for docker services...')
            last_err = None
            while start_time + timeout > time.time():
                try:
                    await asyncio.gather(*[fn(url) for fn, url in fns],
                                         loop=loop)
                    break

                except Exception as err:
                    last_err = err
                    await asyncio.sleep(1, loop=loop)
            else:
                last_err_type = type(last_err)
                raise TimeoutError(f'Unable to start all container services'
                                   f' within {timeout} seconds. Last error:'
                                   f' {last_err} ({last_err_type})')
            print('Docker services are ready')
            yield result
        finally:

            # Send container logs to stdout, so that they get included in
            # the test report.
            # https://docs.pytest.org/en/latest/capture.html
            for container in sorted(containers, key=lambda c: c.name):
                if request.config.getoption('show_docker_logs'):
                    header = f"Logs from {container.name}:"
                    print(header)
                    print("=" * len(header))
                    print(
                        container.logs().decode("utf-8", errors="replace") or
                        "(no logs)"
                    )
                    print()

            docker_project.down(ImageType.none, False)


@pytest.fixture(scope='session')
def postgres(docker_compose):
    return docker_compose['POSTGRES_DSN']


@pytest.fixture(scope='session')
def redis(docker_compose):
    return docker_compose['REDIS_DSN']


@pytest.fixture(scope='session')
def rabbitmq(docker_compose):
    return docker_compose['RABBITMQ_DSN']


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


@pytest.fixture
@async_generator
async def client(loop):
    async with aiohttp.ClientSession(loop=loop) as client:
        await yield_(client)


@pytest.fixture(scope='session')
def tracer_server(loop, tracer_override_addr):
    """Factory to create a TestServer instance, given an app.
    test_server(app, **kwargs)
    """
    if tracer_override_addr:
        host, port = tracer_override_addr.split(':')
        yield host, int(port)
        return

    servers = []

    async def go(**kwargs):
        def tracer_handle(request):
            return aiohttp.web.Response(text='', status=201)

        app = aiohttp.web.Application()
        app.router.add_post('/api/v2/spans', tracer_handle)
        server = TestServer(app, host='127.0.0.1', port=None)
        await server.start_server(loop=loop, **kwargs)
        servers.append(server)
        return server

    srv = loop.run_until_complete(go())

    yield ('127.0.0.1', srv.port)

    async def finalize():
        while servers:
            await servers.pop().close()

    loop.run_until_complete(finalize())


@pytest.fixture(scope='session')
def metrics_server(loop, metrics_override_addr):
    if metrics_override_addr:
        addr = URL(metrics_override_addr)
        yield (
            addr.scheme or 'udp',
            addr.host or '127.0.0.1',
            addr.port or 8094,
        )
        return

    class TelegrafProtocol:
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
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

    yield (scheme, host, port)

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
