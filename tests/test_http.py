import pytest
from aiohttp import web
from aioapp.http import Server, Handler, ResponseCodec


def test_server_fail_create(unused_tcp_port):
    class SomeClass:
        pass

    with pytest.raises(UserWarning):
        Server(
            host='127.0.0.1',
            port=unused_tcp_port,
            handler=SomeClass
        )


async def test_response_code_abstact_create():
    with pytest.raises(NotImplementedError):
        await ResponseCodec().decode(None, None)


async def test_server(app, unused_tcp_port, client):
    class TestHandler(Handler):
        def __init__(self, server):
            super(TestHandler, self).__init__(server)
            self.server.add_route('GET', '/ok', self.ok_handler)

        async def ok_handler(self, context_span, request):
            return web.Response(status=200, text=self.app.my_param)

    http_server = Server(
        host='127.0.0.1',
        port=unused_tcp_port,
        handler=TestHandler
    )
    app.add('http_server', http_server)
    app.my_param = '123'
    await app.run_prepare()

    resp = await client.post('http://127.0.0.1:%d/' % unused_tcp_port)
    assert resp.status == 404

    resp = await client.get('http://127.0.0.1:%d/ok' % unused_tcp_port)
    assert resp.status == 200
    assert await resp.text() == app.my_param


async def test_server_error_handler(app, unused_tcp_port, client):
    class TestHandler(Handler):
        def __init__(self, server):
            super(TestHandler, self).__init__(server)
            self.server.set_error_handler(self.err_handler)

        async def err_handler(self, context_span, request, error):
            return web.Response(status=401, text='Error is ' + str(error))

    http_server = Server(
        host='127.0.0.1',
        port=unused_tcp_port,
        handler=TestHandler
    )
    app.add('http_server', http_server)
    await app.run_prepare()

    resp = await client.post('http://127.0.0.1:%d/' % unused_tcp_port)
    assert resp.status == 401
    assert await resp.text() == 'Error is Not Found'


async def test_server_error_handler_fail(app, unused_tcp_port, client):
    class TestHandler(Handler):
        def __init__(self, server):
            super(TestHandler, self).__init__(server)
            self.server.set_error_handler(self.err_handler)

        async def err_handler(self, context_span, request, error):
            raise Warning()

    http_server = Server(
        host='127.0.0.1',
        port=unused_tcp_port,
        handler=TestHandler
    )
    app.add('http_server', http_server)
    await app.run_prepare()

    resp = await client.post('http://127.0.0.1:%d/' % unused_tcp_port)
    assert resp.status == 500
    assert await resp.text() == ''
