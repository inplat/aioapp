import asyncio
import traceback
import aioredis
from typing import Optional
from ..app import Component
from ..error import PrepareError
from ..tracer import (Span, CLIENT, SPAN_TYPE, SPAN_KIND, SPAN_TYPE_REDIS,
                      SPAN_KIND_REDIS_ACQUIRE, SPAN_KIND_REDIS_QUERY,
                      SPAN_KIND_REDIS_PUBSUB)


class RedisTracerConfig:

    def on_acquire_start(self, ctx: 'Span') -> None:
        pass

    def on_acquire_end(self, ctx: 'Span',
                       err: Optional[Exception]) -> None:
        if err:
            ctx.tag('error.message', str(err))
            ctx.annotate(traceback.format_exc())

    def on_query_start(self, ctx: 'Span', id: str,
                       command: str) -> None:
        pass

    def on_query_end(self, ctx: 'Span',
                     err: Optional[Exception], result) -> None:
        if err:
            ctx.tag('error.message', str(err))
            ctx.annotate(traceback.format_exc())

    def on_pubsub_start(self, ctx: 'Span', id: str, command: str,
                        channels_or_patterns) -> None:
        pass

    def on_pubsub_end(self, ctx: 'Span',
                      err: Optional[Exception], result) -> None:
        if err:
            ctx.tag('error.message', str(err))
            ctx.annotate(traceback.format_exc())


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
        if self.app is None:
            raise UserWarning('Unattached component')

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
                   ctx: Span,
                   tracer_config: Optional[RedisTracerConfig] = None
                   ) -> 'ConnectionContextManager':
        return ConnectionContextManager(self, ctx, tracer_config)

    async def execute(self, ctx: Span, id: str,
                      command: str, *args,
                      tracer_config: Optional[RedisTracerConfig] = None):
        async with self.connection(ctx,
                                   tracer_config=tracer_config) as conn:
            return await conn.execute(ctx, id, command, *args,
                                      tracer_config=tracer_config)

    async def health(self, ctx: Span):
        await self.execute(ctx, 'test', 'GET', 'somekey')


class ConnectionContextManager:
    def __init__(self, redis, ctx,
                 tracer_config: Optional[RedisTracerConfig] = None) -> None:
        self._redis = redis
        self._conn = None
        self._ctx = ctx
        self._tracer_config = tracer_config

    async def __aenter__(self) -> 'Connection':
        span = None
        if self._ctx:
            span = self._ctx.new_child()
        try:
            if span:
                span.kind(CLIENT)
                span.name("redis:Acquire")
                span.metrics_tag(SPAN_TYPE, SPAN_TYPE_REDIS)
                span.metrics_tag(SPAN_KIND, SPAN_KIND_REDIS_ACQUIRE)
                span.remote_endpoint("redis")
                span.tag('redis.size_before', self._redis.pool.size)
                span.tag('redis.free_before', self._redis.pool.freesize)
                span.start()
                if self._tracer_config:
                    self._tracer_config.on_acquire_start(span)
            self._conn = await self._redis.pool.acquire()
            if span:
                if self._tracer_config:
                    self._tracer_config.on_acquire_end(span, None)
                span.finish()
        except Exception as err:
            if span:
                if self._tracer_config:
                    self._tracer_config.on_acquire_end(span, err)
                span.finish(exception=err)
            raise
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

    async def execute(self, ctx: Span, id: str,
                      command: str, *args,
                      tracer_config: Optional[RedisTracerConfig] = None):
        span = None
        if ctx:
            span = ctx.new_child()
        try:
            if span:
                span.kind(CLIENT)
                span.name("redis:%s" % id)
                span.metrics_tag(SPAN_TYPE, SPAN_TYPE_REDIS)
                span.metrics_tag(SPAN_KIND, SPAN_KIND_REDIS_QUERY)
                span.remote_endpoint("redis")
                span.tag("redis.command", command)
                span.annotate(repr(args))
                span.start()
                if tracer_config:
                    tracer_config.on_query_start(span, id, command)
            res = await self._conn.execute(command, *args)
            if span:
                if tracer_config:
                    tracer_config.on_query_end(span, None, res)
                span.finish()
        except Exception as err:
            if span:
                if tracer_config:
                    tracer_config.on_query_end(span, err, None)
                span.finish(exception=err)
            raise

        return res

    async def execute_pubsub(self, ctx: Span, id: str,
                             command,
                             *channels_or_patterns,
                             tracer_config: Optional[
                                 RedisTracerConfig] = None):
        span = None
        if ctx:
            span = ctx.new_child()
        try:
            if span:
                span.kind(CLIENT)
                span.name("redis:%s" % id)
                span.metrics_tag(SPAN_TYPE, SPAN_TYPE_REDIS)
                span.metrics_tag(SPAN_KIND, SPAN_KIND_REDIS_PUBSUB)
                span.remote_endpoint("redis")
                span.tag("redis.pubsub", command)
                span.annotate(repr(channels_or_patterns))
                span.start()
                if tracer_config:
                    tracer_config.on_pubsub_start(span, id, command,
                                                  channels_or_patterns)
            res = await self._conn.execute_pubsub(command,
                                                  *channels_or_patterns)
            if span:
                if tracer_config:
                    tracer_config.on_pubsub_end(span, None, res)
                span.finish()
        except Exception as err:
            if span:
                if tracer_config:
                    tracer_config.on_pubsub_end(span, err, None)
                span.finish(exception=err)
            raise

        return res
