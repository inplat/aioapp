from abc import ABCMeta
import ssl
from typing import Type, Any, Optional
from functools import partial
import asyncio
import traceback
from urllib.parse import urlparse
from aiohttp import web, web_runner, ClientSession, hdrs, helpers
from aiohttp import ClientResponse
from aiohttp import TCPConnector
from .app import Component
import logging
from .tracer import (Span, CLIENT, SERVER, HTTP_PATH, HTTP_METHOD, HTTP_HOST,
                     HTTP_REQUEST_SIZE, HTTP_RESPONSE_SIZE, HTTP_STATUS_CODE,
                     HTTP_URL, SPAN_TYPE, SPAN_KIND, SPAN_TYPE_HTTP,
                     SPAN_KIND_HTTP_IN, SPAN_KIND_HTTP_OUT)

access_logger = logging.getLogger('aiohttp.access')
SPAN_KEY = 'ctx'


class Handler(object):
    __metaclass__ = ABCMeta

    def __init__(self, server: 'Server') -> None:
        self.server = server

    @property
    def app(self):
        return self.server.app

    async def prepare(self) -> None:
        pass


class HttpClientTracerConfig:

    def on_request_start(self, ctx: 'Span',
                         session: ClientSession) -> None:
        pass

    def on_request_end(self, ctx: 'Span', err: Optional[Exception],
                       result: Optional[ClientResponse],
                       response_body: Optional[bytes]) -> None:
        if result:
            ctx.tag(HTTP_STATUS_CODE, result.status, True)
        if response_body is not None:
            ctx.tag(HTTP_RESPONSE_SIZE, str(len(response_body)))

        if err:
            ctx.finish(exception=err)
            ctx.tag('error.message', str(err))


class Server(Component):
    def __init__(self, host: str, port: int, handler: Type[Handler],

                 ssl_context=None,
                 backlog=128,
                 access_log_class=helpers.AccessLogger,
                 access_log_format=helpers.AccessLogger.LOG_FORMAT,
                 access_log=access_logger,
                 reuse_address=None, reuse_port=None,

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
        self.ssl_context = ssl_context
        self.backlog = backlog
        self.access_log_class = access_log_class
        self.access_log_format = access_log_format
        self.access_log = access_log
        self.reuse_address = reuse_address
        self.reuse_port = reuse_port
        self.shutdown_timeout = shutdown_timeout
        self.web_app_handler = None
        self.servers = None
        self.server_creations = None
        self.uris = None
        self.handler = handler(self)
        self._sites: list = []
        self._runner: web_runner.AppRunner = None

    async def wrap_middleware(self, app, handler):
        async def middleware_handler(request: web.Request):
            if self.app.tracer:
                span = self.app.tracer.new_trace_from_headers(request.headers)
                request[SPAN_KEY] = span

                with span:
                    span_name = '{0} {1}'.format(request.method.upper(),
                                                 request.path)
                    span.name(span_name)
                    span.kind(SERVER)
                    span.metrics_tag(SPAN_TYPE, SPAN_TYPE_HTTP)
                    span.metrics_tag(SPAN_KIND, SPAN_KIND_HTTP_IN)
                    span.tag(HTTP_PATH, request.path)
                    span.tag(HTTP_METHOD, request.method.upper(), True)
                    resp, trace_str = await self._error_handle(span, request,
                                                               handler)
                    if isinstance(resp, web.Response):
                        span.tag(HTTP_STATUS_CODE, resp.status, True)
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
            trace = traceback.format_exc()

            if span is not None:
                span.tag('error', 'true', True)
                span.tag('error.message', str(herr))
                span.annotate(trace)

            if self.error_handler:
                try:
                    resp = await self.error_handler(span, request, herr)

                except Exception as eerr:
                    if isinstance(eerr, web.HTTPException):
                        resp = eerr
                    else:
                        self.app.log_err(eerr)
                        resp = web.Response(status=500, text='')
                    trace = traceback.format_exc()
                    if span:
                        span.annotate(trace)
            else:
                if isinstance(herr, web.HTTPException):
                    resp = herr
                else:
                    resp = web.Response(status=500, text='')

            return resp, trace

    def add_route(self, method, uri, handler):
        if not asyncio.iscoroutinefunction(handler):
            raise UserWarning('handler must be coroutine function')
        self.web_app.router.add_route(method, uri,
                                      partial(self._handle_request, handler))

    def set_error_handler(self, handler):
        if not asyncio.iscoroutinefunction(handler):
            raise UserWarning('handler must be coroutine function')
        self.error_handler = handler

    async def _handle_request(self, handler, request):
        res = await handler(request.get(SPAN_KEY), request)
        return res

    async def prepare(self):
        self.app.log_info("Preparing to start http server")
        self._runner = web_runner.AppRunner(
            self.web_app,
            handle_signals=False,
            access_log_class=self.access_log_class,
            access_log_format=self.access_log_format,
            access_log=self.access_log)
        await self.handler.prepare()
        await self._runner.setup()
        self._sites = []
        self._sites.append(web_runner.TCPSite(
            self._runner,
            self.host,
            self.port,
            shutdown_timeout=self.shutdown_timeout,
            ssl_context=self.ssl_context,
            backlog=self.backlog,
            reuse_address=self.reuse_address,
            reuse_port=self.reuse_port))

    async def start(self):
        self.app.log_info("Starting http server")
        await asyncio.gather(*[site.start() for site in self._sites],
                             loop=self.loop)
        self.app.log_info('HTTP server ready to handle connections on %s:%s'
                          '' % (self.host, self.port))

    async def stop(self):
        self.app.log_info("Stopping http server")
        if self._runner:
            await self._runner.cleanup()

    async def health(self, ctx: Span):
        if self.loop is None:
            return

        coro = asyncio.open_connection(host=self.host, port=self.port,
                                       loop=self.loop)
        with ctx.new_child("tcp:connect", CLIENT) as span:
            span.tag('tcp.host', self.host)
            span.tag('tcp.port', str(self.port))
            await asyncio.wait_for(coro, timeout=10, loop=self.loop)


class Client(Component):
    # TODO make pool of clients

    async def prepare(self):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    async def request(self,
                      ctx: Optional[Span],
                      method: str,
                      url: str,
                      data: Any = None,
                      headers: Optional[dict] = None,
                      read_timeout: Optional[float] = None,
                      conn_timeout: Optional[float] = None,
                      ssl_ctx: Optional[ssl.SSLContext] = None,
                      tracer_config: Optional[HttpClientTracerConfig] = None,
                      **kwargs
                      ) -> ClientResponse:
        conn = TCPConnector(ssl_context=ssl_ctx, loop=self.loop)
        headers = headers or {}
        # TODO optional propagate tracing headers
        span = None
        if ctx:
            headers.update(ctx.make_headers())
            span = ctx.new_child()
        try:
            async with ClientSession(loop=self.loop,
                                     headers=headers,
                                     read_timeout=read_timeout,
                                     conn_timeout=conn_timeout,
                                     connector=conn) as session:
                parsed = urlparse(url)

                if span:
                    span.kind(CLIENT)
                    span.metrics_tag(SPAN_TYPE, SPAN_TYPE_HTTP)
                    span.metrics_tag(SPAN_KIND, SPAN_KIND_HTTP_OUT)
                    span.tag(HTTP_METHOD, method, True)
                    span.tag(HTTP_HOST, parsed.netloc, True)
                    span.tag(HTTP_PATH, parsed.path)
                    if data:
                        span.tag(HTTP_REQUEST_SIZE, str(len(data)))
                    else:
                        span.tag(HTTP_REQUEST_SIZE, '0')
                    span.tag(HTTP_URL, url)
                    span.start()
                    if tracer_config:
                        tracer_config.on_request_start(span, session)

                resp = await session._request(method, url, data=data,
                                              **kwargs)
                response_body = await resp.read()
                if span:
                    if tracer_config:
                        tracer_config.on_request_end(span, None, resp,
                                                     response_body)
                    span.finish()
                return resp
        except Exception as err:
            if span:
                if tracer_config:
                    tracer_config.on_request_end(span, err, None, None)
                span.finish()

            raise

    async def get(self, ctx: Span,
                  url: str,
                  headers: Optional[dict] = None,
                  read_timeout: Optional[float] = None,
                  conn_timeout: Optional[float] = None,
                  ssl_ctx: Optional[ssl.SSLContext] = None,
                  tracer_config: Optional[HttpClientTracerConfig] = None,
                  **kwargs
                  ) -> ClientResponse:
        return await self.request(ctx, hdrs.METH_GET, url, None,
                                  headers, read_timeout, conn_timeout, ssl_ctx,
                                  tracer_config, **kwargs)

    async def post(self, ctx: Span,
                   url: str,
                   data: Any = None,
                   headers: Optional[dict] = None,
                   read_timeout: Optional[float] = None,
                   conn_timeout: Optional[float] = None,
                   ssl_ctx: Optional[ssl.SSLContext] = None,
                   tracer_config: Optional[HttpClientTracerConfig] = None,
                   **kwargs
                   ) -> ClientResponse:
        return await self.request(ctx, hdrs.METH_POST, url, data,
                                  headers, read_timeout, conn_timeout, ssl_ctx,
                                  tracer_config, **kwargs)

    async def health(self, ctx: Span):
        pass
