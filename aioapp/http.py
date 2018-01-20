from abc import ABCMeta
import io
from typing import Type, Any
from functools import partial
import asyncio
import traceback
from urllib.parse import urlparse
from aiohttp import web, ClientSession
from aiohttp import ClientResponse
from aiohttp.payload import BytesPayload
from aiohttp import client_exceptions, TCPConnector
from .app import Component
import logging
import aiozipkin as az
import aiozipkin.aiohttp_helpers as azah
import aiozipkin.span as azs
import aiozipkin.constants as azc


access_logger = logging.getLogger('aiohttp.access')
SPAN_KEY = 'zipkin_span'


class Handler(object):
    __metaclass__ = ABCMeta

    def __init__(self, server: 'Server') -> None:
        self.server = server

    @property
    def app(self):
        return self.server.app


class ResponseCodec:

    async def decode(self, context_span: azs.SpanAbc,
                     response: ClientResponse) -> Any:
        raise NotImplementedError()


class Server(Component):

    def __init__(self, host: str, port: int, handler: Type[Handler],
                 access_log_format=None, access_log=access_logger,
                 shutdown_timeout=60.0) -> None:
        if not issubclass(handler, Handler):
            raise UserWarning()
        super(Server, self).__init__()
        self.web_app = web.Application(loop=self.loop,
                                       middlewares=[
                                           self.wrap_middleware, ])
        self.host = host
        self.port = port
        self.error_handler = None
        self.access_log_format = access_log_format
        self.access_log = access_log
        self.shutdown_timeout = shutdown_timeout
        self.web_app_handler = None
        self.servers = None
        self.server_creations = None
        self.uris = None
        self.handler = handler(self)

    async def wrap_middleware(self, app, handler):
        async def middleware_handler(request: web.Request):
            if self.app._tracer:
                context = az.make_context(request.headers)
                if context is None:
                    sampled = azah.parse_sampled(request.headers)
                    debug = azah.parse_debug(request.headers)
                    span = self.app._tracer.new_trace(sampled=sampled,
                                                      debug=debug)
                else:
                    span = self.app._tracer.join_span(context)
                request[SPAN_KEY] = span

                if span.is_noop:
                    resp, trace_str = await self._error_handle(span, request,
                                                               handler)
                    return resp

                with span:
                    span_name = '{0} {1}'.format(request.method.upper(),
                                                 request.path)
                    span.name(span_name)
                    span.kind(azah.SERVER)
                    span.tag(azah.HTTP_PATH, request.path)
                    span.tag(azah.HTTP_METHOD, request.method.upper())
                    _annotate_bytes(span, await request.read())
                    resp, trace_str = await self._error_handle(span, request,
                                                               handler)
                    span.tag(azah.HTTP_STATUS_CODE, resp.status)
                    _annotate_bytes(span, resp.body)
                    if trace_str is not None:
                        span.annotate(trace_str)
                    return resp
            else:
                resp, trace_str = await self._error_handle(None, request,
                                                           handler)
                return resp

        return middleware_handler

    async def _error_handle(self, span, request, handler):
        try:
            resp = await handler(request)
            return resp, None
        except Exception as herr:
            if span is not None:
                span.tag('error', 'true')
                span.tag('error.message', str(herr))

            trace = None
            if self.error_handler:
                try:
                    resp = await self.error_handler(span, request, herr)
                    trace = traceback.format_exc()
                except Exception as eerr:
                    if isinstance(eerr, web.HTTPException):
                        resp = eerr
                    else:
                        self.app.log_err(eerr)
                        resp = web.Response(status=500, text='')
                    trace = traceback.format_exc()
            else:
                if isinstance(herr, web.HTTPException):
                    resp = herr
                else:
                    resp = web.Response(status=500, text='')

            return resp, trace

    def add_route(self, method, uri, handler):
        self.web_app.router.add_route(method, uri,
                                      partial(self._handle_request, handler))

    def set_error_handler(self, handler):
        self.error_handler = handler

    async def _handle_request(self, handler, request):
        res = await handler(request.get(SPAN_KEY), request)
        return res

    async def prepare(self):
        self.app.log_info("Preparing to start http server")
        await self.web_app.startup()

        make_handler_kwargs = dict()
        if self.access_log_format is not None:
            make_handler_kwargs['access_log_format'] = self.access_log_format
        self.web_app_handler = self.web_app.make_handler(
            loop=self.loop,
            access_log=self.access_log,
            **make_handler_kwargs)
        self.server_creations, self.uris = web._make_server_creators(
            self.web_app_handler,
            loop=self.loop, ssl_context=None,
            host=self.host, port=self.port, path=None, sock=None, backlog=128)

    async def start(self):
        self.app.log_info("Starting http server")
        self.servers = await asyncio.gather(*self.server_creations,
                                            loop=self.loop)
        self.app.log_info('HTTP server ready to handle connections on %s'
                          '' % (', '.join(self.uris), ))

    async def stop(self):
        self.app.log_info("Stopping http server")
        server_closures = []
        for srv in self.servers:
            srv.close()
            server_closures.append(srv.wait_closed())
        await asyncio.gather(*server_closures, loop=self.loop)
        await self.web_app.shutdown()
        await self.web_app_handler.shutdown(self.shutdown_timeout)
        await self.web_app.cleanup()


class Client(Component):
    # TODO make pool of clients

    async def prepare(self):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    async def post(self, context_span: azs.SpanAbc, span_params,
                   response_codec, url,
                   data=None, headers=None,
                   read_timeout=None, conn_timeout=None, ssl_ctx=None):
        """
        :type context_span: azs.SpanAbc
        :type span_params: dict
        :type response_codec: AbstractResponseCodec
        :type url: str
        :type data: bytes
        :type headers: dict
        :type read_timeout: float
        :type conn_timeout: float
        :type ssl_ctx: ssl.SSLContext
        :rtype: Awaitable[ClientResponse]
        """
        conn = TCPConnector(ssl_context=ssl_ctx)
        # TODO проверить доступные хосты для передачи трассировочных заголовков
        headers = headers or {}
        headers.update(context_span.context.make_headers())
        with context_span.tracer.new_child(context_span.context) as span:
            async with ClientSession(loop=self.loop,
                                     headers=headers,
                                     read_timeout=read_timeout,
                                     conn_timeout=conn_timeout,
                                     connector=conn) as session:
                if 'name' in span_params:
                    span.name(span_params['name'])
                if 'endpoint_name' in span_params:
                    span.remote_endpoint(span_params['endpoint_name'])
                if 'tags' in span_params:
                    for tag_name, tag_val in span_params['tags'].items():
                        span.tag(tag_name, tag_val)

                span.kind(az.CLIENT)
                span.tag(azah.HTTP_METHOD, "POST")
                parsed = urlparse(url)
                span.tag(azc.HTTP_HOST, parsed.netloc)
                span.tag(azc.HTTP_PATH, parsed.path)
                span.tag(azc.HTTP_REQUEST_SIZE, str(len(data)))
                span.tag(azc.HTTP_URL, url)
                _annotate_bytes(span, data)
                try:
                    async with session.post(url, data=data) as resp:
                        response_body = await resp.read()
                        _annotate_bytes(span, response_body)
                        span.tag(azc.HTTP_STATUS_CODE, resp.status)
                        span.tag(azc.HTTP_RESPONSE_SIZE,
                                 str(len(response_body)))
                        dec = await response_codec.decode(span, resp)
                        return dec
                except client_exceptions.ClientError as e:
                    span.tag("error.message", str(e))
                    raise


def _annotate_bytes(span, data):
    if isinstance(data, BytesPayload):
        pl = io.BytesIO()
        data.write(pl)
        data = pl.getvalue()
    try:
        data_str = data.decode("UTF8")
    except Exception:
        data_str = str(data)
    span.annotate(data_str or 'null')
