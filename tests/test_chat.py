import asyncio
from aioapp.chat import Telegram, TelegramHandler
from aiotg import Bot
from aioapp.error import PrepareError
from aioapp.app import Application
from aioapp.misc import async_call
import pytest


class MockBot(Bot):
    def __init__(self, *args, **kwargs):
        super(MockBot, self).__init__(*args, **kwargs)
        self._mock_id_counter = 1
        self.last_call = None

    def _get_next_id(self):
        self._mock_id_counter += 1
        return self._mock_id_counter

    async def api_call(self, method, **params):
        if self.api_token == 'error':
            raise Exception()
        self.last_call = (method, params)
        if method == 'getMe':
            return {"ok": True,
                    "result": {"id": 1,
                               "is_bot": True,
                               "first_name": "Name1",
                               "username": "user1"}}
        return {}

    async def incoming_message(self, text):
        message_id = self._get_next_id()
        updates = {"ok": True, "result": [
            {"update_id": self._get_next_id(),
             "message": {"message_id": message_id,
                         "from": {"id": 1,
                                  "is_bot": False,
                                  "first_name": "Jon",
                                  "last_name": "Snow",
                                  "username": "jonshow",
                                  "language_code": "en-US"},
                         "chat": {"id": 1,
                                  "first_name": "Jon",
                                  "last_name": "Snow",
                                  "username": "jonshow",
                                  "type": "private"},
                         "date": 1516914025,
                         "text": text,
                         "entities": [
                             {"offset": 0, "length": 6,
                              "type": "bot_command"}]}}]}
        self._process_updates(updates)
        return message_id


async def test_telegram(app, loop):
    app.tg_last_event = None

    class Handler(TelegramHandler):
        def __init__(self, bot):
            super(Handler, self).__init__(bot)
            self.bot.add_command('/start', self.start)
            self.bot.set_default(self.default)

        async def start(self, ctx, chat, match):
            self.app.tg_last_event = 'start'
            await chat.send_text(ctx, 'ok')

        async def default(self, ctx, chat, message):
            self.app.tg_last_event = message['text']
            await chat.reply(ctx, message['text'])

    tg = Telegram(
        api_token='1',
        handler=Handler,
        bot_class=MockBot
    )
    bot = tg.bot
    app.add('tg', tg)
    await app.run_prepare()

    assert tg.tg_username == 'user1'
    assert tg.tg_first_name == 'Name1'
    assert tg.tg_id == 1

    await bot.incoming_message('/start')
    await asyncio.sleep(.1, loop=loop)
    assert app.tg_last_event == 'start'
    assert bot.last_call == ('sendMessage', {'chat_id': 1, 'text': 'ok'})

    message_id = await bot.incoming_message('hello')
    await asyncio.sleep(.1, loop=loop)
    assert app.tg_last_event == 'hello'
    assert bot.last_call == ('sendMessage',
                             {'chat_id': 1, 'text': 'hello',
                              'disable_web_page_preview': 'true',
                              'parse_mode': None,
                              'reply_markup': '{}',
                              'reply_to_message_id': message_id,
                              })


async def test_telegram_prepare_failure(app, unused_tcp_port):
    class Handler(TelegramHandler):
        pass

    with pytest.raises(PrepareError):
        tg = Telegram(
            api_token='error',
            handler=Handler,
            connect_retry_delay=0.001,
            connect_max_attempts=2,
            bot_class=MockBot
        )
        app.add('tg', tg)
        await app.run_prepare()


async def test_telegram_health_bad(app: Application,
                                   loop: asyncio.AbstractEventLoop) -> None:
    tg = Telegram(
        api_token='error',
        handler=TelegramHandler,
        bot_class=MockBot
    )
    app.add('tg', tg)

    async def start():
        await app.run_prepare()
        await tg.start()

    res = async_call(loop, start)
    await asyncio.sleep(1)

    result = await app.health()
    assert 'tg' in result
    assert result['tg'] is not None
    assert isinstance(result['tg'], BaseException)

    if res['fut'] is not None:
        res['fut'].cancel()


async def test_telegram_health_ok(app: Application,
                                  loop: asyncio.AbstractEventLoop) -> None:
    tg = Telegram(
        api_token='1',
        handler=TelegramHandler,
        bot_class=MockBot
    )
    app.add('tg', tg)

    async def start():
        await app.run_prepare()
        await tg.start()

    res = async_call(loop, start)
    await asyncio.sleep(1)

    result = await app.health()
    assert 'tg' in result
    assert result['tg'] is None

    if res['fut'] is not None:
        res['fut'].cancel()
