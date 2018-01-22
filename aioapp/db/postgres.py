import json
from typing import Union, Dict, List, Any
import asyncio
import asyncpg
import asyncpg.protocol
import asyncpg.pool
import aiozipkin as az
import aiozipkin.span as azs
from ..app import Component
from ..error import PrepareError
from ..misc import mask_url_pwd

JsonType = Union[None, int, float, str, bool, List[Any], Dict[str, Any]]


class Postgres(Component):
    def __init__(self, dsn: str, pool_min_size: int=10, pool_max_size: int=10,
                 pool_max_queries: int=50000,
                 pool_max_inactive_connection_lifetime: float=300.0,
                 connect_max_attempts: int=10,
                 connect_retry_delay: float=1.0) -> None:
        super(Postgres, self).__init__()
        self.dsn = dsn
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
    def _masked_dsn(self) -> str:
        return mask_url_pwd(self.dsn)

    async def _connect(self) -> None:
        self._pool: asyncpg.pool.Pool = await asyncpg.create_pool(
            dsn=self.dsn,
            max_size=self.pool_max_size,
            min_size=self.pool_min_size,
            max_queries=self.pool_max_queries,
            max_inactive_connection_lifetime=(
                self.pool_max_inactive_connection_lifetime),
            init=Postgres._conn_init,
            loop=self.loop
        )

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
        self.app.log_info("Connecting to %s" % self._masked_dsn)
        for i in range(self.connect_max_attempts):
            try:
                await self._connect()
                self.app.log_info("Connected to %s" % self._masked_dsn)
                return
            except Exception as e:
                self.app.log_err(str(e))
                await asyncio.sleep(self.connect_retry_delay)
        raise PrepareError("Could not connect to %s" % self._masked_dsn)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.app.log_info("Disconnecting from %s" % self._masked_dsn)
        if self.pool:
            await self.pool.close()

    def connection(self, context_span: azs.SpanAbc
                   ) -> 'ConnectionContextManager':
        return ConnectionContextManager(self, context_span)

    async def query_one(self, context_span: azs.SpanAbc, id: str, query: str,
                        *args: Any, timeout: float = None
                        ) -> asyncpg.protocol.Record:
        async with self.connection(context_span) as conn:
            return await conn.query_one(context_span, id, query, *args,
                                        timeout=timeout)

    async def query_all(self, context_span: azs.SpanAbc, id: str, query: str,
                        *args: Any, timeout: float = None
                        ) -> List[asyncpg.protocol.Record]:
        async with self.connection(context_span) as conn:
            return await conn.query_all(context_span, id, query, *args,
                                        timeout=timeout)

    async def execute(self, context_span: azs.SpanAbc, id: str, query: str,
                      *args: Any, timeout: float = None) -> str:
        async with self.connection(context_span) as conn:
            return await conn.execute(context_span, id, query, *args,
                                      timeout=timeout)


class ConnectionContextManager:
    def __init__(self, db: Postgres, context_span: azs.SpanAbc) -> None:
        self._db = db
        self._conn = None
        self._context_span = context_span

    async def __aenter__(self) -> 'Connection':
        with self._context_span.tracer.new_child(
                self._context_span.context) as span:
            span.kind(az.CLIENT)
            span.name("db:Acquire")
            span.remote_endpoint("postgres")
            self._conn = await self._db._pool.acquire()
        c = Connection(self._db, self._conn)
        return c

    async def __aexit__(self, exc_type: type, exc: BaseException,
                        tb: type) -> bool:
        await self._db._pool.release(self._conn)
        return False


class TransactionContextManager:
    def __init__(self, context_span: azs.SpanAbc, conn: 'Connection',
                 isolation_level: str=None) -> None:
        self._conn = conn
        self._isolation_level = isolation_level
        self._context_span = context_span

    def _begin_query(self) -> str:
        query = "BEGIN TRANSACTION"
        if self._isolation_level:
            query += " ISOLATION LEVEL %s" % self._isolation_level
        return query

    async def __aenter__(self) -> None:
        await self._conn.execute(self._context_span,
                                 query=self._begin_query(),
                                 id="BeginTransaction")

    async def __aexit__(self, exc_type: type, exc: BaseException,
                        tb: type) -> bool:
        if exc:
            await self._conn.execute(self._context_span,
                                     query="ROLLBACK", id="Rollback")
        else:
            await self._conn.execute(self._context_span,
                                     query="COMMIT", id="Commit")
        return False


class Connection:
    def __init__(self, db: Postgres,
                 conn: asyncpg.pool.PoolConnectionProxy) -> None:
        self._db = db
        self._conn = conn

    def xact(self, context_span: azs.SpanAbc,
             isolation_level: str=None) -> 'TransactionContextManager':
        return TransactionContextManager(context_span, self, isolation_level)

    async def execute(self, context_span: azs.SpanAbc, id: str,
                      query: str, *args: Any, timeout: float = None) -> str:
        with context_span.tracer.new_child(context_span.context) as span:
            span.kind(az.CLIENT)
            span.name("db:%s" % id)
            span.remote_endpoint("postgres")
            span.annotate(repr(args))
            res = await self._conn.execute(query, *args, timeout=timeout)
        return res

    async def query_one(self, context_span: azs.SpanAbc, id: str,
                        query: str, *args: Any,
                        timeout: float = None) -> asyncpg.protocol.Record:
        with context_span.tracer.new_child(context_span.context) as span:
            span.kind(az.CLIENT)
            span.name("db:%s" % id)
            span.remote_endpoint("postgres")
            span.annotate(repr(args))
            res = await self._conn.fetchrow(query, *args, timeout=timeout)
        return res

    async def query_all(self, context_span: azs.SpanAbc, id: str,
                        query: str, *args: Any, timeout: float = None
                        ) -> List[asyncpg.protocol.Record]:
        with context_span.tracer.new_child(context_span.context) as span:
            span.kind(az.CLIENT)
            span.name("db:%s" % id)
            span.remote_endpoint("postgres")
            span.annotate(repr(args))
            res = await self._conn.fetch(query, *args, timeout=timeout)
        return res
