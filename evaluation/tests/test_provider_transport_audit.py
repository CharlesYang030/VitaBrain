import httpx
import pytest
from agentlab.config import ProviderProfile
from agentlab.providers.openai_compatible import OpenAICompatibleProvider


def test_retry_audit_counts_http_attempts_without_inventing_usage(monkeypatch):
    from agentlab.providers import openai_compatible as module
    p = ProviderProfile(name='test', protocol='openai_chat', model='test', base_url='https://test.invalid', secret_env='TEST_PROVIDER_TOKEN', max_retries=2)
    monkeypatch.setenv('TEST_PROVIDER_TOKEN', 'unused')
    responses = [503, 200]
    payloads = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            payloads.append(kwargs['json'])
            return httpx.Response(responses.pop(0), request=httpx.Request('POST', url),
                json={'choices':[{'message':{'content':'ok'}}], 'usage':{'total_tokens':9}})
    monkeypatch.setattr(module.httpx, 'Client', Client)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    result = OpenAICompatibleProvider(p).complete([{'role':'user','content':'hello'}])
    audit = result.raw['_transport_audit']
    assert audit['retry_count'] == 1
    assert [r['http_status'] for r in audit['attempts']] == [503, 200]
    assert audit['backoff_ms'] == 20000
    assert result.usage == {'total_tokens':9}
    assert payloads[0] == payloads[1]
    assert '_transport_audit' not in payloads[0]


def test_400_keeps_provider_reason_when_retries_explicitly_disabled(monkeypatch):
    from agentlab.providers import openai_compatible as module
    p = ProviderProfile(name='test', protocol='openai_chat', model='test',
        base_url='https://test.invalid', secret_env='TEST_PROVIDER_TOKEN', max_retries=0)
    monkeypatch.setenv('TEST_PROVIDER_TOKEN', 'private-key-value')
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append(kwargs['json'])
            return httpx.Response(400, request=httpx.Request('POST', url),
                headers={'x-request-id': 'diagnostic-123'},
                json={'error': {'message': 'Invalid image', 'type': 'invalid_request_error',
                    'code': 'invalid_image', 'param': 'messages',
                    'api_key': 'private-key-value', 'request': kwargs['json']}})
    monkeypatch.setattr(module.httpx, 'Client', Client)
    with pytest.raises(RuntimeError) as raised:
        OpenAICompatibleProvider(p).complete([{'role': 'user', 'content': 'private prompt'}])
    assert len(calls) == 1
    assert raised.value.transport_audit['retry_count'] == 0
    details = raised.value.transport_audit['attempts'][0]['provider_error']
    assert details['code'] == 'invalid_image'
    assert details['request_ids'] == {'x-request-id': 'diagnostic-123'}
    assert 'Invalid image' in str(raised.value)
    assert 'diagnostic-123' in str(raised.value)
    assert 'private prompt' not in str(details)
    assert 'private-key-value' not in str(details)


@pytest.mark.parametrize('as_json', [True, False])
def test_error_details_redact_credentials_and_bound_body(monkeypatch, as_json):
    p = ProviderProfile(name='test', protocol='openai_chat', model='test',
        base_url='https://test.invalid', secret_env='TEST_PROVIDER_TOKEN')
    monkeypatch.setenv('TEST_PROVIDER_TOKEN', 'private-key-value')
    message = ('private-key-value Bearer another-secret sk-abcdefghijklmnop '
               'data:image/png;base64,QUJDRA== ' + 'x' * 5000)
    kwargs = {'json': {'error': {'message': message}}} if as_json else {'text': message}
    response = httpx.Response(400, request=httpx.Request('POST', 'https://test.invalid'), **kwargs)
    details = OpenAICompatibleProvider(p)._error_details(response)
    assert len(details['message']) <= 1200
    for value in ('private-key-value', 'another-secret', 'sk-abcdefghijklmnop', 'QUJDRA=='):
        assert value not in details['message']
    assert '[image omitted]' in details['message']
