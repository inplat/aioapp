import logging
import asyncio
import aioamqp.protocol
import aioamqp.channel
import aioamqp
from typing import Optional
from .app import Component
from .error import PrepareError
from .misc import mask_url_pwd, async_call

#
aioamqp.channel.logger.level = logging.CRITICAL
aioamqp.protocol.logger.level = logging.CRITICAL


class ConsumerChannel:
    pass


class PublisherChannel:
    pass


class Amqp(Component):

    def __init__(self, url: Optional[str]=None,
                 heartbeat: int = 5,
                 connect_max_attempts: int=10,
                 connect_retry_delay: float=1.0) -> None:
        super().__init__()
        self.url = url
        self.connect_max_attempts = connect_max_attempts
        self.connect_retry_delay = connect_retry_delay
        self.heartbeat = heartbeat
        self._started = False
        self._shutting_down = False
        self._consuming = False
        self._transport = None
        self._protocol = None

    @property
    def _masked_url(self) -> Optional[str]:
        return mask_url_pwd(self.url)

    async def prepare(self) -> None:
        for i in range(self.connect_max_attempts):
            try:
                await self._connect()
                return
            except Exception as e:
                self.app.log_err(str(e))
                raise
                await asyncio.sleep(self.connect_retry_delay,
                                    loop=self.app.loop)
        raise PrepareError("Could not connect to %s" % self._masked_url)

    async def start(self) -> None:
        await self._start_consumers()
        self._started = True

    async def stop(self) -> None:
        self._shutting_down = True
        await self._stop_consumers()
        await self._cleanup()

    async def _connect(self):
        await self._cleanup()
        self.app.log_info("Connecting to %s" % self._masked_url)
        (self._transport,
         self._protocol) = await aioamqp.from_url(self.url,
                                                  on_error=self._con_error,
                                                  heartbeat=self.heartbeat)
        self.app.log_info("Connected to %s" % self._masked_url)
        # TODO open channels and consume
        if not self._consuming:
            await self._start_consumers()

    async def _con_error(self, error):
        if error:
            self.app.log_err(error)
        if self._shutting_down or not self._started:
            return

        async_call(self.loop, self._reconnect,
                   delay=self.connect_retry_delay)

    async def _reconnect(self):
        try:
            await self._connect()
        except Exception as e:
            self.app.log_err(e)
            async_call(self.loop, self._reconnect,
                       delay=self.connect_retry_delay)

    async def _cleanup(self):
        if self._protocol:
            try:
                await self._protocol.close()
            except Exception as e:
                self.app.log_err(e)
            self._protocol = None
            self._transport = None

    async def _start_consumers(self):
        self._consuming = True

    async def _stop_consumers(self):
        self._consuming = False
