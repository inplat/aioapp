import gc
import pytest
import asyncio
from aioapp.app import Application, Component
from aioapp.error import GracefulExit, PrepareError


def test_app_run():
    class Cmp(Component):
        async def prepare(self):
            pass

        async def start(self):
            self.loop.call_later(0.2, self.interrupt)

        async def stop(self):
            pass

        def interrupt(self):
            raise GracefulExit()

    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        app = Application(loop=loop)
        cmp = Cmp()
        app.add('test', cmp)
        assert cmp is app.test
        with pytest.raises(AttributeError):
            app.test2  # not existing component
        with pytest.raises(UserWarning):
            app.add('test', cmp)  # duplicate
        with pytest.raises(UserWarning):
            app.add('test2', {})  # not a component
        with pytest.raises(UserWarning):
            app.add('test3', Cmp(), stop_after=['test4'])  # invalid stop after
        assert app.run() == 0
    finally:
        gc.collect()


def test_app_run_prepare_fail():
    class Cmp(Component):
        async def prepare(self):
            raise PrepareError()

        async def start(self):
            self.loop.call_later(0.2, self.interrupt)

        async def stop(self):
            pass

        def interrupt(self):
            raise GracefulExit()

    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        app = Application(loop=loop)
        app.add('test', Cmp())
        assert app.run() == 1
    finally:
        gc.collect()


def test_app_stop_seq():
    seq = []

    class Cmp(Component):

        def __init__(self, id, is_interrupt=False):
            super().__init__()
            self.is_interrupt = is_interrupt
            self.id = id

        async def prepare(self):
            pass

        async def start(self):
            if self.is_interrupt:
                self.loop.call_later(0.2, self.interrupt)

        async def stop(self):
            seq.append(self.id)

        def interrupt(self):
            raise GracefulExit()

    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        app = Application(loop=loop)
        app.add('test3', Cmp(3), stop_after=[])
        app.add('test1', Cmp(1), stop_after=['test3'])
        app.add('test4', Cmp(4, is_interrupt=True), stop_after=['test1'])
        app.add('test2', Cmp(2), stop_after=['test4', 'test3'])
        assert app.run() == 0

        assert seq == [3, 1, 4, 2]
    finally:
        gc.collect()


async def test_abc_component():
    cmp = Component()
    with pytest.raises(NotImplementedError):
        await cmp.prepare()
    with pytest.raises(NotImplementedError):
        await cmp.start()
    with pytest.raises(NotImplementedError):
        await cmp.stop()
