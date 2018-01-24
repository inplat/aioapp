import re
import asyncio
import aiozipkin.tracer as azt
from aiostatsd.client import StatsdClient
import aiozipkin.constants as azc

STATS_CLEAN_NAME_RE = re.compile('[^0-9a-zA-Z_.-]')
STATS_CLEAN_TAG_RE = re.compile('[^0-9a-zA-Z_=.-]')


class Tracer(azt.Tracer):
    async def stop(self):
        await self.close()


class TracerTransport(azt.Transport):
    def __init__(self, app, driver, addr, metrics_diver, metrics_addr,
                 metrics_name, send_inteval, loop):
        """
        :type tracer: str
        :type tracer_url: str
        :type statsd_addr: str
        :type statsd_prefix: str
        :type send_inteval: float
        :type loop: asyncio.AbstractEventLoop
        """
        if driver is not None and driver != 'zipkin':
            raise UserWarning('Unsupported tracer driver')
        if metrics_diver is not None and metrics_diver != 'statsd':
            raise UserWarning('Unsupported metrics driver')

        addr = addr or ''
        super(TracerTransport, self).__init__(addr,
                                              send_inteval=send_inteval,
                                              loop=loop)
        self.app = app
        self.loop = loop
        self._driver = driver
        self._metrics_diver = metrics_diver
        self._metrics_addr = metrics_addr
        self._metrics_name = metrics_name

        self.stats = None
        if metrics_diver == 'statsd':
            addr = metrics_addr.split(':')
            host = addr[0]
            port = int(addr[1]) if len(addr) > 1 else 8125
            self.stats = StatsdClient(host, port)
            asyncio.ensure_future(self.stats.run(), loop=loop)

    async def close(self):
        if self.stats:
            try:
                await asyncio.sleep(.001, loop=self.loop)
                await self.stats.stop()
            except Exception as e:
                self.app.log_err(e)
        await super(TracerTransport, self).close()

    async def _send(self):
        data = self._queue[:]

        try:
            if self.stats:
                await self._send_to_statsd(data)
        except Exception as e:
            self.app.log_err(e)

        try:
            if self._driver == 'zipkin':
                # TODO отправить pull request в aiozipkin: не отправлять
                # TODO запрос, если self._queue пуста
                await super(TracerTransport, self)._send()
            else:
                self._queue = []
        except Exception as e:
            self.app.log_err(e)

    async def _send_to_statsd(self, data):
        if self.stats:
            for rec in data:
                tags = []
                t = rec['tags']
                if azc.HTTP_PATH in t and 'kind' in rec:
                    name = 'http'
                    if rec["kind"] == 'SERVER':
                        tags.append(('kind', 'in'))
                    else:
                        tags.append(('kind', 'out'))

                    copy_tags = {
                        azc.HTTP_STATUS_CODE: 'status',
                        azc.HTTP_METHOD: 'method',
                        azc.HTTP_HOST: 'host',
                    }
                    for tag_key, tag_name in copy_tags.items():
                        if tag_key in t:
                            tags.append((tag_name, t[tag_key]))

                elif rec['name'].startswith('db:'):
                    name = 'db'
                    tags.append(('kind', rec['name'][len('db:'):]))
                elif rec['name'].startswith('redis:'):
                    name = 'redis'
                    tags.append(('kind', rec['name'][len('redis:'):]))
                else:
                    name = rec['name']

                name = name.replace(' ', '_').replace(':', '_')
                name = self._metrics_name + name
                name = STATS_CLEAN_NAME_RE.sub('', name)

                if len(tags) > 0:
                    for tag in tags:
                        t = tag[1].replace(':', '-')
                        t = STATS_CLEAN_TAG_RE.sub('', t)
                        name += ',' + tag[0] + "=" + t
                self.stats.send_timer(name,
                                      int(round(rec["duration"] / 1000)),
                                      rate=1.0)
