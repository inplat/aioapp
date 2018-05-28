import asyncio
import pytest
from aiohttp import web
from aioapp.http import Server, Client, Handler
from aioapp.app import Application
from aioapp.misc import async_call
import aiozipkin.span as azs


def _create_span(app) -> azs.SpanAbc:
    if app.tracer:
        return app.tracer.new_trace(sampled=False, debug=False)


def test_server_fail_create(unused_tcp_port):
    class SomeClass:
        pass

    with pytest.raises(UserWarning):
        Server(
            host='127.0.0.1',
            port=unused_tcp_port,
            handler=SomeClass
        )


async def test_server(app, unused_tcp_port):
    class TestHandler(Handler):
        def __init__(self, server):
            super(TestHandler, self).__init__(server)
            self.server.add_route('GET', '/ok', self.ok_handler)
            self.server.add_route('GET', '/fb', self.fb_handler)

        async def ok_handler(self, ctx, request):
            return web.Response(status=200, text=self.app.my_param)

        async def fb_handler(self, ctx, request):
            raise web.HTTPForbidden()

    server = Server(
        host='127.0.0.1',
        port=unused_tcp_port,
        handler=TestHandler,
        access_log_format='%(asctime)-15s %(clientip)s %(user)-8s %(message)s'
    )
    client = Client()
    app.add('server', server)
    app.add('client', client)
    app.my_param = '123'
    await app.run_prepare()

    span = _create_span(app)

    resp = await app.client.post(span,
                                 'http://127.0.0.1:%d/' % unused_tcp_port)
    assert resp.status == 404

    resp = await app.client.get(span,
                                'http://127.0.0.1:%d/fb' % unused_tcp_port)
    assert resp.status == 403

    span2 = None
    if app.tracer:
        span2 = app.tracer.new_trace(sampled=True, debug=False)

    resp = await app.client.get(span2,
                                'http://127.0.0.1:%d/ok' % unused_tcp_port)
    assert resp.status == 200
    assert await resp.text() == app.my_param


async def test_server_error_handler(app, unused_tcp_port):
    class TestHandler(Handler):
        def __init__(self, server):
            super(TestHandler, self).__init__(server)
            self.server.set_error_handler(self.err_handler)

        async def err_handler(self, ctx, request, error):
            return web.Response(status=401, text='Error is ' + str(error))

    server = Server(
        host='127.0.0.1',
        port=unused_tcp_port,
        handler=TestHandler
    )
    client = Client()
    app.add('server', server)
    app.add('client', client)
    await app.run_prepare()

    span = _create_span(app)

    resp = await app.client.post(span,
                                 'http://127.0.0.1:%d/' % unused_tcp_port)
    assert resp.status == 401
    assert await resp.text() == 'Error is Not Found'


async def test_server_error_handler_fail(app, unused_tcp_port):
    class TestHandler(Handler):
        def __init__(self, server):
            super(TestHandler, self).__init__(server)
            self.server.set_error_handler(self.err_handler)

        async def err_handler(self, ctx, request, error):
            raise Warning()

    server = Server(
        host='127.0.0.1',
        port=unused_tcp_port,
        handler=TestHandler
    )
    client = Client()
    app.add('server', server)
    app.add('client', client)
    await app.run_prepare()

    span = _create_span(app)

    resp = await app.client.post(span,
                                 'http://127.0.0.1:%d/' % unused_tcp_port)
    assert resp.status == 500
    assert await resp.text() == ''


async def test_http_health_bad(app: Application, unused_tcp_port: int,
                               loop: asyncio.AbstractEventLoop) -> None:
    http = Server('127.0.0.1', unused_tcp_port, Handler)
    app.add('http', http)

    result = await app.health()
    assert 'http' in result
    assert result['http'] is not None
    assert isinstance(result['http'], BaseException)


async def test_http_health_ok(app: Application, unused_tcp_port: int,
                              loop: asyncio.AbstractEventLoop) -> None:
    http = Server('127.0.0.1', unused_tcp_port, Handler)
    app.add('http', http)

    async def start():
        await app.run_prepare()
        await http.start()

    res = async_call(loop, start)
    await asyncio.sleep(1)

    result = await app.health()
    assert 'http' in result
    assert result['http'] is None

    if res['fut'] is not None:
        res['fut'].cancel()
