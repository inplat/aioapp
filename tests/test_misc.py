import time
import json
import datetime
import decimal
import asyncio
from functools import partial
import pytest
from yarl import URL
from aioapp.misc import (async_call, get_func_params, mask_url_pwd,
                         json_encode, rndstr, parse_dsn)


async def test_async_call(loop):
    fut = asyncio.Future(loop=loop)

    async def func(val1, val2):
        fut.set_result((val1, val2))

    async_call(loop, func, 1, val2=2)
    await asyncio.wait([fut], timeout=1)
    assert fut.result() == (1, 2)


async def test_async_call_delay(loop):
    delay = .1
    timeout = .2

    fut = asyncio.Future(loop=loop)

    async def func(val1, val2):
        fut.set_result((val1, val2))

    start = time.time()
    async_call(loop, func, 1, val2=2, delay=delay)
    await asyncio.wait([fut], timeout=timeout)
    assert time.time() - start >= delay
    assert fut.result() == (1, 2)


async def test_async_call_delay_td(loop):
    delay = .1
    timeout = .2

    fut = asyncio.Future(loop=loop)

    async def func(val1, val2):
        fut.set_result((val1, val2))

    start = time.time()
    async_call(loop, func, 1, val2=2, delay=datetime.timedelta(seconds=delay))
    await asyncio.wait([fut], timeout=timeout)
    assert time.time() - start >= delay
    assert fut.result() == (1, 2)


def test_get_func_params_as_method():
    class A1:
        def func(self, a, b=0):
            pass

    params_a1 = get_func_params(A1().func, {"a": 1})
    assert {'a': 1, 'b': 0} == params_a1


def test_get_func_params_as_func():
    def a2(a, b, c=0, *, e, f=1):
        pass

    params_a2 = get_func_params(a2, {"a": 1, "b": 2, 'e': 3})
    assert {'a': 1, 'b': 2, 'c': 0, 'e': 3, 'f': 1} == params_a2

    params_a3 = get_func_params(a2, {"a": 1, "b": 2, "c": 3, 'e': 3, 'f': 2})
    assert {'a': 1, 'b': 2, 'c': 3, 'e': 3, 'f': 2} == params_a3

    with pytest.raises(TypeError):
        get_func_params(a2, {"a": 1, "b": 2, "c": 3, 'f': 2})


def test_get_func_params_with_functools():
    def a4(a, b, c=3, e=4, *, f=3):
        pass

    pa4 = partial(a4, 1, c=0)
    params_a4 = get_func_params(pa4, {"b": 22})
    assert {'b': 22, 'c': 0, 'e': 4, 'f': 3} == params_a4


def test_get_func_params_with_kwargs():
    def a5(a, **kwargs):
        pass

    params_a5 = get_func_params(a5, {"a": 1, "b": 22})
    assert {'b': 22, 'a': 1} == params_a5


def test_get_func_params():
    def func1(a, b, c=3, d=4):
        pass

    given = {'a': 1, 'b': 2, 'd': 44}
    expected = {'a': 1, 'b': 2, 'c': 3, 'd': 44}
    assert expected == get_func_params(func1, given)
    # неверный вызов функции
    with pytest.raises(UserWarning):
        get_func_params(func1, 'incorrect')

    # аргумент b не передан
    with pytest.raises(Exception):
        get_func_params(func1, {'a': 1})


def test_get_func_params_unexpected_param():
    def func1(a):
        pass

    given = {'a': 1, 'b': 2}
    with pytest.raises(TypeError):
        get_func_params(func1, given)


def test_get_func_params_no_arg():
    def func1(a):
        pass

    given = {}
    with pytest.raises(TypeError):
        get_func_params(func1, given)


def test_get_func_params_no_kwarg():
    def func1(*, a):
        pass

    given = {}
    with pytest.raises(TypeError):
        get_func_params(func1, given)


def test_mask_url_pwd():
    given = 'postgres://user:password@host:123/'
    expected = 'postgres://user:******@host:123/'
    assert mask_url_pwd(given) == expected

    given = None
    expected = None
    assert mask_url_pwd(given) == expected

    given = 'postgres://host:123/'
    expected = 'postgres://host:123/'
    assert mask_url_pwd(given) == expected


def test_json_encode():
    def uk():
        pass

    dt1 = datetime.datetime.now()
    dt1_str = dt1.strftime('%Y-%m-%d %H:%M:%S.%f%z')
    dt2 = datetime.datetime.now(tz=datetime.timezone.utc)
    dt2_str = dt2.strftime('%Y-%m-%d %H:%M:%S.%f%z')
    t1 = dt1.time()
    t1_str = t1.strftime('%H:%M:%S.%f%z')
    t2 = dt2.time()
    t2_str = t2.strftime('%H:%M:%S.%f%z')
    d1 = dt1.date()
    d1_str = d1.strftime('%Y-%m-%d')
    d2 = dt2.date()
    d2_str = d2.strftime('%Y-%m-%d')
    td = datetime.timedelta(hours=1)
    td_exp = td.total_seconds()
    uk_str = repr(uk)
    url = URL('http://localhost/')
    url_str = 'http://localhost/'
    given = {
        'a': decimal.Decimal('1'),
        'b': 2,
        'dt1': dt1,
        'dt2': dt2,
        't1': t1,
        't2': t2,
        'd1': d1,
        'd2': d2,
        'td': td,
        'bytes': b'abc123',
        'bytes2': b'\xc0',
        'uk': uk,
        'url': url,
    }
    expected = {
        'a': 1.,
        'b': 2,
        'dt1': dt1_str,
        'dt2': dt2_str,
        't1': t1_str,
        't2': t2_str,
        'd1': d1_str,
        'd2': d2_str,
        'td': td_exp,
        'bytes': 'abc123',
        'bytes2': "b'\\xc0'",
        'uk': uk_str,
        'url': url_str,
    }
    assert json_encode(given) == json.dumps(expected)


def test_rndstr():
    rnd = rndstr(6)
    assert isinstance(rnd, str)
    assert len(rnd) == 6


def test_parse_dsn():
    parsed = parse_dsn('guest:pwd@localhost:5672/path')
    expected = ['localhost', 5672, 'guest', 'pwd', 'path']
    assert parsed == expected
