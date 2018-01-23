from typing import Tuple
from aioapp.app import Application
from aioapp.db import Postgres
from aioapp.error import PrepareError
import aiozipkin.span as azs
import pytest


async def _start_postgres(app: Application, postgres: Tuple[str, int],
                          connect_max_attempts=10,
                          connect_retry_delay=1.0) -> Postgres:
    dsn = 'postgres://postgres@%s:%d/postgres' % (postgres[0], postgres[1])
    db = Postgres(dsn, connect_max_attempts=connect_max_attempts,
                  connect_retry_delay=connect_retry_delay)
    app.add('db', db)
    await app.run_prepare()
    await db.start()
    return db


def _create_span(app) -> azs.SpanAbc:
    return app._tracer.new_trace(sampled=False, debug=False)


async def test_pgdb(app, postgres):
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
            await conn.execute(span, 'test', 'CREATE TABLE test(id int);')
            await conn.execute(span, 'test',
                               'INSERT INTO test(id) VALUES(1)')

    res = await db.query_one(span, 'test',
                             'SELECT COUNT(*) FROM test', timeout=10)
    assert res[0] == 1

    try:
        async with db.connection(span) as conn:
            async with conn.xact(span, isolation_level='SERIALIZABLE'):
                await conn.execute(span, 'test',
                                   'INSERT INTO test(id) VALUES(2)')
                raise UserWarning()
    except UserWarning:
        pass

    res = await db.query_one(span, 'test',
                             'SELECT COUNT(*) FROM test', timeout=10)
    assert res[0] == 1


async def test_prepare_failure(app, unused_tcp_port):
    with pytest.raises(PrepareError):
        await _start_postgres(app, ('127.0.0.1', unused_tcp_port),
                              connect_max_attempts=2,
                              connect_retry_delay=0.001)
