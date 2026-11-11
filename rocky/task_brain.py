"""Task-specific models, local fallback and opt-in NIM for code/documents."""
from contextlib import contextmanager
from collections import Counter
from dataclasses import replace
import re
import threading

from rocky.brain import OllamaBrain, BrainError, BrainCancelled, clean_answer
from rocky.model_settings import ModelSettings, CredentialVault
from rocky.model_transport import NimClient, local_slot

LANGUAGES = {
    'english': 'en', 'tamil': 'ta', 'hindi': 'hi', 'bengali': 'bn', 'telugu': 'te',
    'kannada': 'kn', 'malayalam': 'ml', 'marathi': 'mr', 'gujarati': 'gu', 'punjabi': 'pa',
    'urdu': 'ur', 'nepali': 'ne', 'sinhala': 'si', 'arabic': 'ar', 'hebrew': 'he',
    'french': 'fr', 'spanish': 'es', 'german': 'de', 'italian': 'it', 'portuguese': 'pt',
    'dutch': 'nl', 'russian': 'ru', 'ukrainian': 'uk', 'polish': 'pl', 'czech': 'cs',
    'romanian': 'ro', 'hungarian': 'hu', 'greek': 'el', 'turkish': 'tr', 'swedish': 'sv',
    'danish': 'da', 'finnish': 'fi', 'norwegian': 'no', 'chinese': 'zh-Hans',
    'japanese': 'ja', 'korean': 'ko', 'vietnamese': 'vi', 'thai': 'th', 'indonesian': 'id',
    'malay': 'ms', 'swahili': 'sw', 'persian': 'fa', 'filipino': 'fil', 'burmese': 'my',
    'khmer': 'km', 'lao': 'lo', 'afrikaans': 'af', 'amharic': 'am', 'icelandic': 'is',
    'serbian': 'sr', 'croatian': 'hr', 'slovak': 'sk', 'slovenian': 'sl', 'bulgarian': 'bg',
    'estonian': 'et', 'latvian': 'lv', 'lithuanian': 'lt',
}


def source_numbers(text):
    """Preserve occurrence counts, signs and percentages across translation."""
    result = []
    for match in re.finditer(r'\d+(?:[.,]\d+)*(?:[^\S\n]*[%％])?', text):
        token = re.sub(r'\s', '', match[0]).replace('％', '%')
        position = match.start()
        if (position and text[position - 1] in '+-−'
                and (position < 2 or not text[position - 2].isalnum())):
            token = text[position - 1].replace('−', '-') + token
        result.append(token)
    return Counter(result)


def translation_languages(request):
    def language(value):
        value = value.strip().casefold()
        aliases = {code.casefold(): name for name, code in LANGUAGES.items()}
        value = aliases.get(value, value)
        if value not in LANGUAGES:
            raise BrainError('Enter a translation language such as Tamil, or a pair such as Tamil to English.')
        return value.title(), LANGUAGES[value]
    parts = re.split(r'\s+(?:to|->|→)\s+|\s*→\s*', request, flags=re.I)
    if len(parts) == 1:
        parts = ['English', parts[0]]
    if len(parts) != 2:
        raise BrainError('Use a target language or a pair such as English to Tamil.')
    return language(parts[0]), language(parts[1])


class TaskBrain(OllamaBrain):
    supports_task_models = True
    supports_grounded_documents = True

    def __init__(self, store, config=None, chat_override=None, **kwargs):
        self.config = config or ModelSettings.load(store)
        if chat_override:
            self.config = replace(self.config, chat=chat_override).validate()
        super().__init__(model=self.config.chat, **kwargs)
        self.store = store
        self._task = threading.local()
        self.last_label = 'Local · ' + self.config.chat
        self.nim = NimClient(CredentialVault(store.path), self.config.nim_model)

    def fork_documents(self):
        return TaskBrain(self.store, config=self.config, timeout=self.timeout, notice=self.notice)

    @contextmanager
    def task(self, name):
        previous = getattr(self._task, 'name', 'chat')
        self._task.name = name
        try:
            yield
        finally:
            self._task.name = previous

    def code_chat(self, question, context):
        with self.task('code'):
            return super().code_chat(question, context)

    def propose_edit(self, instruction, relative, source):
        with self.task('code'):
            return super().propose_edit(instruction, relative, source)

    def document_chat(self, question, context):
        with self.task('documents'):
            return super().document_chat(question, context)

    def document_matches(self, question, context):
        # Passage matching stays local even when online document answers are enabled.
        with self.task('retrieval'):
            return super().document_matches(question, context)

    def document_evidence(self, question, sources):
        with self.task('retrieval'):
            return super().document_evidence(question, sources)

    def document_answer(self, question, sources):
        with self.task('documents'):
            return super().document_answer(question, sources)

    def document_answer_supported(self, question, answer, sources):
        with self.task('documents'):
            return super().document_answer_supported(question, answer, sources)

    def label(self, text):
        self.last_label = text
        self.notice('AIModel', text)

    def _request_impl(self, messages, max_tokens, context_tokens, schema, structured, on_partial, cancel, temperature=.5):
        task = getattr(self._task, 'name', 'chat')
        model = getattr(self.config, 'documents' if task == 'retrieval' else task)
        nim_enabled = ((task == 'code' and self.config.nim_code) or
                       (task == 'documents' and self.config.nim_documents))
        if nim_enabled:
            self.label('Online · NIM · ' + self.config.nim_model)
            try:
                result = self.nim.request(messages, schema, max_tokens, cancel, temperature)
                if structured:
                    return result
                answer = clean_answer(result['reply'])
                if not answer:
                    raise BrainError('NIM returned an empty reply.')
                return answer
            except BrainCancelled:
                raise
            except (BrainError, KeyError, TypeError, ValueError) as exc:
                if cancel.is_set():
                    raise BrainCancelled('AI task cancelled.')
                self.notice('info', 'NIM could not complete this request. Trying the local model.')
        published = False
        def publish(text):
            nonlocal published
            published = published or bool(text)
            if on_partial is not None:
                on_partial(text)
        for candidate in dict.fromkeys((model, self.config.fallback) if task != 'translation' else (model,)):
            if cancel.is_set():
                raise BrainCancelled('AI task cancelled.')
            self.label('Local · ' + candidate + (' · translation' if task == 'translation' else ''))
            try:
                with local_slot(candidate, cancel):
                    return super()._request_impl(messages, max_tokens, context_tokens, schema, structured,
                        publish if on_partial is not None else None, cancel, temperature, model=candidate)
            except BrainCancelled:
                raise
            except BrainError:
                if published or candidate == self.config.fallback or task == 'translation':
                    raise
                self.notice('info', f'{candidate} did not complete the request. Trying {self.config.fallback} locally.')
        raise BrainError('No local model completed the request.')

    def translate_text(self, language_request, text):
        (source, source_code), (target, target_code) = translation_languages(language_request)
        if not text.strip() or len(text) > 1200:
            raise BrainError('Translate passages of up to 1,200 characters at a time.')
        if source_code == target_code:
            self.label('Local · source is already in ' + target)
            return text
        prompt = (f'You are a professional {source} ({source_code}) to {target} ({target_code}) translator. '
                  f'Your goal is to accurately convey the meaning and nuances of the original {source} text '
                  f'while adhering to {target} grammar, vocabulary, and cultural sensitivities.\n'
                  f'Produce only the {target} translation, without any additional explanations or commentary. '
                  f'Preserve names and keep all numbers as written. Please translate the following {source} text into {target}:\n\n\n{text}')
        with self.task('translation'):
            translated = self._request([{'role': 'user', 'content': prompt}], max_tokens=3072,
                                       context_tokens=8192, structured='text', temperature=0)
        scripts = {'ta': '\u0b80-\u0bff', 'hi': '\u0900-\u097f', 'te': '\u0c00-\u0c7f',
                   'kn': '\u0c80-\u0cff', 'ml': '\u0d00-\u0d7f', 'bn': '\u0980-\u09ff',
                   'si': '\u0d80-\u0dff', 'ar': '\u0600-\u06ff', 'ru': '\u0400-\u04ff',
                   'zh-Hans': '\u4e00-\u9fff', 'ja': '\u3040-\u30ff\u4e00-\u9fff', 'ko': '\uac00-\ud7af'}
        if (any(character.isalpha() for character in text) and target_code in scripts
                and not re.search('[' + scripts[target_code] + ']', translated)):
            raise BrainError('The translation was not in the requested language. Try a shorter passage.')
        if source_numbers(text) != source_numbers(translated):
            raise BrainError('The translation changed a number. Try a shorter passage and check the source.')
        return translated
