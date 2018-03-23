import json
import traceback
from typing import Union, Dict, List, Any, Optional
import asyncio
import asyncpg
import asyncpg.protocol
import asyncpg.pool
from ..app import Component
from ..error import PrepareError
from ..misc import mask_url_pwd
from ..tracer import (Span, CLIENT, SPAN_TYPE, SPAN_KIND, SPAN_TYPE_POSTGRES,
                      SPAN_KIND_POSTRGES_ACQUIRE, SPAN_KIND_POSTRGES_QUERY)

JsonType = Union[None, int, float, str, bool, List[Any], Dict[str, Any]]


class PostgresTracerConfig:

    def on_acquire_start(self, context_span: 'Span') -> None:
        pass

    def on_acquire_end(self, context_span: 'Span',
                       err: Optional[Exception]) -> None:
        if err:
            context_span.tag('error.message', str(err))
            context_span.annotate(traceback.format_exc())

    def on_query_start(self, context_span: 'Span', id: str, query: str,
                       args: tuple, timeout: Optional[float]) -> None:
        pass

    def on_query_end(self, context_span: 'Span',
                     err: Optional[Exception], result) -> None:
        if err:
            context_span.tag('error.message', str(err))
            context_span.annotate(traceback.format_exc())


class Postgres(Component):
    def __init__(self, url: str, pool_min_size: int = 10,
                 pool_max_size: int = 10,
                 pool_max_queries: int = 50000,
                 pool_max_inactive_connection_lifetime: float = 300.0,
                 connect_max_attempts: int = 10,
                 connect_retry_delay: float = 1.0) -> None:
        super(Postgres, self).__init__()
        self.url = url
        self.pool_min_size = pool_min_size
        self.pool_max_size = pool_max_size
        self.pool_max_queries = pool_max_queries
        self.pool_max_inactive_connection_lifetime = \
            pool_max_inactive_connection_lifetime
        self.connect_max_attempts = connect_max_attempts
        self.connect_retry_delay = connect_retry_delay
        self._pool: asyncpg.pool.Pool = None

    @property
    def pool(self) -> asyncpg.pool.Pool:
        return self._pool

    @property
    def _masked_url(self) -> str:
        return mask_url_pwd(self.url)

    async def _connect(self) -> None:
        self.app.log_info("Connecting to %s" % self._masked_url)
        self._pool: asyncpg.pool.Pool = await asyncpg.create_pool(
            dsn=self.url,
            max_size=self.pool_max_size,
            min_size=self.pool_min_size,
            max_queries=self.pool_max_queries,
            max_inactive_connection_lifetime=(
                self.pool_max_inactive_connection_lifetime),
            init=Postgres._conn_init,
            loop=self.loop
        )
        self.app.log_info("Connected to %s" % self._masked_url)

    @staticmethod
    async def _conn_init(conn: asyncpg.pool.PoolConnectionProxy) -> None:
        def _json_encoder(value: JsonType) -> str:
            return json.dumps(value)

        def _json_decoder(value: str) -> JsonType:
            return json.loads(value)

        await conn.set_type_codec(
            'json', encoder=_json_encoder, decoder=_json_decoder,
            schema='pg_catalog'
        )

        def _jsonb_encoder(value: JsonType) -> bytes:
            return b'\x01' + json.dumps(value).encode('utf-8')

        def _jsonb_decoder(value: bytes) -> JsonType:
            return json.loads(value[1:].decode('utf-8'))

        # Example was got from https://github.com/MagicStack/asyncpg/issues/140
        await conn.set_type_codec(
            'jsonb',
            encoder=_jsonb_encoder,
            decoder=_jsonb_decoder,
            schema='pg_catalog',
            format='binary',
        )

    async def prepare(self) -> None:
        for i in range(self.connect_max_attempts):
            try:
                await self._connect()
                return
            except Exception as e:
                self.app.log_err(str(e))
                await asyncio.sleep(self.connect_retry_delay)
        raise PrepareError("Could not connect to %s" % self._masked_url)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        if self.pool:
            self.app.log_info("Disconnecting from %s" % self._masked_url)
            await self.pool.close()

    def connection(self, context_span: Span,
                   acquire_timeout=None,
                   tracer_config: Optional[PostgresTracerConfig] = None
                   ) -> 'ConnectionContextManager':
        return ConnectionContextManager(self, context_span,
                                        acquire_timeout=acquire_timeout,
                                        tracer_config=tracer_config)

    async def query_one(self, context_span: Span, id: str, query: str,
                        *args: Any, timeout: float = None,
                        tracer_config: Optional[PostgresTracerConfig] = None
                        ) -> asyncpg.protocol.Record:
        async with self.connection(context_span,
                                   tracer_config=tracer_config) as conn:
            return await conn.query_one(context_span, id, query, *args,
                                        timeout=timeout,
                                        tracer_config=tracer_config)

    async def query_all(self, context_span: Span, id: str, query: str,
                        *args: Any, timeout: float = None,
                        tracer_config: Optional[PostgresTracerConfig] = None
                        ) -> List[asyncpg.protocol.Record]:
        async with self.connection(context_span,
                                   tracer_config=tracer_config) as conn:
            return await conn.query_all(context_span, id, query, *args,
                                        timeout=timeout,
                                        tracer_config=tracer_config)

    async def execute(self, context_span: Span, id: str, query: str,
                      *args: Any, timeout: float = None,
                      tracer_config: Optional[PostgresTracerConfig] = None
                      ) -> str:
        async with self.connection(context_span,
                                   tracer_config=tracer_config) as conn:
            return await conn.execute(context_span, id, query, *args,
                                      timeout=timeout,
                                      tracer_config=tracer_config)


class ConnectionContextManager:
    def __init__(self, db: Postgres, context_span: Span,
                 acquire_timeout: float = None,
                 tracer_config: Optional[PostgresTracerConfig] = None) -> None:
        self._db = db
        self._conn = None
        self._context_span = context_span
        self._acquire_timeout = acquire_timeout
        self._tracer_config = tracer_config

    async def __aenter__(self) -> 'Connection':
        span = None
        if self._context_span:
            span = self._context_span.new_child()
        try:
            if span:
                span.kind(CLIENT)
                span.name("db:Acquire")
                span.metrics_tag(SPAN_TYPE, SPAN_TYPE_POSTGRES)
                span.metrics_tag(SPAN_KIND, SPAN_KIND_POSTRGES_ACQUIRE)
                span.remote_endpoint("postgres")
                span.start()
                if self._tracer_config:
                    self._tracer_config.on_acquire_start(span)
            self._conn = await self._db._pool.acquire(
                timeout=self._acquire_timeout)
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
        c = Connection(self._db, self._conn)
        return c

    async def __aexit__(self, exc_type: type, exc: BaseException,
                        tb: type) -> bool:
        await self._db._pool.release(self._conn)
        return False


class TransactionContextManager:
    def __init__(self, context_span: Span, conn: 'Connection',
                 isolation_level: str = None,
                 tracer_config: Optional[PostgresTracerConfig] = None) -> None:
        self._conn = conn
        self._isolation_level = isolation_level
        self._context_span = context_span
        self._tracer_config = tracer_config

    def _begin_query(self) -> str:
        query = "BEGIN TRANSACTION"
        if self._isolation_level:
            query += " ISOLATION LEVEL %s" % self._isolation_level
        return query

    async def __aenter__(self) -> None:
        await self._conn.execute(self._context_span,
                                 query=self._begin_query(),
                                 id="BeginTransaction",
                                 tracer_config=self._tracer_config)

    async def __aexit__(self, exc_type: type, exc: BaseException,
                        tb: type) -> bool:
        if exc:
            await self._conn.execute(self._context_span,
                                     query="ROLLBACK", id="Rollback",
                                     tracer_config=self._tracer_config)
        else:
            await self._conn.execute(self._context_span,
                                     query="COMMIT", id="Commit",
                                     tracer_config=self._tracer_config)
        return False


class Connection:
    def __init__(self, db: Postgres,
                 conn: asyncpg.pool.PoolConnectionProxy) -> None:
        self._db = db
        self._conn = conn

    def xact(self, context_span: Span,
             isolation_level: str = None,
             tracer_config: Optional[PostgresTracerConfig] = None
             ) -> 'TransactionContextManager':
        return TransactionContextManager(context_span, self, isolation_level,
                                         tracer_config)

    async def execute(self, context_span: Span, id: str,
                      query: str, *args: Any, timeout: float = None,
                      tracer_config: Optional[
                          PostgresTracerConfig] = None) -> str:
        span = None
        if context_span:
            span = context_span.new_child()

        try:
            if span:
                span.kind(CLIENT)
                span.name("db:%s" % id)
                span.metrics_tag(SPAN_TYPE, SPAN_TYPE_POSTGRES)
                span.metrics_tag(SPAN_KIND, SPAN_KIND_POSTRGES_QUERY)
                span.remote_endpoint("postgres")
                span.annotate(repr(args))
                span.start()
                if tracer_config:
                    tracer_config.on_query_start(span, id, query, args,
                                                 timeout)
            res = await self._conn.execute(query, *args, timeout=timeout)
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

    async def query_one(self, context_span: Span, id: str,
                        query: str, *args: Any,
                        timeout: float = None,
                        tracer_config: Optional[PostgresTracerConfig] = None
                        ) -> asyncpg.protocol.Record:
        span = None
        if context_span:
            span = context_span.new_child()
        try:
            if span:
                span.kind(CLIENT)
                span.name("db:%s" % id)
                span.metrics_tag(SPAN_TYPE, SPAN_TYPE_POSTGRES)
                span.metrics_tag(SPAN_KIND, SPAN_KIND_POSTRGES_QUERY)
                span.remote_endpoint("postgres")
                span.annotate(repr(args))
                span.start()
                if tracer_config:
                    tracer_config.on_query_start(span, id, query, args,
                                                 timeout)
            res = await self._conn.fetchrow(query, *args, timeout=timeout)
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

    async def query_all(self, context_span: Span, id: str,
                        query: str, *args: Any, timeout: float = None,
                        tracer_config: Optional[PostgresTracerConfig] = None
                        ) -> List[asyncpg.protocol.Record]:
        span = None
        if context_span:
            span = context_span.new_child()
        try:
            if span:
                span.kind(CLIENT)
                span.name("db:%s" % id)
                span.metrics_tag(SPAN_TYPE, SPAN_TYPE_POSTGRES)
                span.metrics_tag(SPAN_KIND, SPAN_KIND_POSTRGES_QUERY)
                span.remote_endpoint("postgres")
                span.annotate(repr(args))
                span.start()
                if tracer_config:
                    tracer_config.on_query_start(span, id, query, args,
                                                 timeout)
            res = await self._conn.fetch(query, *args, timeout=timeout)
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
