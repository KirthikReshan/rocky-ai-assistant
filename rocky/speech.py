"""Offline neural speech, with Windows SAPI as a fallback."""
import io
import wave
from pathlib import Path

VOICE_DIR = Path(__file__).resolve().parents[1] / "models/voices"
VOICE = VOICE_DIR / "en_US-lessac-medium.onnx"


class Speech:
    def __init__(self, report):
        self.report = report
        self.voice = None
        self.config = None
        self.sapi = None
        try:
            import win32com.client
            self.sapi = win32com.client.Dispatch("SAPI.SpVoice")
            self.sapi.Rate = 1
        except Exception as exc:
            report('info', f'Windows voice unavailable: {exc}')
        voice_path = VOICE if VOICE.is_file() else VOICE_DIR / "en_US-ryan-medium.onnx"
        if voice_path.is_file():
            try:
                from piper import PiperVoice, SynthesisConfig
                self.voice = PiperVoice.load(str(voice_path))
                self.config = SynthesisConfig(length_scale=1.08, volume=0.9)
                report("info", f"Using local Piper neural voice ({voice_path.stem}), gentle pacing.")
            except Exception as exc:
                report("info", f"Piper unavailable; using Windows voice: {exc}")
        if self.voice is None and self.sapi is None:
            raise RuntimeError('No speech output engine is available')

    def speak(self, text, device=None, cancelled=None):
        if cancelled and cancelled():
            return
        if self.voice is not None:
            try:
                from rocky.playback import play_pcm
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as output:
                    output.setframerate(self.voice.config.sample_rate)
                    output.setsampwidth(2)
                    output.setnchannels(1)
                    for chunk in self.voice.synthesize(text, syn_config=self.config):
                        if cancelled and cancelled():
                            return
                        output.writeframes(chunk.audio_int16_bytes)
                buffer.seek(0)
                with wave.open(buffer, "rb") as audio:
                    pcm, sample_rate = audio.readframes(audio.getnframes()), audio.getframerate()
            except Exception as exc:
                if cancelled and cancelled():
                    return
                self.report("info", f"Neural speech failed; using Windows voice: {exc}")
                self.voice = None
            else:
                # A disconnected output is not a failed neural voice. Keep Piper
                # available for the next reply and respect the selected device.
                self.report('status', 'Speaking')
                play_pcm(pcm, sample_rate, self.report, device, cancelled=cancelled)
                return
        if self.sapi is None:
            raise RuntimeError('No fallback Windows voice is available; reply is shown as text')
        if cancelled and cancelled():
            return
        # Async speech lets the worker poll cancellation without calling COM on the UI thread.
        self.report('status', 'Speaking')
        self.sapi.Speak(text, 1)
        try:
            while not self.sapi.WaitUntilDone(50):
                if cancelled and cancelled():
                    self.sapi.Speak('', 3)  # Async + purge queued speech.
                    return
        except Exception:
            self.sapi.Speak('', 3)
            raise
