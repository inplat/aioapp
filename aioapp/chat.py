import traceback
from functools import partial
from typing import Optional
import asyncio
from abc import ABCMeta
from typing import Type, Callable, Any
from .app import Component
from .error import PrepareError
from aiotg import Bot, Chat, Sender
from .misc import json_encode
from .tracer import (Span, CLIENT, SERVER, SPAN_TYPE, SPAN_KIND,
                     SPAN_TYPE_TELEGRAM, SPAN_KIND_TELEGRAM_IN,
                     SPAN_KIND_TELEGRAM_OUT)


class TelegramTracerConfig:

    def on_api_call_start(self, ctx: 'Span', method: str,
                          params: dict) -> None:
        pass

    def on_api_call_end(self, ctx: 'Span',
                        err: Optional[Exception], result) -> None:
        if err:
            ctx.tag('error.message', str(err))
            ctx.annotate(traceback.format_exc())


class TelegramHandler(object):
    __metaclass__ = ABCMeta

    def __init__(self, bot: 'Telegram') -> None:
        self.bot: Telegram = bot

    @property
    def app(self):
        return self.bot.app


class Telegram(Component):
    def __init__(self, api_token: str, handler: Type[TelegramHandler],
                 connect_max_attempts: int = 10,
                 connect_retry_delay: float = 1.0,
                 api_timeout: int = 60, bot_class=Bot) -> None:
        super(Telegram, self).__init__()
        self.tg_id: Optional[int] = None
        self.tg_first_name: Optional[str] = None
        self.tg_username: Optional[str] = None
        self.api_token: str = api_token
        self.bot: Bot = bot_class(self.api_token,
                                  api_timeout=api_timeout,
                                  json_serialize=json_encode)
        self.handler = handler(self)
        self._connect_max_attempts: int = connect_max_attempts
        self._connect_retry_delay: float = connect_retry_delay
        self._run_fut: Optional[asyncio.Future] = None
        self._stopping: bool = False
        self._active_calls: int = 0
        self._stop_calls_fut: Optional[asyncio.Future] = None
        self._active_msgs: int = 0
        self._stop_msgs_fut: Optional[asyncio.Future] = None

    async def prepare(self) -> None:
        if self.app is None:
            raise UserWarning('Unattached component')
        attempt = 0
        while True:
            try:
                self.app.log_info("Connecting to telegram")
                me = await self.bot.get_me()
                break
            except Exception as e:
                self.app.log_err(e)
                await asyncio.sleep(self._connect_retry_delay)
                if attempt > self._connect_max_attempts:
                    raise PrepareError("Could not connect to telegram")
                attempt += 1

        self.tg_id = me.get('id')
        self.tg_first_name = me.get('first_name')
        self.tg_username = me.get('username')
        self.app.log_info('Connected to telegram as "%s"' % self.tg_username)
        self._stop_calls_fut = asyncio.Future(loop=self.app.loop)
        self._stop_msgs_fut = asyncio.Future(loop=self.app.loop)

    async def start(self):
        self._run_fut = asyncio.ensure_future(self.bot.loop(), loop=self.loop)

    async def stop(self):
        self.app.log_info("Stopping telegram bot")
        self._stopping = True
        self.bot.stop()
        if self._run_fut:
            self._run_fut.cancel()
        # await self.handler.stop()

        if self._active_msgs > 0:
            self.app.log_info("Waiting for telegram handler to stop")
            await asyncio.wait([self._stop_msgs_fut], loop=self.app.loop)

        if self._active_calls > 0:
            self.app.log_info("Waiting for stopping telegram api calls")
            await asyncio.wait([self._stop_calls_fut], loop=self.app.loop)

        # fixme support aiohttp>=3.0
        if self.bot._session:
            try:
                await self.bot._session.close()
                self.bot._session = None
            except Exception as err:
                self.app.log_err(err)

    async def health(self, ctx: Span):
        await self.bot.get_me()

    async def send_message(self, ctx, chat_id, text,
                           tracer_config: Optional[
                               TelegramTracerConfig] = None,
                           **options) -> None:
        await self.api_call(ctx, "sendMessage",
                            chat_id=chat_id, text=text,
                            tracer_config=tracer_config, **options)

    async def api_call(self, ctx: Span, method,
                       tracer_config: Optional[TelegramTracerConfig] = None,
                       **params):
        self._active_calls += 1
        try:
            span = None
            if ctx:
                span = ctx.new_child()
            try:
                if span:
                    span.name('telegram:%s' % method)
                    span.metrics_tag(SPAN_TYPE, SPAN_TYPE_TELEGRAM)
                    span.metrics_tag(SPAN_KIND, SPAN_KIND_TELEGRAM_OUT)
                    span.kind(CLIENT)
                    span.tag('telegram.method', method, True)
                    if 'chat_id' in params:
                        span.tag('telegram:chat_id',
                                 str(params.get('chat_id')))
                    span.annotate(json_encode(params))
                    span.start()
                    if tracer_config:
                        tracer_config.on_api_call_start(span, method, params)
                res = await self.bot.api_call(method, **params)
                if span:
                    if tracer_config:
                        tracer_config.on_api_call_end(span, None, res)
                    span.finish()
            except Exception as err:
                if span:
                    if tracer_config:
                        tracer_config.on_api_call_end(span, err, None)
                    span.finish(exception=err)
                raise

        finally:
            self._active_calls -= 1
            if self._stopping and self._active_calls == 0:
                if self._stop_calls_fut is not None:
                    self._stop_calls_fut.set_result(1)

    def add_command(self, regexp, fn: Callable[[Span, 'TelegramChat',
                                                Any], None]) -> None:
        """
        Manually register regexp based command
        """
        self.bot.add_command(regexp, self._graceful_fn(fn))

    def set_default(self, fn):
        """
        Set callback for default command that is called on unrecognized
        commands for 1-to-1 chats
        If default_in_groups option is True, callback is called in groups too
        """
        self.bot.default(self._graceful_fn(fn))

    def add_inline(self, regexp, fn):
        """
        Manually register regexp based callback
        """
        self.bot.add_inline(regexp, self._graceful_fn(fn))

    def add_callback(self, regexp, fn):
        """
        Manually register regexp based callback
        """
        self.bot.add_inline(regexp, self._graceful_fn(fn))

    def _graceful_fn(self, func):
        if not asyncio.iscoroutinefunction(func):
            raise UserWarning('handler must be coroutine function')

        async def wrap(func, chat, match):
            self._active_msgs += 1
            try:
                span = None
                if self.app.tracer:
                    span = self.app.tracer.new_trace()
                    span.start()
                try:
                    if span:
                        span.name('telegram:in')
                        span.kind(SERVER)
                        span.metrics_tag(SPAN_TYPE, SPAN_TYPE_TELEGRAM)
                        span.metrics_tag(SPAN_KIND, SPAN_KIND_TELEGRAM_IN)
                        span.tag('telegram:date',
                                 chat.message.get('date'))
                        span.tag('telegram:message_id',
                                 chat.message.get('message_id'))
                        span.tag('telegram:from_username',
                                 chat.message.get('from',
                                                  {}).get('username'))
                        span.tag('telegram:from_last_name',
                                 chat.message.get('from',
                                                  {}).get('last_name'))
                        span.tag('telegram:from_first_name',
                                 chat.message.get('from',
                                                  {}).get('first_name'))
                        span.tag('telegram:from_id',
                                 chat.message.get('from',
                                                  {}).get('id'))
                        span.tag('telegram:from_is_bot',
                                 chat.message.get('from',
                                                  {}).get('is_bot'))
                        span.tag('telegram:from_language_code',
                                 chat.message.get('from',
                                                  {}).get('language_code'))
                        span.tag('telegram:chat_username',
                                 chat.message.get('chat',
                                                  {}).get('username'))
                        span.tag('telegram:chat_last_name',
                                 chat.message.get('chat',
                                                  {}).get('last_name'))
                        span.tag('telegram:chat_first_name',
                                 chat.message.get('chat',
                                                  {}).get('first_name'))
                        span.tag('telegram:chat_id',
                                 chat.message.get('chat',
                                                  {}).get('id'))
                        span.tag('telegram:chat_type',
                                 chat.message.get('chat',
                                                  {}).get('private'))
                    await func(span, TelegramChat(chat, self), match)
                except Exception as err:
                    if span:
                        span.tag('error.message', str(err))
                        span.annotate(traceback.format_exc())
                        span.finish(exception=err)
                    raise
                finally:
                    if span:
                        span.finish()
            finally:
                self._active_msgs -= 1
                if self._stopping and self._active_msgs == 0:
                    self._stop_msgs_fut.set_result(1)

        pt = partial(wrap, func)
        pt.__name__ = func.__name__
        return pt


class TelegramChat:
    def __init__(self, chat: Chat, bot: Telegram) -> None:
        self._chat = chat
        self._bot = bot
        self.id = chat.id
        self.sender: Sender = chat.sender
        self.message: dict = chat.message
        self.type: str = chat.type

    async def send_text(self, ctx, text,
                        tracer_config: Optional[TelegramTracerConfig] = None,
                        **options):
        await self._bot.send_message(ctx, self.id, text,
                                     tracer_config=tracer_config,
                                     **options)

    async def reply(self, ctx, text, markup=None, parse_mode=None,
                    tracer_config: Optional[TelegramTracerConfig] = None):
        if markup is None:
            markup = {}

        await self.send_text(
            ctx,
            text,
            reply_to_message_id=self._chat.message["message_id"],
            disable_web_page_preview='true',
            reply_markup=self._chat.bot.json_serialize(markup),
            parse_mode=parse_mode,
            tracer_config=tracer_config
        )
