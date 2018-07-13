import logging
import asyncio
from aiohttp import web, web_request
from aioapp.app import Application
from aioapp import http, db, chat, amqp, tracer


class HttpHandler(http.Handler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def prepare(self):
        self.server.add_route('GET', '/', self.home_handler)
        self.server.set_error_handler(self.error_handler)

    async def error_handler(self, ctx, request: web_request.Request,
                            error: Exception) -> web.Response:
        self.app.log_err(error)
        if isinstance(error, web.HTTPException):
            return error
        return web.Response(body='Internal Error: ' + str(error), status=500)

    async def home_handler(self, ctx: tracer.Span,
                           request: web_request.Request) -> web.Response:
        with ctx.new_child() as span:
            span.name('test:sleep')
            with span.new_child() as span2:
                span2.name('test2:sleep')
                await asyncio.sleep(.15, loop=self.app.loop)

        await self.app.db.query_one(ctx,
                                    'postgres:test', 'SELECT $1::int as a',
                                    123)
        # await self.app.tg.send_message(ctx,
        #                                1825135, request.url)

        await self.app.redis.execute(ctx, 'redis:set',
                                     'SET', 'key', 1)
        res = await self.app.redis.execute(ctx, 'redis:set',
                                           'GET', 'key')

        return web.Response(text='Hello world! ' + res)


class TelegramHandler(chat.TelegramHandler):
    def __init__(self, *args, **kwargs):
        super(TelegramHandler, self).__init__(*args, **kwargs)
        cmds = {
            '/start': self.start,
            '/echo (.*)': self.echo,
        }
        for regexp, fn in cmds.items():
            self.bot.add_command(regexp, fn)
        self.bot.set_default(self.default)

    async def default(self, ctx, chat, message):
        await asyncio.sleep(0.2)
        await self.bot.send_message(ctx, chat.id,
                                    'what?' + str(ctx))

    async def start(self, ctx, chat, match):
        await chat.send_text(ctx, 'hello')

    async def echo(self, ctx, chat, match):
        await chat.reply(ctx, match.group(1))


async def span_finish(span: tracer.Span):
    print('span finish:', span)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    # telegram_api_token = os.environ.get('TELEGRAM_API_TOKEN')
    # if not telegram_api_token:
    #     print('Environment variable TELEGRAM_API_TOKEN not given')
    #     print('TELEGRAM_API_TOKEN=<token> python -m examples.full')
    #     exit(1)

    loop = asyncio.get_event_loop()
    app = Application(
        loop=loop
    )
    app.add(
        'http_server',
        http.Server(
            '127.0.0.1',
            8080,
            HttpHandler
        )
    )
    app.add(
        'db',
        db.Postgres(
            url='postgres://postgres@127.0.0.1:19801/postgres',
            pool_min_size=2,
            pool_max_size=19,
            pool_max_queries=50000,
            pool_max_inactive_connection_lifetime=300.,
            connect_max_attempts=10,
            connect_retry_delay=1.0),
        stop_after=['http_server']

    )

    app.add(
        'redis',
        db.Redis(
            url='redis://127.0.0.1:19802/0?encoding=utf-8',
            pool_min_size=2,
            pool_max_size=4,
            connect_max_attempts=10,
            connect_retry_delay=1.0
        )
    )

    # Fucking RKN
    # app.add(
    #     'tg',
    #     chat.Telegram(
    #         api_token=telegram_api_token,
    #         handler=TelegramHandler,
    #         connect_max_attempts=10,
    #         connect_retry_delay=1,
    #     )
    # )

    app.add(
        'amqp',
        amqp.Amqp(
            url='amqp://guest:guest@127.0.0.1:19803/',
        )
    )

    app.setup_logging(
        tracer_driver='zipkin',
        tracer_addr='http://127.0.0.1:19806/',
        tracer_name='test-svc',
        tracer_sample_rate=1.0,
        tracer_send_inteval=3,
        metrics_driver='telegraf-influx',
        metrics_addr='udp://127.0.0.1:19804',
        metrics_name='test_svc_',
        on_span_finish=span_finish
    )

    app.run()
