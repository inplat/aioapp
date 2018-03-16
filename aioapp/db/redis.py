import asyncio
import traceback
import aioredis
from ..app import Component
from ..error import PrepareError
from ..tracer import (Span, CLIENT, SPAN_TYPE, SPAN_KIND, SPAN_TYPE_REDIS,
                      SPAN_KIND_REDIS_ACQUIRE, SPAN_KIND_REDIS_QUERY,
                      SPAN_KIND_REDIS_PUBSUB)


class Redis(Component):
    def __init__(self, url: str, pool_min_size: int = 1,
                 pool_max_size: int = 10,
                 connect_max_attempts: int = 10,
                 connect_retry_delay: float = 1.0) -> None:
        super(Redis, self).__init__()
        self.url = url
        self.pool_min_size = pool_min_size
        self.pool_max_size = pool_max_size
        self.connect_max_attempts = connect_max_attempts
        self.connect_retry_delay = connect_retry_delay
        self.pool = None

    async def prepare(self):
        for i in range(self.connect_max_attempts):
            try:
                await self._connect()
                return
            except Exception as e:
                self.app.log_err(str(e))
                await asyncio.sleep(self.connect_retry_delay)
        raise PrepareError("Could not connect to %s" % self.url)

    async def _connect(self):
        self.app.log_info("Connecting to %s" % self.url)
        self.pool = await aioredis.create_pool(self.url,
                                               minsize=self.pool_min_size,
                                               maxsize=self.pool_max_size,
                                               loop=self.loop)
        self.app.log_info("Connected to %s" % self.url)

    async def start(self):
        pass

    async def stop(self):
        if self.pool:
            self.app.log_info("Disconnecting from %s" % self.url)
            self.pool.close()
            await self.pool.wait_closed()

    def connection(self,
                   context_span: Span) -> 'ConnectionContextManager':
        return ConnectionContextManager(self, context_span)

    async def execute(self, context_span: Span, id: str,
                      command: str, *args):
        async with self.connection(context_span) as conn:
            return await conn.execute(context_span, id, command, *args)


class ConnectionContextManager:
    def __init__(self, redis, context_span) -> None:
        self._redis = redis
        self._conn = None
        self._context_span = context_span

    async def __aenter__(self) -> 'Connection':
        span = None
        if self._context_span:
            span = self._context_span.new_child()
            span.start()
        try:
            if span:
                span.kind(CLIENT)
                span.name("redis:Acquire")
                span.tag(SPAN_TYPE, SPAN_TYPE_REDIS)
                span.tag(SPAN_KIND, SPAN_KIND_REDIS_ACQUIRE)
                span.remote_endpoint("redis")
                span.tag('redis.size_before', self._redis.pool.size)
                span.tag('redis.free_before', self._redis.pool.freesize)
            self._conn = await self._redis.pool.acquire()
        except Exception as err:
            if span:
                span.tag('error.message', str(err))
                span.annotate(traceback.format_exc())
                span.finish(exception=err)
            raise
        finally:
            if span:
                span.finish()
        c = Connection(self._redis, self._conn)
        return c

    async def __aexit__(self, exc_type, exc, tb):
        self._redis.pool.release(self._conn)


class Connection:
    def __init__(self, redis, conn) -> None:
        """
        :type redis: Redis
        """
        self._redis = redis
        self._conn = conn
        self.loop = self._redis.loop

    @property
    def pubsub_channels(self):
        return self._conn.pubsub_channels

    async def execute(self, context_span: Span, id: str,
                      command: str, *args):
        span = None
        if context_span:
            span = context_span.new_child()
            span.start()
        try:
            if span:
                span.kind(CLIENT)
                span.name("redis:%s" % id)
                span.tag(SPAN_TYPE, SPAN_TYPE_REDIS)
                span.tag(SPAN_KIND, SPAN_KIND_REDIS_QUERY)
                span.remote_endpoint("redis")
                span.tag("redis.command", command)
                span.annotate(repr(args))
            res = await self._conn.execute(command, *args)
        except Exception as err:
            if span:
                span.tag('error.message', str(err))
                span.annotate(traceback.format_exc())
                span.finish(exception=err)
            raise
        finally:
            if span:
                span.finish()
        return res

    async def execute_pubsub(self, context_span: Span, id: str,
                             command, *channels_or_patterns):
        span = None
        if context_span:
            span = context_span.new_child()
            span.start()
        try:
            if span:
                span.kind(CLIENT)
                span.name("redis:%s" % id)
                span.tag(SPAN_TYPE, SPAN_TYPE_REDIS)
                span.tag(SPAN_KIND, SPAN_KIND_REDIS_PUBSUB)
                span.remote_endpoint("redis")
                span.tag("redis.pubsub", command)
                span.annotate(repr(channels_or_patterns))
            res = await self._conn.execute_pubsub(command,
                                                  *channels_or_patterns)
        except Exception as err:
            if span:
                span.tag('error.message', str(err))
                span.annotate(traceback.format_exc())
                span.finish(exception=err)
            raise
        finally:
            if span:
                span.finish()
        return res
