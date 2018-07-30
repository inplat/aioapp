import aioapp.app
import aiozipkin.helpers as azh
from aioapp.tracer import SERVER, CLIENT


async def test_tracer(app: aioapp.app.Application, tracer_server,
                      metrics_server):
    tracer_server[2].clear()
    metrics_server[3].clear()
    with app.tracer.new_trace(sampled=True) as span:
        span.name('test')
        span.kind(SERVER)
        span.tag('key1', '1', metrics=True)
        span.tag('key2', '2')
        span.metrics_tag('key3', '3')
        with span.new_child('test_child', CLIENT):
            pass
        with span.new_child('test_skipped', CLIENT) as span_skipped:
            span_skipped.skip()

    await app.tracer.close()

    req = tracer_server[2]
    if app.tracer.tracer is not None:
        assert len(req) == 1
        assert len(req[0]) == 2

        names = []
        for span in req[0]:
            assert 'traceId' in span
            assert 'name' in span
            assert 'parentId' in span
            assert 'id' in span
            assert 'timestamp' in span
            assert 'duration' in span
            assert 'debug' in span
            assert 'shared' in span
            assert 'localEndpoint' in span
            assert 'remoteEndpoint' in span
            assert 'annotations' in span
            assert 'tags' in span
            names.append(span['name'])

            if span['name'] == 'test':
                assert span['tags'] == {'key1': '1', 'key2': '2'}

        assert 'test' in names
        assert 'test_child' in names
    else:
        assert len(req) == 0

    if app.tracer.metrics is not None:
        print(metrics_server[3])
        assert len(metrics_server[3]) == 2
        names = [line['name'] for line in metrics_server[3]]
        assert 'test_test_child' in names
        assert 'test_test' in names
        for m in metrics_server[3]:
            assert 'duration' in metrics_server[3][0]['duration']
            int(metrics_server[3][0]['time'].strip())
            if m['name'] == 'test_test':
                assert m['tags'] == {'key1': '1', 'key3': '3'}
    else:
        assert len(metrics_server[3]) == 0


async def test_tracer_from_hdrs(request, app: aioapp.app.Application,
                                tracer_server):
    tracer_server[2].clear()
    hdrs = {
        azh.TRACE_ID_HEADER: '5813232b6c610041db4a6ef9d4dcf19b',
        azh.SPAN_ID_HEADER: '5c639fc540090ee6',
        azh.SAMPLED_ID_HEADER: '1',
    }
    with app.tracer.new_trace_from_headers(hdrs) as span:
        span.name('test')
        span.kind(SERVER)

        span_hdrs = span.make_headers()
        assert (span_hdrs[azh.TRACE_ID_HEADER]
                == '5813232b6c610041db4a6ef9d4dcf19b')
        assert span_hdrs[azh.SPAN_ID_HEADER] == span.id

    await app.tracer.close()

    req = tracer_server[2]
    if app.tracer.tracer is not None:
        assert len(req) == 1
        assert req[0][0]['traceId'] == '5813232b6c610041db4a6ef9d4dcf19b'
        assert req[0][0]['id'] == '5c639fc540090ee6'
    else:
        assert len(req) == 0
