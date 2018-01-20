import json
import asyncio
import asyncpg
import asyncpg.pool
import aiozipkin as az
import aiozipkin.span as azs  # noqa
from .app import Component
from .error import PrepareError
from .misc import mask_url_pwd


class PgDb(Component):
    def __init__(self, dsn, pool_min_size=10, pool_max_size=10,
                 pool_max_queries=50000,
                 pool_max_inactive_connection_lifetime=300.0,
                 connect_max_attempts=10, connect_retry_delay=1.0) -> None:
        super(PgDb, self).__init__()
        self.dsn = dsn
        self.pool_min_size = pool_min_size
        self.pool_max_size = pool_max_size
        self.pool_max_queries = pool_max_queries
        self.pool_max_inactive_connection_lifetime = \
            pool_max_inactive_connection_lifetime
        self.connect_max_attempts = connect_max_attempts
        self.connect_retry_delay = connect_retry_delay
        self._pool = None  # type: asyncpg.pool.Pool

    @property
    def pool(self) -> asyncpg.pool.Pool:
        return self._pool

    @property
    def _masked_dsn(self):
        return mask_url_pwd(self.dsn)

    async def _connect(self):
        self._pool = await asyncpg.create_pool(
            dsn=self.dsn,
            max_size=self.pool_max_size,
            min_size=self.pool_min_size,
            max_queries=self.pool_max_queries,
            max_inactive_connection_lifetime=(
                self.pool_max_inactive_connection_lifetime),
            init=PgDb._conn_init,
            loop=self.loop
        )

    @staticmethod
    async def _conn_init(conn):
        def _json_encoder(value):
            return json.dumps(value)

        def _json_decoder(value):
            return json.loads(value)

        await conn.set_type_codec(
            'json', encoder=_json_encoder, decoder=_json_decoder,
            schema='pg_catalog'
        )

        def _jsonb_encoder(value):
            return b'\x01' + json.dumps(value).encode('utf-8')

        def _jsonb_decoder(value):
            return json.loads(value[1:].decode('utf-8'))

        # Example was got from https://github.com/MagicStack/asyncpg/issues/140
        await conn.set_type_codec(
            'jsonb',
            encoder=_jsonb_encoder,
            decoder=_jsonb_decoder,
            schema='pg_catalog',
            format='binary',
        )

    async def prepare(self):
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

    async def start(self):
        pass

    async def stop(self):
        self.app.log_info("Disconnecting from %s" % self._masked_dsn)
        if self.pool:
            await self.pool.close()

    def connection(self, context_span):
        return ConnectionContextManager(self, context_span)

    async def query_one(self, context_span: azs.SpanAbc, id: str, query: str,
                        *args, timeout: float = None):
        async with self.connection(context_span) as conn:
            return await conn.query_one(context_span, id, query, *args,
                                        timeout=timeout)

    async def query_all(self, context_span: azs.SpanAbc, id: str, query: str,
                        *args, timeout: float = None):
        async with self.connection(context_span) as conn:
            return await conn.query_all(context_span, id, query, *args,
                                        timeout=timeout)

    async def execute(self, context_span: azs.SpanAbc, id: str, query: str,
                      *args, timeout: float = None):
        async with self.connection(context_span) as conn:
            return await conn.execute(context_span, id, query, *args,
                                      timeout=timeout)


class ConnectionContextManager:
    def __init__(self, db, context_span):
        self._db = db
        self._conn = None
        self._context_span = context_span

    async def __aenter__(self):
        with self._context_span.tracer.new_child(
                self._context_span.context) as span:
            span.kind(az.CLIENT)
            span.name("db:Acquire")
            span.remote_endpoint("postgres")
            self._conn = await self._db._pool.acquire()
        c = Connection(self._db, self._conn)
        return c

    async def __aexit__(self, exc_type, exc, tb):
        await self._db._pool.release(self._conn)


class TransactionContextManager:
    def __init__(self, context_span, conn, isolation_level=None):
        """
        :type context_span: azs.SpanAbc
        :type conn: Connection
        :type isolation_level: str
        """
        self._conn = conn
        self._isolation_level = isolation_level
        self._context_span = context_span

    def _begin_query(self):
        query = "BEGIN TRANSACTION"
        if self._isolation_level:
            query += " ISOLATION LEVEL %s" % self._isolation_level
        return query

    async def __aenter__(self):
        await self._conn.execute(self._context_span,
                                 query=self._begin_query(),
                                 id="BeginTransaction")

    async def __aexit__(self, exc_type, exc, tb):
        if exc:
            await self._conn.execute(self._context_span,
                                     query="ROLLBACK", id="Rollback")
        else:
            await self._conn.execute(self._context_span,
                                     query="COMMIT", id="Commit")


class Connection:
    def __init__(self, db, conn):
        """
        :type db: PgDb
        """
        self._db = db
        self._conn = conn

    def xact(self, context_span, isolation_level=None):
        """
        :type context_span: azs.SpanAbc
        :type isolation_level: str
        :rtype: TransactionContextManager
        """
        return TransactionContextManager(context_span, self, isolation_level)

    async def execute(self, context_span: azs.SpanAbc, id: str,
                      query: str, *args, timeout: float = None):
        with context_span.tracer.new_child(context_span.context) as span:
            span.kind(az.CLIENT)
            span.name("db:%s" % id)
            span.remote_endpoint("postgres")
            span.annotate(repr(args))
            res = await self._conn.execute(query, *args, timeout=timeout)
        return res

    async def query_one(self, context_span: azs.SpanAbc, id: str,
                        query: str, *args, timeout: float = None):
        with context_span.tracer.new_child(context_span.context) as span:
            span.kind(az.CLIENT)
            span.name("db:%s" % id)
            span.remote_endpoint("postgres")
            span.annotate(repr(args))
            res = await self._conn.fetchrow(query, *args, timeout=timeout)
        return res

    async def query_all(self, context_span: azs.SpanAbc, id: str,
                        query: str, *args, timeout: float = None):
        with context_span.tracer.new_child(context_span.context) as span:
            span.kind(az.CLIENT)
            span.name("db:%s" % id)
            span.remote_endpoint("postgres")
            span.annotate(repr(args))
            res = await self._conn.fetch(query, *args, timeout=timeout)
        return res
