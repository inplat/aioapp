import logging
import asyncio
import aioamqp.channel  # noqa
import aioamqp.envelope  # noqa
import aioamqp.properties
from aioapp.app import Application
from aioapp import amqp
from aioapp.tracer import Span


class PubChannel(amqp.Channel):
    name = 'pub'

    async def start(self) -> None:
        await self.open()


class SubChannel(amqp.Channel):
    name = 'sub'

    def __init__(self):
        super().__init__()
        self.queue = None

    async def start(self) -> None:
        await self.open()
        result = await self._safe_declare_queue(exclusive=True)
        if result is not None:
            self.queue = result['queue']
            print(self.queue)
            await self.consume(self.msg, self.queue)
            await self.publish(None, b'msg', '', self.queue)  # type: ignore

    async def msg(self, ctx: Span,
                  channel: aioamqp.channel.Channel,
                  body: bytes,
                  envelope: aioamqp.envelope.Envelope,
                  properties: aioamqp.properties.Properties) -> None:
        await self.ack(ctx, envelope.delivery_tag)
        print('MESSAGE', body)
        await asyncio.sleep(1)
        if self.amqp:
            ch = self.amqp.channel('pub')
            if ch:
                await ch.publish(ctx, b'123', '', self.queue)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    loop = asyncio.get_event_loop()
    app = Application(
        loop=loop
    )
    app.add(
        'amqp',
        amqp.Amqp(
            url='amqp://guest:guest@localhost:10901/',
            channels=[
                PubChannel(),
                SubChannel()
            ]
        )
    )

    app.run()
