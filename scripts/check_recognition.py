"""Offline synthetic audio smoke test; does not record the user's microphone."""
import io
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from piper import PiperVoice
from faster_whisper.audio import decode_audio
from rocky.speech import VOICE
from rocky.transcription import Transcriber, Utterance
import webrtcvad
import numpy as np


def main():
    voice = PiperVoice.load(str(VOICE))
    transcriber = Transcriber()
    phrases = ["Remind me in two days to call my friend.",
               "Can you explain Python lists in simple words?",
               "Please open the folder and search for my project."]
    vad = webrtcvad.Vad(1)
    for phrase in phrases:
        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            voice.synthesize_wav(phrase, output)
        audio.seek(0)
        samples = decode_audio(audio, sampling_rate=16000)
        pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
        pcm = bytes(16000) + pcm + bytes(64000)
        utterance = Utterance()
        recording = None
        for offset in range(0, len(pcm) - 959, 960):
            frame = pcm[offset:offset + 960]
            recording = utterance.feed(frame, vad.is_speech(frame, 16000))
            if recording is not None:
                break
        if recording is None:
            raise RuntimeError("Speech endpoint did not finish")
        started = time.monotonic()
        actual = transcriber.transcribe(recording)
        print("Expected:", phrase, flush=True)
        print("Heard:   ", actual, flush=True)
        print("Transcription seconds:", round(time.monotonic() - started, 2), flush=True)
        if not actual:
            raise RuntimeError("Empty transcription")
    print("Synthetic pipeline smoke test passed. Human microphone accuracy remains unmeasured.")


if __name__ == "__main__":
    main()
