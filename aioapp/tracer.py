from typing import Optional, Any
import time
import re
import asyncio
import aiozipkin as az
import aiozipkin.tracer as azt
import aiozipkin.span as azs
import aiozipkin.helpers as azh
import aiozipkin.utils as azu

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

ERROR = 'error'
LOCAL_COMPONENT = 'lc'

CLIENT_ADDR = 'ca'
MESSAGE_ADDR = 'ma'
SERVER_ADDR = 'sa'


class Span:
    def __init__(self,
                 tracer: 'Tracer',
                 trace_id: str, id: Optional[str] = None,
                 parent_id: Optional[str] = None,
                 sampled: Optional[bool] = None, debug: bool = False,
                 shared: bool = False) -> None:
        self.tracer = tracer
        self.trace_id = trace_id
        self.id = id
        self.parent_id = parent_id
        self.sampled = sampled
        self.debug = debug
        self.shared = shared
        self._name: Optional[str] = None
        self._kind: Optional[str] = None
        self._tags: dict = {}
        self._annotations: list = []
        self._remote_endpoint: tuple = None
        self._start_stamp: Optional[int] = None
        self._finish_stamp: Optional[int] = None
        self._span: Any = None

        if self.tracer.tracer_driver == DRIVER_ZIPKIN:
            self._span = self.get_zipkin_span()

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
            trace_id=self.trace_id,
            id=azu.generate_random_64bit_string(),
            parent_id=self.id,
            sampled=self.sampled,
            debug=self.debug
        )
        if name is not None:
            span.name(name)
        if kind:
            span.kind(kind)
        return span

    def start(self, ts: Optional[float] = None):
        now = time.time()
        self._start_stamp = int((ts or now) * 1000000)
        if self._span and self.tracer.tracer_driver == DRIVER_ZIPKIN:
            span: azs.Span = self._span
            span.start(ts=now)

        return self

    def finish(self, ts: Optional[float] = None,
               exception: Optional[Exception] = None) -> 'Span':
        now = time.time()
        self._finish_stamp = int((ts or now) * 1000000)
        if exception is not None:
            self.tag('error', str(exception))
            self.tag('error.message', str(exception))
        if self._span and self.tracer.tracer_driver == DRIVER_ZIPKIN:
            span: azs.Span = self._span
            span.finish(ts=now, exception=exception)
        return self

    def tag(self, key: str, value: str) -> 'Span':
        self._tags[key] = str(value)
        if self._span and self.tracer.tracer_driver == DRIVER_ZIPKIN:
            span: azs.Span = self._span
            span.tag(key, value)
        return self

    def annotate(self, value: str, ts: Optional[float] = None) -> 'Span':
        self._annotations.append((value, int((ts or time.time()) * 1000000)))
        if self._span and self.tracer.tracer_driver == DRIVER_ZIPKIN:
            span: azs.Span = self._span
            span.annotate(value, ts)
        return self

    def kind(self, span_kind: str) -> 'Span':
        self._kind = span_kind
        if self._span and self.tracer.tracer_driver == DRIVER_ZIPKIN:
            span: azs.Span = self._span
            span.kind(span_kind)
        return self

    def name(self, span_name: str) -> 'Span':
        self._name = span_name
        if self._span and self.tracer.tracer_driver == DRIVER_ZIPKIN:
            span: azs.Span = self._span
            span.name(span_name)
        return self

    def remote_endpoint(self,
                        servce_name: str, *,
                        ipv4: Optional[str] = None,
                        ipv6: Optional[str] = None,
                        port: Optional[int] = None) -> 'Span':
        self._remote_endpoint = (servce_name, ipv4, ipv6, port)
        if self._span and self.tracer.tracer_driver == DRIVER_ZIPKIN:
            span: azs.Span = self._span
            span.remote_endpoint(servce_name, ipv4=ipv4, ipv6=ipv6, port=port)
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
                sampled=self.sampled,
                debug=self.debug,
                shared=self.shared

            ))
        return span


class Tracer:

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.tracer = None
        self.tracer_driver: Optional[str] = None
        self.default_sampled: Optional[bool] = None
        self.default_debug: Optional[bool] = None

    def new_trace(self, sampled: Optional[bool] = None,
                  debug: Optional[bool] = None):
        if sampled is None:
            sampled = self.default_sampled
        if debug is None:
            debug = self.default_debug

        span = Span(
            tracer=self,
            trace_id=azu.generate_random_128bit_string(),
            id=azu.generate_random_64bit_string(),
            sampled=sampled,
            debug=debug)
        return span

    def new_trace_from_headers(self, headers: dict):
        headers = {k.lower(): v for k, v in headers.items()}

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
            trace_id=trace_id,
            id=span_id,
            parent_id=headers.get(azh.PARENT_ID_HEADER.lower(), None),
            sampled=sampled,
            shared=False,
            debug=debug,
        )

        return span

    def setup_tracer(self, driver: str, name: str, addr: str,
                     sample_rate: float, send_inteval: float,
                     default_sampled: Optional[bool] = None,
                     default_debug: Optional[bool] = None) -> None:
        if driver != DRIVER_ZIPKIN:
            raise UserWarning('Unsupported tracer driver')

        self.tracer_driver = driver
        self.default_sampled = default_sampled
        self.default_debug = default_debug

        endpoint = az.create_endpoint(name)
        sampler = az.Sampler(sample_rate=sample_rate)
        transport = azt.Transport(addr, send_inteval=send_inteval,
                                  loop=self.loop)
        self.tracer = az.Tracer(transport, sampler, endpoint)

    def setup_metrics(self, driver: str, addr: str, name: str) -> None:
        if driver != 'telegraf-influx':
            raise UserWarning('Unsupported metrics driver')

    async def close(self):
        if self.tracer:
            await self.tracer.close()

#
# class Tracer(azt.Tracer):
#     async def stop(self):
#         await self.close()

#
# class TracerTransport(azt.Transport):
#     def __init__(self, app, driver, addr, metrics_diver, metrics_addr,
#                  metrics_name, send_inteval, loop):
#         """
#         :type tracer: str
#         :type tracer_url: str
#         :type statsd_addr: str
#         :type statsd_prefix: str
#         :type send_inteval: float
#         :type loop: asyncio.AbstractEventLoop
#         """
#         if driver is not None and driver != 'zipkin':
#             raise UserWarning('Unsupported tracer driver')
#         if metrics_diver is not None and metrics_diver != 'statsd':
#             raise UserWarning('Unsupported metrics driver')
#
#         addr = addr or ''
#         super(TracerTransport, self).__init__(addr,
#                                               send_inteval=send_inteval,
#                                               loop=loop)
#         self.app = app
#         self.loop = loop
#         self._driver = driver
#         self._metrics_diver = metrics_diver
#         self._metrics_addr = metrics_addr
#         self._metrics_name = metrics_name
#
#         self.stats = None
#         if metrics_diver == 'statsd':
#             addr = metrics_addr.split(':')
#             host = addr[0]
#             port = int(addr[1]) if len(addr) > 1 else 8125
#             self.stats = StatsdClient(host, port)
#             asyncio.ensure_future(self.stats.run(), loop=loop)
#
#     async def close(self):
#         if self.stats:
#             try:
#                 await asyncio.sleep(.001, loop=self.loop)
#                 await self.stats.stop()
#             except Exception as e:
#                 self.app.log_err(e)
#         await super(TracerTransport, self).close()
#
#     async def _send(self):
#         data = self._queue[:]
#
#         try:
#             if self.stats:
#                 await self._send_to_statsd(data)
#         except Exception as e:
#             self.app.log_err(e)
#
#         try:
#             if self._driver == 'zipkin':
#                 # TODO отправить pull request в aiozipkin: не отправлять
#                 # TODO запрос, если self._queue пуста
#                 await super(TracerTransport, self)._send()
#             else:
#                 self._queue = []
#         except Exception as e:
#             self.app.log_err(e)
#
#     async def _send_to_statsd(self, data):
#         if self.stats:
#             for rec in data:
#                 tags = []
#                 t = rec['tags']
#                 if azc.HTTP_PATH in t and 'kind' in rec:
#                     name = 'http'
#                     if rec["kind"] == 'SERVER':
#                         tags.append(('kind', 'in'))
#                     else:
#                         tags.append(('kind', 'out'))
#
#                     copy_tags = {
#                         azc.HTTP_STATUS_CODE: 'status',
#                         azc.HTTP_METHOD: 'method',
#                         azc.HTTP_HOST: 'host',
#                     }
#                     for tag_key, tag_name in copy_tags.items():
#                         if tag_key in t:
#                             tags.append((tag_name, t[tag_key]))
#
#                 elif rec['name'].startswith('db:'):
#                     name = 'db'
#                     tags.append(('kind', rec['name'][len('db:'):]))
#                 elif rec['name'].startswith('redis:'):
#                     name = 'redis'
#                     tags.append(('kind', rec['name'][len('redis:'):]))
#                 else:
#                     name = rec['name']
#
#                 name = name.replace(' ', '_').replace(':', '_')
#                 name = self._metrics_name + name
#                 name = STATS_CLEAN_NAME_RE.sub('', name)
#
#                 if len(tags) > 0:
#                     for tag in tags:
#                         t = tag[1].replace(':', '-')
#                         t = STATS_CLEAN_TAG_RE.sub('', t)
#                         name += ',' + tag[0] + "=" + t
#                 self.stats.send_timer(name,
#                                       int(round(rec["duration"] / 1000)),
#                                       rate=1.0)
