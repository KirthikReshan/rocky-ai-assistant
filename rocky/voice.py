"""One worker owns audio and speech; follow-up listening starts after each reply."""
import json
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from rocky.core import is_wake
from rocky.assistant import Assistant
from rocky.conversation import Conversation, wants_to_end
from rocky.transcription import Utterance, Transcriber, WHISPER_MODEL
from rocky.voice_benchmark import record_test, levels, score, command_score
from rocky.sentence_stream import SentenceStream, remaining_reply
from rocky.wake import PhraseDetector, evaluate_wake, is_stop_phrase, wake_command
from rocky.voice_interrupt import VoiceInterrupt
from rocky.document_memory import DocumentText


@dataclass(frozen=True)
class _VoiceCommand:
    """A queued voice result belongs to one uninterrupted listening session."""
    text: str
    generation: int


@dataclass(frozen=True)
class _InterruptAudio:
    pcm: bytes
    generation: int


class VoiceWorker(threading.Thread):
    def __init__(self, events, model_path: Path, device=None, assistant=None, follow_up=30, stt="auto"):
        super().__init__(name="rocky-voice", daemon=True)
        self.events = events
        self.model_path = model_path
        self.device = device
        self.output_device = None
        self.silence_seconds = 1.5
        self.recognition_device = 'cpu'
        self.speech_vocabulary = ''
        self.wake_threshold = .55
        self.verify_wake = False
        self.voice_interrupt_enabled = False
        self.stt = stt
        self.assistant = assistant or Assistant()
        self.conversation = Conversation(follow_up)
        self.stopping = threading.Event()
        self.speaking = threading.Event()
        self.speech_cancelled = threading.Event()
        self.reply_superseded = threading.Event()
        self._listening_lock = threading.Lock()
        self._listening_generation = 0
        self.enabled = threading.Event()
        self.enabled.set()
        self.messages = queue.Queue(maxsize=16)
        self.tests = queue.Queue(maxsize=1)
        self.announcements = queue.Queue(maxsize=32)
        self.audio = queue.Queue(maxsize=128)
        self.audio_lost = threading.Event()
        self._last_unheard_at = float('-inf')
        self._document_speech = False

    def unheard(self):
        now = time.monotonic()
        if now - self._last_unheard_at >= 15:
            self.emit('info', "I couldn't make out that sentence. Please try again.")
            self._last_unheard_at = now
        # Noise/empty recognition must not keep a conversation awake indefinitely.

    def emit(self, kind, text):
        self.events.put((kind, text))

    def set_listening(self, enabled):
        with self._listening_lock:
            if enabled:
                self.enabled.set()
            else:
                self._listening_generation += 1
                self.enabled.clear()
                self.conversation.end()
            self.emit('microphone', bool(enabled))

    def _voice_is_current(self, generation):
        with self._listening_lock:
            return (self.enabled.is_set() and not self.stopping.is_set()
                    and generation == self._listening_generation)

    def respond_safely(self, message, on_draft=None):
        try:
            if on_draft is not None:
                return self.assistant.respond(message, on_draft=on_draft)
            return self.assistant.respond(message)
        except Exception as exc:
            # A single tool/model failure must not terminate the command consumer.
            # Never retry actions automatically: they may already have had side effects.
            self.emit('error', f'Command failed ({type(exc).__name__}): {exc}')
            return "I couldn't complete that request. You can try another command."

    def should_end(self, message):
        if message.lower().strip().rstrip('.!?') == 'go to sleep':
            reminders = getattr(self.assistant, 'reminders', None)
            if reminders is not None and getattr(reminders, 'expects_body', False) is True:
                return False  # This can be the answer to "What should I remind you about?"
        return wants_to_end(message)

    def stop_speaking(self):
        if self.speaking.is_set():
            self.speech_cancelled.set()

    def clear_document_messages(self):
        with self.announcements.mutex:
            retained = [item for item in self.announcements.queue if not isinstance(item, DocumentText)]
            removed = len(self.announcements.queue) - len(retained)
            self.announcements.queue.clear()
            self.announcements.queue.extend(retained)
            self.announcements.unfinished_tasks = max(0, self.announcements.unfinished_tasks - removed)
            if self.announcements.unfinished_tasks == 0:
                self.announcements.all_tasks_done.notify_all()
            self.announcements.not_full.notify_all()
        if self._document_speech:
            self.stop_speaking()

    def flush(self):
        while True:
            try:
                self.audio.get_nowait()
            except queue.Empty:
                return

    def run(self):
        pythoncom = None
        com_initialized = False
        speaker = None
        try:
            import pythoncom
            pythoncom.CoInitialize()
            com_initialized = True
        except Exception as exc:
            self.emit('info', f'Windows speech initialization unavailable: {exc}. Text commands remain available.')
        try:
            sd = None
            try:
                import sounddevice as sd
                import winsound
                from vosk import Model, KaldiRecognizer
            except Exception as exc:
                sd = None
                self.set_listening(False)
                self.emit('error', f'Audio recognition unavailable: {exc}. Text commands remain available.')

            speaker = None
            try:
                from rocky.speech import Speech
                speaker = Speech(self.emit)
            except Exception as exc:
                self.emit('error', f'Speech output unavailable: {exc}. Replies will be shown as text.')
            transcriber = None
            vad = None
            if sd is not None and self.stt != "vosk" and (WHISPER_MODEL / "model.bin").is_file():
                try:
                    import webrtcvad
                    self.emit("status", "Loading improved offline speech recognition")
                    transcriber = Transcriber(device=self.recognition_device, report=self.emit)
                    vad = webrtcvad.Vad(1)
                    self.emit("info", f"Command recognition: Whisper small.en, {transcriber.engine}. Wake detection: Vosk.")
                except Exception as exc:
                    self.emit("error", f"Whisper unavailable; using Vosk: {exc}")
            elif self.stt != "vosk":
                self.emit("info", "Whisper model missing; using Vosk. Run scripts/download_stt.py to upgrade.")
            try:
                if sd is not None:
                    self.emit("info", f"Microphone: {sd.query_devices(self.device, 'input')['name']}")
            except Exception as exc:
                self.set_listening(False)
                self.emit("error", f"No microphone available: {exc}. Text still works.")

            def speak(text):
                if isinstance(text, DocumentText) and not text.current():
                    return
                self._document_speech = isinstance(text, DocumentText)
                self.emit("Rocky", text)
                if speaker is not None:
                    self.speech_cancelled.clear()
                    self.speaking.set()
                    self.emit('speaking', True)
                    self.emit("status", "Preparing voice")
                    interruptor = start_interruptor()
                    try:
                        if interruptor is not None:
                            interruptor.set_reference(text)
                        speaker.speak(text, device=self.output_device,
                                      cancelled=lambda: self.speech_cancelled.is_set() or self.stopping.is_set()
                                      or (isinstance(text, DocumentText) and not text.current()))
                    except Exception as exc:
                        self.emit("error", f"Speech output unavailable: {exc}")
                    finally:
                        close_interruptor(interruptor)
                        self.speaking.clear()
                        self.emit('speaking', False)
                self._document_speech = False
                self.flush()

            def respond_and_speak(message):
                self.reply_superseded.clear()
                if speaker is None or getattr(self.assistant, 'supports_sentence_speech', False) is not True:
                    response = self.respond_safely(message)
                    if not self.stopping.is_set():
                        speak(response)
                    return
                self.speech_cancelled.clear()
                self.speaking.set()
                self.emit('speaking', True)
                failed = False
                document_reply = None
                interruptor = start_interruptor()

                def say_sentence(text):
                    nonlocal failed
                    if failed or self.speech_cancelled.is_set() or self.stopping.is_set():
                        return
                    self.emit('status', 'Preparing voice')
                    try:
                        if interruptor is not None:
                            interruptor.set_reference(text)
                        speaker.speak(text, device=self.output_device,
                                      cancelled=lambda: self.speech_cancelled.is_set() or self.stopping.is_set()
                                      or (document_reply is not None and not document_reply.current()))
                    except Exception as exc:
                        failed = True
                        self.emit('error', f'Speech output unavailable: {exc}')

                pending = queue.Queue()
                finished = threading.Event()
                result = []
                spoken = []

                def generate():
                    com_ready = False
                    try:
                        if pythoncom is not None:
                            pythoncom.CoInitialize()
                            com_ready = True
                        sentences = SentenceStream(pending.put_nowait)
                        result.append('Stopped the previous reply.' if self.reply_superseded.is_set() else
                                      self.respond_safely(message, on_draft=sentences.feed))
                        if not self.stopping.is_set():
                            self.emit('Rocky', result[0])
                    except Exception as exc:
                        result.append("I couldn't complete that request. Please try again.")
                        self.emit('error', f'Request failed: {exc}')
                        if not self.stopping.is_set():
                            self.emit('Rocky', result[0])
                    finally:
                        try:
                            if com_ready:
                                pythoncom.CoUninitialize()
                        finally:
                            finished.set()

                generator = threading.Thread(target=generate, name='rocky-reply', daemon=True)
                try:
                    generator.start()
                    while not finished.is_set() and not self.stopping.is_set():
                        try:
                            sentence = pending.get(timeout=.05)
                        except queue.Empty:
                            continue
                        if finished.is_set():
                            break
                        if not self.speech_cancelled.is_set() and not failed:
                            say_sentence(sentence)
                            spoken.append(sentence)
                    if not self.stopping.is_set():
                        generator.join()
                        response = result[0]
                        if isinstance(response, DocumentText) and not response.current():
                            return
                        self._document_speech = isinstance(response, DocumentText)
                        document_reply = response if self._document_speech else None
                        remainder = remaining_reply(response, spoken)
                        if remainder:
                            say_sentence(remainder)
                finally:
                    close_interruptor(interruptor)
                    self.speaking.clear()
                    self._document_speech = False
                    self.emit('speaking', False)
                    self.flush()

            model = None
            if sd is not None and self.model_path.is_dir():
                self.emit("status", "Loading offline speech model")
                try:
                    model = Model(str(self.model_path))
                except Exception as exc:
                    self.emit('error', f'Speech model could not load: {exc}. Text commands remain available.')
            else:
                self.emit("info", "Speech model missing. Text and speech output are available.")
            if model is None:
                self.set_listening(False)
            self.emit('VoiceReady', {
                'speech': 'Piper' if speaker is not None and speaker.voice is not None else
                          'Windows voice' if speaker is not None else 'Unavailable',
                'recognition': 'Whisper' if transcriber is not None else 'Vosk' if model is not None else 'Text only',
                'wake': model is not None,
            })

            def start_interruptor():
                if not self.voice_interrupt_enabled or model is None or sd is None or not self.enabled.is_set():
                    return None
                with self._listening_lock:
                    generation = self._listening_generation
                device = self.device
                def queue_interruption(pcm):
                    if self._voice_is_current(generation):
                        try:
                            self.messages.put_nowait(_InterruptAudio(pcm, generation))
                            self.reply_superseded.set()
                            cancel = getattr(self.assistant, 'cancel_current', None)
                            if callable(cancel):
                                cancel()
                            else:
                                brain = getattr(self.assistant, 'brain', None)
                                if brain is not None and callable(getattr(brain, 'cancel_current', None)):
                                    brain.cancel_current()
                        except queue.Full:
                            self.emit('info', 'Message queue full. Please repeat the new question.')
                listener = VoiceInterrupt(sd, model, device,
                                          lambda: (self.voice_interrupt_enabled and self.device == device
                                                   and self._voice_is_current(generation)),
                                          self.stop_speaking, self.emit, on_command=queue_interruption)
                listener.start()
                return listener

            def close_interruptor(listener):
                if listener is not None and not listener.close():
                    self.set_listening(False)
                    self.emit('info', 'Voice stop is still releasing the microphone. Resume it after the device recovers.')

            def callback(data, frames, timing, status):
                if status:
                    self.audio_lost.set()
                try:
                    self.audio.put_nowait(bytes(data))
                except queue.Full:
                    self.audio_lost.set()

            def capture(active):
                recognizer = KaldiRecognizer(model, 16000) if active else None
                wake_threshold = self.wake_threshold
                wake_detector = None if active else PhraseDetector(model, threshold=wake_threshold, prefix=True)
                self.flush()
                self.audio_lost.clear()
                pause_seconds = self.silence_seconds
                utterance = Utterance(silence_seconds=pause_seconds)
                partial = ""
                started = time.monotonic()
                capture_device = self.device
                with sd.RawInputStream(samplerate=16000, blocksize=480,
                                       device=capture_device, dtype="int16", channels=1,
                                       callback=callback):
                    while self.enabled.is_set() and not self.stopping.is_set():
                        if self.device != capture_device:
                            return "device", ""
                        if not active and self.wake_threshold != wake_threshold:
                            return "device", ""
                        if self.silence_seconds != pause_seconds and not utterance.started:
                            return "device", ""
                        if not self.messages.empty():
                            return "queued", ""
                        if not self.tests.empty():
                            return "queued", ""
                        if not self.announcements.empty():
                            return "queued", ""
                        if self.audio_lost.is_set():
                            self.emit("info", "Audio was interrupted. Please repeat your sentence.")
                            return "retry", ""
                        if active and not utterance.started and not partial and not self.conversation.active:
                            return "idle", ""
                        try:
                            data = self.audio.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        if not active:
                            if wake_detector.feed(data):
                                return ('wake_command_candidate' if wake_detector.has_suffix else 'wake_candidate',
                                        bytes(wake_detector.audio))
                            continue
                        if active and transcriber is not None:
                            pcm = utterance.feed(data, vad.is_speech(data, 16000))
                            if pcm is not None:
                                return "audio", pcm
                            continue
                        if recognizer.AcceptWaveform(data):
                            text = json.loads(recognizer.Result()).get("text", "")
                            if text:
                                if is_wake(text):
                                    return "wake", text
                                if active:
                                    return "command", text
                            partial = ""
                        else:
                            current = json.loads(recognizer.PartialResult()).get("partial", "")
                            if current != partial:
                                partial = current
                            # Never end speech merely because partial text stops changing.
                            if active and partial and time.monotonic() - started > 60:
                                final = json.loads(recognizer.FinalResult()).get("text", "")
                                return "command", final or partial
                return "paused", ""

            while not self.stopping.is_set():
                if transcriber is not None:
                    transcriber.vocabulary = self.speech_vocabulary
                try:
                    test = self.tests.get_nowait()
                except queue.Empty:
                    test = None
                if test is not None:
                    self.conversation.end()
                    self.emit('status', 'Opening microphone for the test')
                    try:
                        if sd is None:
                            raise RuntimeError('Audio input is unavailable.')
                        device = self.device
                        wake_threshold, verify_wake = self.wake_threshold, self.verify_wake
                        device_name = sd.query_devices(device, 'input')['name']
                        pcm, overflow = record_test(sd, device, self.stopping,
                                                    lambda value: self.emit('TestLevel', value),
                                                    started=lambda: self.emit('TestStarted', None))
                        result = dict(test, **levels(pcm), overflow=overflow,
                                      device=device_name)
                        if test['expected']:
                            self.emit('status', 'Checking the test recording')
                            started = time.monotonic()
                            if test.get('test_kind', 'command') != 'command':
                                if model is None:
                                    raise RuntimeError('Wake-word model is unavailable.')
                                if verify_wake and transcriber is None:
                                    raise RuntimeError('Wake verification needs Whisper. Check setup status.')
                                detected = evaluate_wake(pcm, model, wake_threshold,
                                                         transcriber if verify_wake else None)
                                result.update(heard='Wake activated' if detected else 'No wake activation',
                                              engine='Vosk + Whisper' if verify_wake else 'Vosk',
                                              word_errors=0, reference_words=0,
                                              exact=detected == (test['test_kind'] == 'wake_positive'),
                                              wake_detected=int(detected),
                                              wake_baseline=int(evaluate_wake(pcm, model, threshold=0)))
                                result['wake_config'] = f"threshold {wake_threshold:.2f}, verification {verify_wake}"
                            else:
                                if transcriber is None:
                                    raise RuntimeError('Whisper is unavailable. Check the voice setup messages.')
                                result['heard'] = transcriber.transcribe(pcm)
                                result['engine'] = transcriber.engine
                                result.update(score(test['expected'], result['heard']))
                                result['action_correct'] = command_score(test['expected'], result['heard'], test.get('nicknames'))
                            result['latency'] = time.monotonic() - started
                        self.emit('TestResult', result)
                    except Exception as exc:
                        self.emit('TestError', str(exc))
                    self.flush()
                    continue
                try:
                    announcement = self.announcements.get_nowait()
                except queue.Empty:
                    announcement = None
                if announcement is not None:
                    speak(announcement)
                    continue
                try:
                    message = self.messages.get_nowait()
                except queue.Empty:
                    message = None
                if message is not None:
                    if isinstance(message, _InterruptAudio):
                        if not self._voice_is_current(message.generation):
                            continue
                        generation = message.generation
                        try:
                            if transcriber is not None:
                                transcript = transcriber.transcribe(message.pcm)
                            else:
                                decoder = KaldiRecognizer(model, 16000)
                                chunks = []
                                for offset in range(0, len(message.pcm), 9600):
                                    if decoder.AcceptWaveform(message.pcm[offset:offset + 9600]):
                                        chunks.append(json.loads(decoder.Result()).get('text', ''))
                                chunks.append(json.loads(decoder.FinalResult()).get('text', ''))
                                transcript = ' '.join(chunks)
                            text = wake_command(transcript)
                        except Exception as exc:
                            self.emit('error', f'Could not recognize interruption: {exc}')
                            continue
                        if not self._voice_is_current(generation):
                            continue
                        self.conversation.renew()
                        if not text:
                            continue
                        self.emit('You', text)
                        message = _VoiceCommand(text, generation)
                    voice_message = message if isinstance(message, _VoiceCommand) else None
                    if voice_message is not None:
                        if not self._voice_is_current(voice_message.generation):
                            continue
                        message = voice_message.text
                    if is_stop_phrase(message):
                        self.stop_speaking()
                        self.conversation.renew()
                        self.emit('info', 'Voice stop received.')
                        continue
                    if self.should_end(message):
                        self.conversation.end()
                        speak("Talk to you later.")
                        continue
                    self.emit("status", "Working on your request (microphone paused)")
                    if voice_message is not None and not self._voice_is_current(voice_message.generation):
                        continue
                    respond_and_speak(message)
                    if not self.stopping.is_set():
                        self.conversation.renew()
                    continue
                if model is None or not self.enabled.is_set():
                    self.conversation.end()
                    self.emit("status", "Text only" if model is None else "Microphone paused")
                    self.stopping.wait(0.1)
                    continue
                active = self.conversation.active
                self.emit("status", "Listening — you can keep talking" if active else 'Say "Hey Rocky" to start')
                with self._listening_lock:
                    capture_generation = self._listening_generation
                try:
                    kind, heard = capture(active)
                    if not self._voice_is_current(capture_generation):
                        continue
                    if kind in {'wake_candidate', 'wake_command_candidate'}:
                        combined = kind == 'wake_command_candidate'
                        if self.verify_wake:
                            if transcriber is None:
                                self.emit('info', 'Wake verification needs Whisper. Check setup status or turn verification off.')
                                continue
                        transcript = None
                        suffix = None
                        if self.verify_wake or combined:
                            try:
                                if transcriber is not None:
                                    transcript = transcriber.transcribe(heard)
                                else:
                                    # Decode command words with the full vocabulary, never
                                    # pass the constrained wake grammar's [unk] to tools.
                                    decoder = KaldiRecognizer(model, 16000)
                                    words = []
                                    for offset in range(0, len(heard), 9600):
                                        if decoder.AcceptWaveform(heard[offset:offset + 9600]):
                                            words.append(json.loads(decoder.Result()).get('text', ''))
                                    words.append(json.loads(decoder.FinalResult()).get('text', ''))
                                    transcript = ' '.join(filter(None, words)).strip()
                            except Exception as exc:
                                self.emit('error', f'Transcription failed; please try again: {exc}')
                                continue
                            suffix = wake_command(transcript)
                            if self.verify_wake and suffix is None:
                                continue
                        if transcript and (combined or suffix):
                            heard = suffix if suffix is not None else transcript
                            kind = 'command' if heard else 'wake'
                        else:
                            kind = 'wake'
                    if kind == "audio":
                        self.emit("status", "Transcribing your sentence locally")
                        try:
                            heard = transcriber.transcribe(heard)
                        except Exception as exc:
                            self.emit("error", f"Transcription failed; please try again: {exc}")
                            heard = ""
                        if self.stopping.is_set() or not self.enabled.is_set():
                            continue
                        if not heard:
                            self.unheard()
                            continue
                        kind = "wake" if is_wake(heard) else "command"
                    if kind == 'command':
                        suffix = wake_command(heard)
                        if suffix is not None:
                            heard = suffix
                            kind = 'command' if suffix else 'wake'
                    # Pausing can happen while Vosk or wake verification is decoding.
                    # Discard that result just as we do for an in-flight Whisper command.
                    if not self._voice_is_current(capture_generation):
                        continue
                    if kind == "wake":
                        try:
                            winsound.Beep(660, 100)
                        except RuntimeError:
                            pass
                        with self._listening_lock:
                            if self.enabled.is_set() and capture_generation == self._listening_generation:
                                self.conversation.renew()
                    elif kind == "command" and heard:
                        self.emit("You", heard)
                        try:
                            self.messages.put_nowait(_VoiceCommand(heard, capture_generation))
                        except queue.Full:
                            self.emit("info", "Message queue full; please try again.")
                    elif kind in {"idle", "paused"}:
                        self.conversation.end()
                except Exception as exc:
                    self.set_listening(False)
                    self.emit("error", f"Microphone unavailable: {exc}. Text still works; resume after fixing the device.")
        except Exception as exc:
            self.set_listening(False)
            self.emit("error", f"Voice worker stopped: {exc}")
        finally:
            # Release the SAPI COM object on its owning thread before COM shuts down.
            speaker = None
            if com_initialized:
                pythoncom.CoUninitialize()
