import logging
import asyncio
from aiohttp import web, web_request
from aioapp.app import Application
from aioapp import http, db, chat


class HttpHandler(http.Handler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.server.add_route('GET', '/', self.home_handler)
        self.server.set_error_handler(self.error_handler)

    async def error_handler(self, context_span, request: web_request.Request,
                            error: Exception) -> web.Response:
        self.app.log_err(error)
        if isinstance(error, web.HTTPException):
            return error
        return web.Response(body='Internal Error: ' + str(error), status=500)

    async def home_handler(self, context_span,
                           request: web_request.Request) -> web.Response:
        with context_span.tracer.new_child(context_span.context) as span:
            span.name('test:sleep')
            with span.tracer.new_child(context_span.context) as span2:
                span2.name('test2:sleep')
                await asyncio.sleep(.1, loop=self.app.loop)

        await self.app.db.query_one(context_span,
                                    'test_query', 'SELECT $1::int as a',
                                    123)
        await self.app.tg.send_message(context_span,
                                       1825135, request.url)
        return web.Response(text='Hello world!')


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

    async def default(self, context_span, chat, message):
        await asyncio.sleep(0.2)
        await self.bot.send_message(context_span, chat.id,
                                    'what?' + str(context_span))

    async def start(self, context_span, chat, match):
        await chat.send_text(context_span, 'hello')

    async def echo(self, context_span, chat, match):
        await chat.reply(context_span, match.group(1))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

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
        db.PgDb(
            'postgres://user:passwd@localhost:15432/db',
            pool_min_size=2,
            pool_max_size=19,
            pool_max_queries=50000,
            pool_max_inactive_connection_lifetime=300.,
            connect_max_attempts=10,
            connect_retry_delay=1),
        stop_after=['http_server']

    )
    app.add(
        'tg',
        chat.Telegram(
            api_token='**************************',
            handler=TelegramHandler,
            connect_max_attempts=10,
            connect_retry_delay=1,
        )
    )

    app.setup_logging(
        tracer_driver='zipkin',
        tracer_addr='http://localhost:9411/',
        tracer_name='test-svc',
        tracer_sample_rate=1.0,
        tracer_send_inteval=3,
        metrics_driver='statsd',
        metrics_addr='localhost:8125',
        metrics_name='test_svc_'
    )

    app.run()
