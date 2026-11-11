"""Read-only checks for Rocky's local AI and voice setup."""

import json
from pathlib import Path
import urllib.request
from http.client import HTTPException

from rocky.speech import VOICE, VOICE_DIR
from rocky.transcription import WHISPER_MODEL


def check_ollama(model, offline=False):
    if offline:
        return 'AI chat is disabled for this launch. Restart without the offline option to enable it.'
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with http.open('http://127.0.0.1:11434/api/tags', timeout=3) as response:
            payload = json.loads(response.read(1_000_001))
        models = payload['models']
        if not isinstance(models, list):
            raise ValueError('Expected model list')
        installed = {entry.get('name', entry.get('model')) for entry in models if isinstance(entry, dict)}
        wanted = model if ':' in model.rsplit('/', 1)[-1] else model + ':latest'
        if model in installed or wanted in installed:
            return f'Ollama is reachable. {model} is installed.'
        return f'Ollama is reachable, but {model} is missing. In PowerShell run: ollama pull {model}'
    except (OSError, HTTPException):
        return 'Ollama is unreachable. Open Ollama, wait for it to start, then click Check again.'
    except (ValueError, KeyError, TypeError):
        return 'Ollama returned an unexpected response. Check again or restart Ollama.'


def check_audio(direction, device):
    try:
        import sounddevice as sd

        info = sd.query_devices(device, direction)
        if direction == 'input':
            sd.check_input_settings(device=device, samplerate=16000, channels=1, dtype='int16')
        else:
            sd.check_output_settings(device=device, samplerate=info['default_samplerate'],
                                     channels=1, dtype='int16')
        return f"{info['name']} supports Rocky's audio format."
    except Exception as exc:
        return f'Audio device unavailable: {exc}. Select a device in Settings and check again.'


def check_setup(model, wake_path, input_device=None, output_device=None, offline=False, stt='auto'):
    wake_path = Path(wake_path)
    graph_present = ((wake_path / 'graph/HCLG.fst').is_file() or
                     all((wake_path / name).is_file() for name in ('graph/HCLr.fst', 'graph/Gr.fst')))
    wake_present = graph_present and all((wake_path / name).is_file()
                                        for name in ('am/final.mdl', 'conf/model.conf'))
    whisper_files = ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')
    whisper_present = all((WHISPER_MODEL / name).is_file() for name in whisper_files)
    voice = VOICE if VOICE.is_file() else VOICE_DIR / 'en_US-ryan-medium.onnx'
    piper_present = voice.is_file() and Path(str(voice) + '.json').is_file()
    return [
        ('AI model', check_ollama(model, offline)),
        ('Microphone', check_audio('input', input_device)),
        ('Speakers', check_audio('output', output_device)),
        ('Wake-word files', 'Vosk files are present.' if wake_present else
         'Vosk files are missing or incomplete. Restore the wake-word model files and restart Rocky.'),
        ('Speech recognition', 'Vosk mode was selected for this launch.' if stt == 'vosk' else
         'Whisper files are present.' if whisper_present else
         'Whisper files are missing or incomplete. Restore the Whisper model files for improved recognition.'),
        ('Neural voice files', 'Piper voice files are present.' if piper_present else
         'Piper files are missing. Rocky will try the installed Windows voice.'),
    ]
