import time
import json
import asyncio
import aioamqp
import traceback
from datetime import timedelta
from aioamqp.exceptions import ChannelClosed
from aioamqp.envelope import Envelope  # noqa
from aioamqp.properties import Properties  # noqa
from typing import List
from .app import Component
from .misc import mask_url_pwd, async_call, get_func_params
from .error import (PrepareError, TaskFormatError, UnknownTaskError,
                    BadTaskParamsError)
import aiozipkin.aiohttp_helpers as azah  # noqa
import aiozipkin.helpers as azh  # noqa
import aiozipkin.span as azs  # noqa


class Consumer:
    def __init__(self, tm, url, connect_max_attempts, connect_retry_delay,
                 queue, heartbeat):
        self.tm = tm
        self.url = url
        self.connect_max_attempts = connect_max_attempts
        self.connect_retry_delay = connect_retry_delay
        self.queue = queue
        self.heartbeat = heartbeat
        self._transport = None
        self._protocol = None

    async def connect(self):
        self.tm.app.log_info("Connecting to %s" % self.tm.masked_url)
        for i in range(self.connect_max_attempts):
            try:
                await self._connect()
                self.tm.app.log_info("Connected to %s" % self.tm.masked_url)
                return
            except Exception as e:
                self.tm.app.log_err(e)
                await asyncio.sleep(self.connect_retry_delay,
                                    loop=self.tm.app.loop)
        raise PrepareError("Could not connect to %s" % self.tm.masked_url)

    async def _connect(self):
        (self._transport,
         self._protocol) = await aioamqp.from_url(self.url,
                                                  on_error=self._con_error,
                                                  heartbeat=self.heartbeat)

    async def disconnect(self):
        if self._protocol:
            self.tm.app.log_info("Disconnecting from %s" % self.tm.masked_url)
            await self._protocol.close()

    async def _con_error(self, error):
        print("AMQP ERROR", error)


ConsumerList = List[Consumer]


class AbstractHandler:

    def __init__(self, app):
        """
        :type app: api_subscribe.core.app.BaseApplication
        """
        self.app = app

    def _tasks(self):
        # return {"some_task": self.some_task}
        raise NotImplementedError()


class TaskManager(Component):
    def __init__(self, amqp_url, concurrency, connect_max_attempts,
                 connect_retry_delay, queue, scheduled_queue, handler):
        super(TaskManager, self).__init__()
        self.url = amqp_url
        self.concurrency = concurrency
        self.connect_max_attempts = connect_max_attempts
        self.connect_retry_delay = connect_retry_delay
        self.queue = queue
        self.scheduled_queue = scheduled_queue
        self.heartbeat = 5
        if not isinstance(handler, AbstractHandler):
            raise UserWarning()
        self.handler = handler
        self._transport = None
        self._protocol = None
        self._pub_ch = None  # publisher channel
        self._pub_lock = None  # publisher channel
        self._cons_chs = []  # consumer channels list
        self._cons_locks = {}  # consumer locks <consumer tag>: <lock>
        self._cons_tags = {}  # map of <consumer tag>: <channel>
        self._shutting_down = False

        self._i = 0

    @property
    def _masked_url(self):
        return mask_url_pwd(self.url)

    async def prepare(self):
        self.app.log_info("Connecting to %s" % self._masked_url)
        for i in range(self.connect_max_attempts):
            try:
                await self._connect()
                self.app.log_info("Connected to %s" % self._masked_url)
                return
            except Exception as e:
                self.app.log_err(str(e))
                await asyncio.sleep(self.connect_retry_delay,
                                    loop=self.app.loop)
        raise PrepareError("Could not connect to %s" % self._masked_url)
        # for consumer in self._consumers:
        #     await consumer.connect()

    async def _safe_declare(self, queue, arguments=None):
        ch = await self._protocol.channel()
        try:
            await ch.queue_declare(queue, passive=False, durable=True,
                                   arguments=arguments)
        except ChannelClosed as e:
            if e.code == 406:
                # если у очереди не совпадают атрибуты, то игнор
                pass
            else:
                raise
        finally:
            if ch.is_open:
                await ch.close()

    async def _connect(self):
        await self._cleanup()
        (self._transport,
         self._protocol) = await aioamqp.from_url(self.url,
                                                  on_error=self._con_error,
                                                  heartbeat=self.heartbeat)

        await self._safe_declare(self.queue)
        await self._safe_declare(self.scheduled_queue, {
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": self.queue
        })

        self._pub_ch = await self._protocol.channel()
        self._pub_lock = asyncio.Lock()

        for i in range(self.concurrency):
            ch = await self._protocol.channel()
            self._cons_chs.append(ch)
            await ch.basic_qos(prefetch_count=1)
            res = await ch.basic_consume(self._handle_message,
                                         queue_name=self.queue)
            self._cons_locks[res.get('consumer_tag')] = asyncio.Lock()
            self._cons_tags[res.get('consumer_tag')] = ch

    async def _stop_consuming(self):
        for lock in self._cons_locks.values():
            await lock.acquire()
        if self._pub_lock:
            self._pub_lock.acquire()
        for consumer_tag, channel in self._cons_tags.items():
            if channel.is_open:
                await channel.basic_cancel(consumer_tag)

    async def _cleanup(self):
        chs = self._cons_chs
        if self._pub_ch:
            chs.append(self._pub_ch)
        for ch in chs:
            try:
                await ch.close()
            except ChannelClosed:
                pass
            except Exception as e:
                self.app.log_err(e)
        self._cons_chs = []
        self._cons_locks = {}
        self._cons_tags = {}
        self._pub_ch = None
        self._pub_lock = None
        if self._protocol:
            try:
                await self._protocol.close()
            except Exception as e:
                self.app.log_err(e)
            self._protocol = None
            self._transport = None

    async def _handle_message(self, channel, body, envelope, properties):
        """
        :type body: bytes
        :type envelope: Envelope
        :type properties: Properties
        """
        context_span: azs.SpanAbc = self.app.tracer.new_trace(sampled=True,
                                                              debug=False)

        context_span.name('amqp:message')
        context_span.kind(azh.SERVER)
        context_span.tag('amqp.routing_key', envelope.routing_key)
        context_span.tag('amqp.exchange_name', envelope.exchange_name)
        context_span.tag('amqp.headers', properties.headers)
        context_span.tag('amqp.delivery_mode', properties.delivery_mode)
        context_span.tag('amqp.expiration', properties.expiration)
        _annotate_bytes(context_span, body)

        async def _amsg(context_span, channel, body, envelope, properties):
            with context_span:
                try:
                    with (await self._cons_locks[envelope.consumer_tag]):
                        await channel.basic_client_ack(envelope.delivery_tag)
                        context_span.tag('acknowledged', 'true')
                        try:
                            task = Task.load(self, body, context_span)
                            await task.run(**task.params)
                        except Exception as e:
                            self.app.log_err(e)
                except Exception as err:
                    context_span.tag('error', 'true')
                    context_span.tag('error.message', err)
                    context_span.annotate(traceback.format_exc())
        async_call(self.loop, _amsg, context_span, channel, body, envelope,
                   properties)

    async def _send_message(self, context_span, payload, exchange_name,
                            routing_key, properties=None, mandatory=False,
                            immediate=False):
        start = time.time()
        with (await self._pub_lock):
            context_span.tag('amqp.acquire_time_ms',
                             1000 * time.time() - start)
            await self._pub_ch.basic_publish(payload, exchange_name,
                                             routing_key, properties,
                                             mandatory, immediate)

    async def run(self, context_span, name, params, delay=None):
        """
        :type context_span: azs.SpanAbc
        :type name: str
        :type delay: datetime.timedelta
        :type params: dict
        """
        task = Task(self, name, params, attempt=1)
        await task.schedule(context_span, delay)

    async def _con_error(self, error):
        if self._shutting_down:
            return

        async def _reconnect():
            try:
                await self._connect()
            except Exception as e:
                self.app.log_err(e)
                async_call(self.loop, _reconnect,
                           delay=self.connect_retry_delay)
        print('*'*80, error, type(error))
        self.app.log_err(error)
        async_call(self.loop, _reconnect, delay=self.connect_retry_delay)

    async def start(self):
        pass

    async def stop(self):
        self._shutting_down = True
        await self._stop_consuming()
        await self._cleanup()


class Task:

    def __init__(self, tm, name, params, attempt=1) -> None:
        """
        :type tm: TaskManager
        :type name: str
        :type params: dict
        :type attempt: int
        """
        self.body = None
        self.context_span = None  # type: azs.SpanAbc
        self.tm = tm
        self.app = tm.app
        self.attempt = attempt
        if name is None or name[0:1] == '_' or not isinstance(name, str):
            raise UnknownTaskError("Unknown task")
        if params is not None and not isinstance(params, dict):
            raise BadTaskParamsError("Bad task parameters")
        self.name = name

        try:
            if params is not None:
                self.params = get_func_params(self.run, params)
            else:
                self.params = {}
        except Exception as e:
            raise BadTaskParamsError(str(e))

    def __str__(self):
        return "Task(%s, %s)" % (self.name, self.params)

    @staticmethod
    def load(tm, body, context_span):
        """
        :type tm: TaskManager
        :type body: bytes
        :type context_span: azs.SpanAbc
        :return:
        """
        name, params, attempt = Task.decode(context_span, body)
        tasks = tm.handler._tasks()
        task_cls = tasks.get(name)
        if task_cls is None:
            raise UnknownTaskError("Unknown task %s" % name)

        task = task_cls(tm, name, params, attempt=attempt)
        task.body = body
        task.context_span = context_span
        return task

    @staticmethod
    def decode(context_span, body):
        """
        :type context_span: azs.SpanAbc
        :type body: bytes
        :return:
        """
        try:
            data = json.loads(body.decode("UTF-8"))
        except Exception:
            raise TaskFormatError("Bad task encoding: %s" % str(body))
        if not isinstance(data, dict):
            raise TaskFormatError("Bad task format: %s" % str(body))
        name = data.get("name")
        context_span.tag('subscr.task_name', name)
        context_span.name('task:%s' % name)
        params = data.get("params")
        context_span.annotate(repr(params))
        attempt = int(data.get("attempt") or 1)
        context_span.tag('subscr.task_attempt', str(attempt))
        if name is None or not isinstance(name, str):
            raise UnknownTaskError("Unknown task")
        if params is not None and not isinstance(params, dict):
            raise BadTaskParamsError("Bad task parameters")
        return name, params, attempt

    @staticmethod
    def encode(name, params, attempt=1):
        req = {
            "name": name,
            "params": params,
            "attempt": attempt,
        }
        return json.dumps(req).encode("UTF8")

    async def schedule(self, context_span, delay=None):
        """
        :type context_span: azs.SpanAbc
        :type delay: datetime.timedelta
        """
        properties = {"delivery_mode": 2}
        if delay is not None:
            if not isinstance(delay, timedelta):
                raise UserWarning()
            queue = self.tm.scheduled_queue
            properties["expiration"] = str(
                max(0, int(delay.total_seconds() * 1000)))
        else:
            queue = self.tm.queue
        with context_span.tracer.new_child(context_span.context) as span:
            span.name('schedule:' + self.name)
            span.tag('subscr.task_name', self.name)
            span.tag('amqp:queue', queue)
            span.tag('amqp:expiration', properties.get('expiration', 'null'))
            span.tag('amqp:delivery_mode',
                     properties.get('delivery_mode', 'null'))
            payload = Task.encode(self.name, self.params)
            _annotate_bytes(span, payload)
            await self.tm._send_message(
                span,
                payload,
                '',
                queue,
                properties)

    async def run(self, **kwargs):  # type: ignore
        raise NotImplementedError()


class TaskRetry(Exception):
    pass


def _annotate_bytes(span, data):
    try:
        data_str = data.decode("UTF8")
    except Exception:
        data_str = str(data)
    span.annotate(data_str or 'null')
