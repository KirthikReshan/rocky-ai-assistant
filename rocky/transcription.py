"""Local command transcription and speech-based endpointing."""
from collections import deque
from pathlib import Path
import re
from collections import Counter
import gc

WHISPER_MODEL = Path(__file__).resolve().parents[1] / "models/whisper-small.en"
APP_VOCABULARY = 'WhatsApp, VS Code, OpenAI Codex, YouTube, Ollama'
_cuda_libraries = []
_cuda_directories = []


def prepare_cuda():
    """Load bundled CUDA math libraries without changing system-wide paths."""
    if _cuda_libraries:
        return
    import ctypes
    import os
    import nvidia.cublas

    directory = Path(next(iter(nvidia.cublas.__path__))) / 'bin'
    handle = os.add_dll_directory(str(directory))
    try:
        libraries = [ctypes.WinDLL(str(directory / name))
                     for name in ('cublasLt64_12.dll', 'cublas64_12.dll')]
    except OSError:
        handle.close()
        raise
    _cuda_libraries.extend(libraries)
    _cuda_directories.append(handle)


def suspicious_transcript(text):
    """Reject looping phrases, without deleting genuine individual word repeats."""
    words = re.findall(r"[a-z0-9']+", text.lower())
    for size in range(4, 9):
        phrases = Counter(tuple(words[i:i + size]) for i in range(len(words) - size + 1))
        if phrases and max(phrases.values()) >= 4:
            return True
    return False


class Utterance:
    """30 ms frames; retain pre-roll and wait for actual silence, not stable text."""
    def __init__(self, silence_seconds=1.5, max_seconds=30):
        self.pre_roll = deque(maxlen=20)
        self.votes = deque(maxlen=5)
        self.frames = []
        self.started = False
        self.silent_frames = 0
        self.silence_limit = round(silence_seconds / .03)
        self.frame_limit = round(max_seconds / .03)

    def feed(self, pcm, voiced):
        if not self.started:
            self.pre_roll.append(pcm)
            self.votes.append(voiced)
            if sum(self.votes) >= 3:
                self.started = True
                self.frames.extend(self.pre_roll)
            return None
        self.frames.append(pcm)
        self.silent_frames = 0 if voiced else self.silent_frames + 1
        if self.silent_frames >= self.silence_limit or len(self.frames) >= self.frame_limit:
            return b"".join(self.frames)
        return None


class Transcriber:
    def __init__(self, device='cpu', report=None):
        self.report = report or (lambda *args: None)
        self.model = None
        self.device = 'cpu'
        self.vocabulary = ''
        if device == 'cuda':
            try:
                prepare_cuda()
                self._load('cuda')
            except (RuntimeError, OSError, ImportError) as exc:
                self._fallback(exc)
        else:
            self._load('cpu')
        self.report('SpeechEngine', self.engine)

    @property
    def engine(self):
        return 'NVIDIA GPU' if self.device == 'cuda' else 'CPU'

    def _load(self, device):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(str(WHISPER_MODEL), device=device,
                                  compute_type='int8_float16' if device == 'cuda' else 'int8',
                                  cpu_threads=6, local_files_only=True)
        self.device = device

    def _fallback(self, error):
        self.model = None
        gc.collect()
        self.report('info', f'GPU recognition unavailable; switching to CPU: {error}')
        self._load('cpu')
        self.report('SpeechEngine', self.engine)

    def transcribe(self, pcm):
        import numpy as np
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            return self._decode(samples)
        except (RuntimeError, OSError) as exc:
            if self.device != 'cuda':
                raise
            self._fallback(exc)
            return self._decode(samples)

    def _decode(self, samples):
        hotwords = f'{APP_VOCABULARY}, {self.vocabulary}' if self.vocabulary else APP_VOCABULARY
        segments, _ = self.model.transcribe(samples, language="en", beam_size=5,
                                            condition_on_previous_text=False,
                                            vad_filter=True, temperature=0,
                                            hotwords=hotwords,
                                            repetition_penalty=1.1, no_repeat_ngram_size=6,
                                            max_new_tokens=256)
        parts = []
        for segment in segments:
            # A failed segment invalidates the whole command: don't invent missing words.
            if (segment.avg_logprob < -1.2 or segment.no_speech_prob > .6
                    or segment.compression_ratio > 2.4):
                return ""
            parts.append(segment.text.strip())
        text = " ".join(parts).strip()
        return "" if suspicious_transcript(text) else text
