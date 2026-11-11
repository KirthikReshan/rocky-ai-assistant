"""Bounded local model scheduling and a fixed-host NVIDIA adapter."""
from contextlib import contextmanager
from http.client import HTTPException
import json
import threading
import urllib.error
import urllib.request

from rocky.brain import BrainError, BrainCancelled, clean_answer

LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LOCAL_SLOT = threading.Lock()
_resident_model = None


def local_json(endpoint, body=None, timeout=120, http=None):
    request = urllib.request.Request('http://127.0.0.1:11434/api/' + endpoint,
        data=None if body is None else json.dumps(body, allow_nan=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    try:
        with (http or LOCAL_HTTP).open(request, timeout=timeout) as response:
            data = response.read(4_000_001)
        if len(data) > 4_000_000:
            raise ValueError('Model response exceeded its limit')
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError('Expected a model response object')
        return payload
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise BrainError('A local model is missing. Check AI models in Settings.') from None
        raise BrainError('Ollama could not process this request. Try a shorter selection.') from None
    except (OSError, HTTPException, ValueError, TypeError):
        raise BrainError('Local Ollama did not return a usable response. Check that it is running.') from None


@contextmanager
def local_slot(model, cancel, generative=True):
    global _resident_model
    while not LOCAL_SLOT.acquire(timeout=.1):
        if cancel.is_set():
            raise BrainCancelled('AI task cancelled.')
    try:
        if cancel.is_set():
            raise BrainCancelled('AI task cancelled.')
        if generative:
            if _resident_model is not None and _resident_model != model:
                # Release only the previous model used by Rocky, never every
                # model in the user's Ollama instance.
                previous = _resident_model
                _resident_model = None
                try:
                    local_json('generate', {'model': previous, 'keep_alive': 0, 'stream': False}, timeout=10)
                except BrainError:
                    # The user may have stopped or removed the model already.
                    pass
        if generative:
            _resident_model = model
        yield
    finally:
        LOCAL_SLOT.release()


def installed_models():
    result = local_json('tags', timeout=5)
    models = result.get('models')
    if not isinstance(models, list):
        raise BrainError('Ollama returned an invalid model list.')
    return {entry['name']: entry.get('digest', '') for entry in models
            if isinstance(entry, dict) and isinstance(entry.get('name'), str)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class NimClient:
    URL = 'https://integrate.api.nvidia.com/v1/chat/completions'

    def __init__(self, vault, model):
        self.vault, self.model = vault, model
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, messages, schema, max_tokens, cancel, temperature):
        if cancel.is_set():
            raise BrainCancelled('AI task cancelled.')
        try:
            key = self.vault.read()
        except OSError:
            raise BrainError('The NVIDIA key is unavailable in Windows Credential Manager.') from None
        if not key:
            raise BrainError('Add your NVIDIA key in Settings → AI models to enable NIM.')
        if len(key) > 2000 or not key.isascii() or any(c.isspace() for c in key):
            raise BrainError('The saved NVIDIA key is invalid.')
        schema = schema or {'type': 'object', 'properties': {'reply': {'type': 'string'}},
                            'required': ['reply'], 'additionalProperties': False}
        data = {'model': self.model, 'messages': messages, 'stream': False,
                'max_tokens': max_tokens, 'temperature': temperature,
                'chat_template_kwargs': {'enable_thinking': False},
                'response_format': {'type': 'json_schema', 'json_schema': {'name': 'rocky_response', 'schema': schema}}}
        request = urllib.request.Request(self.URL, data=json.dumps(data).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with self.http.open(request, timeout=45) as response:
                raw = response.read(1_000_001)
            if cancel.is_set():
                raise BrainCancelled('AI task cancelled.')
            if len(raw) > 1_000_000:
                raise ValueError('Oversized response')
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError('Expected a response object')
            choice = payload['choices'][0]
            if not isinstance(choice, dict):
                raise ValueError('Invalid response choice')
            if choice.get('finish_reason') != 'stop':
                raise ValueError('Incomplete response')
            content = clean_answer(choice['message']['content'])
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError('Expected a JSON object')
            return result
        except BrainCancelled:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise BrainError('NIM rejected the API key or account access.') from None
            if exc.code == 429:
                raise BrainError('NIM reached its rate or account limit.') from None
            raise BrainError('NIM could not complete the request.') from None
        except (OSError, HTTPException, ValueError, KeyError, IndexError, TypeError, AttributeError):
            # Never expose response bodies, request headers or key-bearing errors.
            raise BrainError('NIM was unavailable or returned an invalid answer.') from None
