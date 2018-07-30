import asyncio
from aioapp.app import Application, Component


class SomeComponent(Component):

    async def prepare(self):
        print('connecting to database')

    async def start(self):
        # do not run here infinity task. this is just trigger on start
        print('ready to executing')

    async def stop(self):
        print('closing database connection')

    async def health(self, ctx):
        some_thing_wrong = False
        if some_thing_wrong:
            raise Exception('Something wrong')


class MyApp(Application):
    def __init__(self, loop, on_start=None):
        super().__init__(loop=loop, on_start=on_start)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()

    app = MyApp(loop=loop)
    app.run()
