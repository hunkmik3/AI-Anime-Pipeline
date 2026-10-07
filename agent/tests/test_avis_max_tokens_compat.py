import asyncio
import json

import httpx
import pytest

from flowboard.services import avis_text as avis


ERROR = 'maxTokens is not supported by this model'


def response(kind, detail):
    if kind == 'http':
        return httpx.Response(400, json={'errors': [detail]})
    return httpx.Response(200, text='data: '+json.dumps({'error': detail})+'\n\n')


def success():
    return httpx.Response(200, text='data: {"delta":"{}","finishReason":"stop"}\n\n')


@pytest.fixture
def mock_avis(monkeypatch):
    monkeypatch.setenv('AVIS_API_KEY', 'test-key-no-provider-access')
    monkeypatch.setattr(avis, '_NO_MAX_TOKENS', {'gpt-6-luna', 'gpt-6-astra'})
    monkeypatch.setattr(avis, '_NO_TEMPERATURE', {'gpt-6-luna', 'gpt-6-astra'})
    requests, responses = [], []

    def handle(request):
        requests.append(json.loads(request.content))
        assert responses, 'Unexpected request/retry'
        return responses.pop(0)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(avis.httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs))
    return requests, responses


def complete(model='test-model', attempts=4):
    return asyncio.run(avis.complete(model, [{'role': 'user', 'content': 'Return JSON.'}],
                                    temperature=.2, max_tokens=123, attempts=attempts))


@pytest.mark.parametrize('model', ['test-model', 'other-supported-model'])
def test_supported_model_keeps_requested_max_tokens(mock_avis, model):
    requests, responses = mock_avis
    responses.append(success())
    assert complete(model, 1).text == '{}'
    assert requests[0]['maxTokens'] == 123
    assert 'temperature' in requests[0]


@pytest.mark.parametrize('model', ['gpt-6-luna', 'gpt-6-astra'])
def test_known_gpt_omits_confirmed_unsupported_parameters_on_first_request(mock_avis, model):
    requests, responses = mock_avis
    responses.append(success())
    complete(model, 1)
    assert 'maxTokens' not in requests[0] and 'temperature' not in requests[0]


@pytest.mark.parametrize('kind', ['http', 'sse'])
def test_explicit_rejection_is_learned_for_retry_and_next_call(mock_avis, kind):
    requests, responses = mock_avis
    responses.extend([response(kind, ERROR), success(), success()])
    assert complete(attempts=2).text == '{}'
    assert complete(attempts=1).text == '{}'
    assert requests[0]['maxTokens'] == 123
    assert all('maxTokens' not in r for r in requests[1:])
    assert all(r['temperature'] == .2 for r in requests)
    assert 'test-model' in avis._NO_MAX_TOKENS


@pytest.mark.parametrize('kind', ['http', 'sse'])
def test_parameter_rejection_does_not_add_attempts(mock_avis, kind):
    requests, responses = mock_avis
    responses.append(response(kind, ERROR))
    with pytest.raises(avis.AvisTextError, match='gave up after 1 attempts'):
        complete(attempts=1)
    assert len(requests) == 1
    assert 'test-model' in avis._NO_MAX_TOKENS


@pytest.mark.parametrize('kind', ['http', 'sse'])
def test_ambiguous_parameter_error_is_not_learned_or_retried(mock_avis, kind):
    requests, responses = mock_avis
    responses.append(response(kind, 'Unknown maxTokens processing failure'))
    with pytest.raises(avis.AvisTextError):
        complete()
    assert len(requests) == 1 and 'test-model' not in avis._NO_MAX_TOKENS


@pytest.mark.parametrize('kind', ['http', 'sse'])
def test_content_refusal_takes_precedence_over_parameter_text(mock_avis, kind):
    requests, responses = mock_avis
    responses.append(response(kind, 'Content policy refusal: '+ERROR))
    with pytest.raises(avis.AvisContentRefusal):
        complete()
    assert len(requests) == 1 and 'test-model' not in avis._NO_MAX_TOKENS


def test_safety_finish_reason_is_not_a_compatibility_retry(mock_avis):
    requests, responses = mock_avis
    responses.append(httpx.Response(200, text='data: '+json.dumps(
        {'error': ERROR, 'finishReason': 'safety'})+'\n\n'))
    with pytest.raises(avis.AvisContentRefusal):
        complete()
    assert len(requests) == 1 and 'test-model' not in avis._NO_MAX_TOKENS


def test_partial_output_is_not_replayed_or_used_to_learn_parameters(mock_avis):
    requests, responses = mock_avis
    responses.append(httpx.Response(200, text='data: {"delta":"partial"}\n\n'
                                    +'data: '+json.dumps({'error': ERROR})+'\n\n'))
    assert complete().text == 'partial'
    assert len(requests) == 1 and 'test-model' not in avis._NO_MAX_TOKENS


@pytest.mark.parametrize('kind', ['http', 'sse'])
def test_temperature_compatibility_keeps_existing_semantics(mock_avis, kind):
    requests, responses = mock_avis
    responses.extend([response(kind, 'temperature is not supported by this model'), success()])
    assert complete(attempts=2).text == '{}'
    assert 'temperature' in requests[0] and 'temperature' not in requests[1]
    assert all(r['maxTokens'] == 123 for r in requests)
    assert 'test-model' in avis._NO_TEMPERATURE and 'test-model' not in avis._NO_MAX_TOKENS
