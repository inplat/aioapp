from abc import ABCMeta
import io
import ssl
from typing import Type, Any, Optional
from functools import partial
import asyncio
import traceback
from urllib.parse import urlparse
from aiohttp import web, web_runner, ClientSession, hdrs, helpers
from aiohttp import ClientResponse
from aiohttp.payload import BytesPayload
from aiohttp import TCPConnector
from .app import Component
import logging
from .tracer import (Span, CLIENT, SERVER, HTTP_PATH, HTTP_METHOD, HTTP_HOST,
                     HTTP_REQUEST_SIZE, HTTP_RESPONSE_SIZE, HTTP_STATUS_CODE,
                     HTTP_URL, SPAN_TYPE, SPAN_KIND, SPAN_TYPE_HTTP,
                     SPAN_KIND_HTTP_IN, SPAN_KIND_HTTP_OUT)

access_logger = logging.getLogger('aiohttp.access')
SPAN_KEY = 'context_span'


class Handler(object):
    __metaclass__ = ABCMeta

    def __init__(self, server: 'Server') -> None:
        self.server = server

    @property
    def app(self):
        return self.server.app


class ResponseCodec:
    async def decode(self, context_span: Span,
                     response: ClientResponse) -> Any:
        raise NotImplementedError()


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
                    span.tag(SPAN_TYPE, SPAN_TYPE_HTTP)
                    span.tag(SPAN_KIND, SPAN_KIND_HTTP_IN)
                    span.tag(HTTP_PATH, request.path)
                    span.tag(HTTP_METHOD, request.method.upper())
                    _annotate_bytes(span, await request.read())
                    resp, trace_str = await self._error_handle(span, request,
                                                               handler)
                    span.tag(HTTP_STATUS_CODE, resp.status)
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
            trace = traceback.format_exc()

            if span is not None:
                span.tag('error', 'true')
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
            handle_signals=True,
            access_log_class=self.access_log_class,
            access_log_format=self.access_log_format,
            access_log=self.access_log)
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


class Client(Component):
    # TODO make pool of clients

    async def prepare(self):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    async def request(self,
                      context_span: Optional[Span],
                      method: str,
                      url: str,
                      data: Any = None,
                      headers: Optional[dict] = None,
                      read_timeout: Optional[float] = None,
                      conn_timeout: Optional[float] = None,
                      ssl_ctx: Optional[ssl.SSLContext] = None,
                      span_params: Optional[dict] = None,
                      response_codec: Optional[ResponseCodec] = None,
                      **kwargs
                      ) -> ClientResponse:
        conn = TCPConnector(ssl_context=ssl_ctx, loop=self.loop)
        headers = headers or {}
        # TODO optional propagate tracing headers
        span = None
        if context_span:
            headers.update(context_span.make_headers())
            span = context_span.new_child()
            span.start()
        try:
            async with ClientSession(loop=self.loop,
                                     headers=headers,
                                     read_timeout=read_timeout,
                                     conn_timeout=conn_timeout,
                                     connector=conn) as session:
                parsed = urlparse(url)

                if span:
                    if span_params and 'name' in span_params:
                        span.name(span_params['name'])
                    else:
                        span.name('client {0} {1}'.format('POST', parsed.path))
                    if span_params and 'endpoint_name' in span_params:
                        span.remote_endpoint(span_params['endpoint_name'])
                    else:
                        span.remote_endpoint(parsed.netloc)
                    if span_params and 'tags' in span_params:
                        for tag_name, tag_val in span_params['tags'].items():
                            span.tag(tag_name, tag_val)
                    span.kind(CLIENT)
                    span.tag(SPAN_TYPE, SPAN_TYPE_HTTP)
                    span.tag(SPAN_KIND, SPAN_KIND_HTTP_OUT)
                    span.tag(HTTP_METHOD, "POST")
                    span.tag(HTTP_HOST, parsed.netloc)
                    span.tag(HTTP_PATH, parsed.path)
                    if data:
                        span.tag(HTTP_REQUEST_SIZE, str(len(data)))
                    else:
                        span.tag(HTTP_REQUEST_SIZE, '0')
                    span.tag(HTTP_URL, url)
                    if (span_params and 'annotate_request' in span_params and
                            span_params['annotate_request']):
                        _annotate_bytes(span, data)

                resp = await session._request(method, url, data=data,
                                              **kwargs)
                response_body = await resp.read()
                resp.release()
                if span:
                    if (span_params and 'annotate_response' in span_params and
                            span_params['annotate_request']):
                        _annotate_bytes(span, data)
                    span.tag(HTTP_STATUS_CODE, resp.status)
                    span.tag(HTTP_RESPONSE_SIZE,
                             str(len(response_body)))
                if response_codec:
                    dec = await response_codec.decode(span, resp)
                    return dec
                else:
                    return resp
        except Exception as err:
            if span:
                span.finish(exception=err)
                span.tag('error.message', str(err))
            raise
        finally:
            if span:
                span.finish()

    async def get(self, context_span: Span,
                  url: str,
                  headers: Optional[dict] = None,
                  read_timeout: Optional[float] = None,
                  conn_timeout: Optional[float] = None,
                  ssl_ctx: Optional[ssl.SSLContext] = None,
                  span_params: Optional[dict] = None,
                  response_codec: Optional[ResponseCodec] = None,
                  **kwargs
                  ) -> ClientResponse:
        return await self.request(context_span, hdrs.METH_GET, url, None,
                                  headers, read_timeout, conn_timeout, ssl_ctx,
                                  span_params, response_codec, **kwargs)

    async def post(self, context_span: Span,
                   url: str,
                   data: Any = None,
                   headers: Optional[dict] = None,
                   read_timeout: Optional[float] = None,
                   conn_timeout: Optional[float] = None,
                   ssl_ctx: Optional[ssl.SSLContext] = None,
                   span_params: Optional[dict] = None,
                   response_codec: Optional[ResponseCodec] = None,
                   **kwargs
                   ) -> ClientResponse:
        return await self.request(context_span, hdrs.METH_POST, url, data,
                                  headers, read_timeout, conn_timeout, ssl_ctx,
                                  span_params, response_codec, **kwargs)


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
