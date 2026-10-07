import asyncio
import pytest

from flowboard.services import atrium_text, avis_text


def test_gemini_payload_preserves_roles_and_source_image_bytes(monkeypatch):
    monkeypatch.setenv('FLOWBOARD_ATRIUM_THINKING_BUDGET', '4096')
    body = atrium_text.build_body('atrium:gemini-2.5-flash', [
        {'role': 'system', 'content': 'source rules'},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'frame e1'},
            {'type': 'imageBase64', 'data': 'YWJj', 'mediaType': 'image/jpeg'}]},
        {'role': 'assistant', 'content': '{"old":true}'},
    ], max_tokens=12000)
    assert body['model'] == 'gemini-2.5-flash'
    assert body['config']['systemInstruction']['parts'] == [{'text': 'source rules'}]
    assert body['contents'][0]['parts'][1]['inlineData'] == {'mimeType': 'image/jpeg', 'data': 'YWJj'}
    assert body['contents'][1]['role'] == 'model'
    assert 'safetySettings' not in body
    assert body['config']['maxOutputTokens'] == 12000 + 4096
    assert body['config']['thinkingConfig'] == {'thinkingBudget': 4096}


@pytest.mark.parametrize('data', [
    {'promptFeedback': {'blockReason': 'SAFETY'}},
    {'candidates': [{'finishReason': 'SAFETY'}]},
    {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'I cannot assist with that request.'}]}}]},
])
def test_atrium_refusal_is_terminal(data):
    with pytest.raises(avis_text.AvisContentRefusal):
        atrium_text.parse_response('atrium:gemini-2.5-flash', data)


def test_atrium_preserves_truncation_and_excludes_thoughts():
    result = atrium_text.parse_response('atrium:gemini-2.5-flash', {
        'candidates': [{'finishReason': 'MAX_TOKENS', 'content': {'parts': [
            {'thought': True, 'text': 'private reasoning'}, {'text': '{"partial":'}]}}],
        'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 3}})
    assert result.text == '{"partial":'
    assert result.finish_reason == 'length'
    assert result.model == 'atrium:gemini-2.5-flash'


def test_explicit_provider_selection_does_not_require_avis_key(monkeypatch):
    monkeypatch.delenv('AVIS_API_KEY', raising=False)
    calls = []
    async def fake(model, messages, **kwargs):
        calls.append((model, messages, kwargs))
        return avis_text.Completion(text='{}', model=model)
    monkeypatch.setattr(atrium_text, 'complete', fake)
    result = asyncio.run(avis_text.complete('atrium:gemini-2.5-flash', [], attempts=1))
    assert result.model.startswith('atrium:')
    assert len(calls) == 1


def test_atrium_converts_inline_media_to_urls_without_changing_text(monkeypatch):
    seen = []
    def publish(part):
        seen.append(part.copy())
        return {'mimeType': part['mimeType'], 'fileUri': 'https://example.test/source.jpg'}
    monkeypatch.setattr(atrium_text, '_publish_image', publish)
    body = atrium_text.build_body('atrium:gemini-2.5-flash', [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'source e1'},
        {'type': 'imageBase64', 'data': 'YWJj', 'mediaType': 'image/jpeg'}]}])
    result = asyncio.run(atrium_text.prepare_body(body))
    assert seen == [{'mimeType': 'image/jpeg', 'data': 'YWJj'}]
    assert result['contents'][0]['parts'] == [{'text': 'source e1'}, {
        'fileData': {'mimeType': 'image/jpeg', 'fileUri': 'https://example.test/source.jpg'}}]
