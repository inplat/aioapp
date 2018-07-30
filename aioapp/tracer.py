from typing import Optional, Any, Callable
from yarl import URL
import time
import re
import asyncio
import aioapp.app  # noqa
import aiozipkin as az
import aiozipkin.tracer as azt
import aiozipkin.span as azs
import aiozipkin.helpers as azh
import aiozipkin.utils as azu
from .misc import async_call

STATS_CLEAN_NAME_RE = re.compile('[^0-9a-zA-Z_.-]')
STATS_CLEAN_TAG_RE = re.compile('[^0-9a-zA-Z_=.-]')

DRIVER_ZIPKIN = 'zipkin'

CLIENT = 'CLIENT'
SERVER = 'SERVER'
HTTP_HOST = 'http.host'
HTTP_METHOD = 'http.method'
HTTP_PATH = 'http.path'
HTTP_REQUEST_SIZE = 'http.request.size'
HTTP_RESPONSE_SIZE = 'http.response.size'
HTTP_STATUS_CODE = 'http.status_code'
HTTP_URL = 'http.url'
SPAN_TYPE = 'span_type'
# SPAN_TYPE_HTTP = 'http'
# SPAN_TYPE_POSTGRES = 'postgres'
# SPAN_TYPE_REDIS = 'redis'
# SPAN_TYPE_AMQP = 'amqp'
# SPAN_TYPE_TELEGRAM = 'telegram'

SPAN_KIND = 'span_kind'
# SPAN_KIND_HTTP_IN = 'in'
# SPAN_KIND_HTTP_OUT = 'out'
# SPAN_KIND_POSTRGES_ACQUIRE = 'acquire'
# SPAN_KIND_POSTRGES_QUERY = 'query'
# SPAN_KIND_REDIS_ACQUIRE = 'acquire'
# SPAN_KIND_REDIS_QUERY = 'query'
# SPAN_KIND_REDIS_PUBSUB = 'pubsub'
# SPAN_KIND_AMQP_OUT = 'out'
# SPAN_KIND_AMQP_ACK = 'ack'
# SPAN_KIND_AMQP_NACK = 'nack'
# SPAN_KIND_AMQP_IN = 'in'
# SPAN_KIND_TELEGRAM_OUT = 'out'
# SPAN_KIND_TELEGRAM_IN = 'in'

ERROR = 'error'
LOCAL_COMPONENT = 'lc'

CLIENT_ADDR = 'ca'
MESSAGE_ADDR = 'ma'
SERVER_ADDR = 'sa'


class Span:
    def __init__(self,
                 tracer: Optional['Tracer'],
                 metrics: Optional['InfluxMetrics'],
                 trace_id: str, id: Optional[str] = None,
                 parent_id: Optional[str] = None,
                 sampled: Optional[bool] = None,
                 debug: Optional[bool] = False,
                 shared: bool = False,
                 skip: bool = False) -> None:
        self.tracer = tracer
        self.metrics = metrics
        self.trace_id = trace_id
        self.id = id
        self.parent_id = parent_id
        self.sampled = sampled
        self.debug = debug
        self.shared = shared
        self._name: Optional[str] = None
        self._kind: Optional[str] = None
        self._tags: dict = {}
        self._tags_metrics: dict = {}
        self._annotations: list = []
        self._remote_endpoint: Optional[tuple] = None
        self._start_stamp: Optional[int] = None
        self._finish_stamp: Optional[int] = None
        self._span: Any = None
        self._skip = skip

    def skip(self):
        self._skip = True

    def make_headers(self):
        headers = {
            azh.TRACE_ID_HEADER: self.trace_id,
            azh.SPAN_ID_HEADER: self.id,
            azh.FLAGS_HEADER: '0',
            azh.SAMPLED_ID_HEADER: '1' if self.sampled else '0',
        }
        if self.parent_id is not None:
            headers[azh.PARENT_ID_HEADER] = self.parent_id
        return headers

    def new_child(self, name: Optional[str] = None,
                  kind: Optional[str] = None) -> 'Span':
        span = Span(
            tracer=self.tracer,
            metrics=self.metrics,
            trace_id=self.trace_id,
            id=azu.generate_random_64bit_string(),
            parent_id=self.id,
            sampled=self.sampled,
            debug=self.debug,
            skip=self._skip
        )
        if name is not None:
            span.name(name)
        if kind:
            span.kind(kind)
        return span

    def start(self, ts: Optional[float] = None):
        now = time.time()
        self._start_stamp = int((ts or now) * 1000000)
        return self

    def finish(self, ts: Optional[float] = None,
               exception: Optional[Exception] = None) -> 'Span':
        now = time.time()
        self._finish_stamp = int((ts or now) * 1000000)
        if exception is not None:
            self.tag('error', 'true', True)
            self.tag('error.message', str(exception))

        if self.tracer is not None:
            if self.tracer.tracer_driver == DRIVER_ZIPKIN:
                _span = self.get_zipkin_span()
                if self._start_stamp is not None:
                    _span.start(ts=self._start_stamp / 1000000)
                    for _tag_name, _tag_val in self._tags.items():
                        _span.tag(_tag_name, _tag_val)
                    for _ann, _ann_stamp in self._annotations:
                        _span.annotate(_ann, _ann_stamp / 1000000)
                    if self._kind:
                        _span.kind(self._kind)
                    if self._name:
                        _span.name(self._name)
                    if self._remote_endpoint:
                        _span.remote_endpoint(self._remote_endpoint[0],
                                              ipv4=self._remote_endpoint[1],
                                              ipv6=self._remote_endpoint[2],
                                              port=self._remote_endpoint[3])
                    _span.finish(ts=now, exception=exception)

        if self.metrics and not self._skip:
            self.metrics.send(self)

        if self.tracer is not None and self.tracer.on_span_finish is not None:
            call = self.tracer.on_span_finish(self)
            if asyncio.iscoroutine(call):
                asyncio.ensure_future(call, loop=self.tracer.loop)

        return self

    def tag(self, key: str, value: str, metrics: bool = False) -> 'Span':
        self._tags[key] = str(value)
        if metrics:
            self._tags_metrics[key] = self._tags[key]
        return self

    def metrics_tag(self, key: str, value: str) -> 'Span':
        self._tags_metrics[key] = str(value)
        return self

    def annotate(self, value: str, ts: Optional[float] = None) -> 'Span':
        self._annotations.append((value, int((ts or time.time()) * 1000000)))
        return self

    def kind(self, span_kind: str) -> 'Span':
        self._kind = span_kind
        return self

    def name(self, span_name: str) -> 'Span':
        self._name = span_name
        return self

    def remote_endpoint(self,
                        servce_name: str, *,
                        ipv4: Optional[str] = None,
                        ipv6: Optional[str] = None,
                        port: Optional[int] = None) -> 'Span':
        self._remote_endpoint = (servce_name, ipv4, ipv6, port)
        return self

    def __enter__(self) -> 'Span':
        self.start()
        return self

    def __exit__(self, exception_type, exception_value, traceback) -> None:
        self.finish(exception=exception_value)

    def get_zipkin_span(self):
        tracer = self.tracer.tracer
        span = tracer.to_span(
            azs.TraceContext(
                trace_id=self.trace_id,
                parent_id=self.parent_id,
                span_id=self.id,
                sampled=self.sampled if not self._skip else False,
                debug=self.debug,
                shared=self.shared

            ))
        return span

    def __str__(self):
        if self._start_stamp is not None and self._finish_stamp is not None:
            duration = (' in %s ms' % (
                (self._finish_stamp - self._start_stamp) / 1000.,))
        else:
            duration = ''
        return 'AioappSpan: %s%s' % (self._name, duration)


class Tracer:

    def __init__(self, app: 'aioapp.app.Application',
                 loop: asyncio.AbstractEventLoop) -> None:
        self.app = app
        self.loop = loop
        self.tracer: Optional[az.Tracer] = None
        self.metrics: Optional[InfluxMetrics] = None
        self.tracer_driver: Optional[str] = None
        self.default_sampled: Optional[bool] = None
        self.default_debug: Optional[bool] = None
        self.on_span_finish: Optional[Callable] = None

    def new_trace(self, sampled: Optional[bool] = None,
                  debug: Optional[bool] = None,
                  skip: bool = False):
        if sampled is None:
            sampled = self.default_sampled
        if debug is None:
            debug = self.default_debug
        span = Span(
            tracer=self,
            metrics=self.metrics,
            trace_id=azu.generate_random_128bit_string(),
            id=azu.generate_random_64bit_string(),
            sampled=sampled,
            debug=debug,
            skip=skip)
        return span

    def new_trace_from_headers(self, headers: dict, skip: bool = False):
        if headers:
            headers = {k.lower(): v for k, v in headers.items()}
        else:
            headers = {}

        sampled = azh.parse_sampled(headers)
        if sampled is None:
            sampled = self.default_sampled
        debug = azh.parse_debug(headers)
        if debug is None:
            debug = self.default_debug

        if not all(h in headers for h in (
                azh.TRACE_ID_HEADER.lower(), azh.SPAN_ID_HEADER.lower())):
            return self.new_trace(sampled=sampled,
                                  debug=debug)

        trace_id = headers.get(azh.TRACE_ID_HEADER.lower())
        if not trace_id:
            trace_id = azu.generate_random_128bit_string()

        span_id = headers.get(azh.SPAN_ID_HEADER.lower())
        if not span_id:
            span_id = azu.generate_random_64bit_string()

        span = Span(
            tracer=self,
            metrics=self.metrics,
            trace_id=trace_id,
            id=span_id,
            parent_id=headers.get(azh.PARENT_ID_HEADER.lower(), None),
            sampled=sampled,
            shared=False,
            debug=debug,
            skip=skip
        )

        return span

    def setup_tracer(self, driver: str, name: str, addr: str,
                     sample_rate: float, send_interval: float,
                     default_sampled: bool = True,
                     default_debug: bool = False) -> None:
        if driver != DRIVER_ZIPKIN:
            raise UserWarning('Unsupported tracer driver')

        self.tracer_driver = driver
        self.default_sampled = default_sampled
        self.default_debug = default_debug

        endpoint = az.create_endpoint(name)
        sampler = az.Sampler(sample_rate=sample_rate)
        transport = azt.Transport(str(URL(addr).with_path('/api/v2/spans')),
                                  send_interval=send_interval,
                                  loop=self.loop)
        self.tracer = az.Tracer(transport, sampler, endpoint)

    def setup_metrics(self, driver: str, addr: str, name: str) -> None:
        if driver != 'telegraf-influx':
            raise UserWarning('Unsupported metrics driver')
        url = URL(addr)
        self.metrics = InfluxMetrics(self, url, name, self.loop)

    async def close(self):
        if self.tracer:
            await self.tracer.close()
        if self.metrics:
            await self.metrics.close()


class InfluxMetrics:

    def __init__(self, tracer: Tracer, url: URL, name: Optional[str],
                 loop: asyncio.AbstractEventLoop) -> None:
        self.tracer = tracer
        self.name = name
        self.url = url
        self.loop = loop
        self.transport = None
        self.closing = False
        self._connect()

    def _connect(self):
        if self.url.scheme == 'udp':
            asyncio.ensure_future(self._async_conn(), loop=self.loop)
        else:
            raise NotImplementedError(str(self.url))

    async def _async_conn(self):
        connect = self.loop.create_datagram_endpoint(
            lambda: self,
            remote_addr=(self.url.host, self.url.port))
        self.transport, self.protocol = await connect

    def _escape_name(self, name):
        name = name.replace('\n', '')
        name = name.replace(',', '\\,')
        name = name.replace(' ', '\\ ')
        return name

    def send(self, span: Span):
        if self.transport:
            if SPAN_TYPE in span._tags_metrics:
                name = self._escape_name(span._tags_metrics.pop(SPAN_TYPE))
            else:
                name = self._escape_name(span._name)
            if self.name:
                name = self.name + name
            tags = []
            for key, value in span._tags_metrics.items():
                tag = '%s=%s' % (self._escape_name(key),
                                 self._escape_name(value))
                tags.append(tag)

            duration = span._finish_stamp - span._start_stamp

            if tags:
                name = name + ',' + (','.join(tags))
            line = '%s duration=%s %s\n' % (name,
                                            duration,
                                            span._finish_stamp * 1000)
            self.transport.sendto(line.encode())

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        pass

    def error_received(self, exc):
        self.tracer.app.log_err(exc)

    def connection_lost(self, exc):
        self.tracer.app.log_err(exc)
        self.transport = None
        if not self.closing:
            async_call(self.loop, self._connect)

    async def close(self):
        self.closing = True
        if self.transport:
            self.transport.close()
