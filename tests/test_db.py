from typing import Tuple
from aioapp.app import Application
from aioapp.db import Postgres, Redis
from aioapp.error import PrepareError
from async_timeout import timeout
import aiozipkin.span as azs
import pytest
import string
from aioapp.misc import rndstr


async def _start_postgres(app: Application, addr: Tuple[str, int],
                          connect_max_attempts=10,
                          connect_retry_delay=1.0) -> Postgres:
    url = 'postgres://postgres@%s:%d/postgres' % (addr[0], addr[1])
    db = Postgres(url, connect_max_attempts=connect_max_attempts,
                  connect_retry_delay=connect_retry_delay)
    app.add('db', db)
    await app.run_prepare()
    await db.start()
    return db


async def _start_redis(app: Application, addr: Tuple[str, int],
                       connect_max_attempts=10,
                       connect_retry_delay=1.0) -> Redis:
    url = 'redis://%s:%d/0?encoding=utf-8' % (addr[0], addr[1])
    db = Redis(url, connect_max_attempts=connect_max_attempts,
               connect_retry_delay=connect_retry_delay)
    app.add('redis', db)
    await app.run_prepare()
    await db.start()
    return db


def _create_span(app) -> azs.SpanAbc:
    if app.tracer:
        return app.tracer.new_trace(sampled=False, debug=False)


async def test_postgres(app, postgres):
    table_name = 'tbl_' + rndstr(20, string.ascii_lowercase + string.digits)
    db = await _start_postgres(app, postgres)
    span = _create_span(app)

    res = await db.execute(span, 'test',
                           'SELECT $1::int as a', 1, timeout=10)
    assert res is not None

    res = await db.query_one(span, 'test',
                             'SELECT $1::int as a, $2::json, $3::jsonb', 1, {},
                             {}, timeout=10)
    assert res is not None
    assert len(res) == 3
    assert res[0] == 1
    assert res[1] == {}
    assert res[2] == {}
    assert res['a'] == 1

    res = await db.query_one(span, 'test',
                             'SELECT $1::int as a WHERE FALSE', 1, timeout=10)
    assert res is None

    res = await db.query_all(span, 'test',
                             'SELECT UNNEST(ARRAY[$1::int, $2::int]) as a',
                             1, 2, timeout=10)
    assert res is not None
    assert len(res) == 2
    assert res[0][0] == 1
    assert res[1][0] == 2
    assert res[0]['a'] == 1
    assert res[1]['a'] == 2

    res = await db.query_all(span, 'test',
                             'SELECT $1::int as a WHERE FALSE', 1, timeout=10)
    assert res is not None
    assert len(res) == 0

    async with db.connection(span) as conn:
        async with conn.xact(span):
            await conn.execute(span, 'test',
                               'CREATE TABLE %s(id int);' % table_name)
            await conn.execute(span, 'test',
                               'INSERT INTO %s(id) VALUES(1)' % table_name)

    res = await db.query_one(span, 'test',
                             'SELECT COUNT(*) FROM %s' % table_name,
                             timeout=10)
    assert res[0] == 1

    try:
        async with db.connection(span) as conn:
            async with conn.xact(span, isolation_level='SERIALIZABLE'):
                await conn.execute(span, 'test',
                                   'INSERT INTO %s(id) VALUES(2)' % table_name)
                raise UserWarning()
    except UserWarning:
        pass

    res = await db.query_one(span, 'test',
                             'SELECT COUNT(*) FROM %s' % table_name,
                             timeout=10)
    assert res[0] == 1


async def test_postgres_prepare_failure(app, unused_tcp_port):
    with pytest.raises(PrepareError):
        await _start_postgres(app, ('127.0.0.1', unused_tcp_port),
                              connect_max_attempts=2,
                              connect_retry_delay=0.001)


async def test_redis(app, redis):
    db = await _start_redis(app, redis)
    span = _create_span(app)

    res = await db.execute(span, 'redis:set', 'SET', 'key1', 1)
    assert res == 'OK'

    res = await db.execute(span, 'redis:get', 'GET', 'key1')
    assert res == '1'

    async with db.connection(span) as conn1:
        async with db.connection(span) as conn2:
            res = await conn1.execute(span, 'redis:get', 'GET', 'key1')
            assert res == '1'

            res = await conn2.execute_pubsub(span, 'redis:sub',
                                             'SUBSCRIBE', 'test_channel')
            assert [[b'subscribe', b'test_channel', 1]] == res

            channel = conn2.pubsub_channels['test_channel']

            res = await conn1.execute(span, 'redis:pub', 'PUBLISH',
                                      'test_channel', 'val')
            assert res == 1

            async with timeout(5):
                if await channel.wait_message():
                    msg = await channel.get(encoding="UTF-8")
                    assert msg == 'val'

            res = await conn2.execute_pubsub(span, 'redis:unsub',
                                             'UNSUBSCRIBE', 'test_channel')
            assert [[b'unsubscribe', b'test_channel', 0]] == res


async def test_redis_prepare_failure(app, unused_tcp_port):
    with pytest.raises(PrepareError):
        await _start_redis(app, ('127.0.0.1', unused_tcp_port),
                           connect_max_attempts=2, connect_retry_delay=0.001)
