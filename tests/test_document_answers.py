import io, json, unittest
from unittest.mock import Mock
from rocky.brain import OllamaBrain, BrainError

class DocumentAnswerTests(unittest.TestCase):
    def test_untranslated_text_and_model_control_tokens_are_rejected(self):
        for translation in ('The deadline is Friday.', 'The deadline is Friday. /no_think'):
            brain = OllamaBrain()
            brain.http.open = Mock(return_value=io.BytesIO(json.dumps({'message': {'content': json.dumps({
                'translation': translation})}}).encode()))
            with self.assertRaisesRegex(BrainError, 'usable translation'):
                brain.document_chat('Translate all supplied source text faithfully into Tamil.', '[S1] The deadline is Friday.')

    def test_translation_preserves_clickable_source_reference(self):
        brain = OllamaBrain()
        brain.http.open = Mock(return_value=io.BytesIO(json.dumps({'message': {'content': json.dumps({
            'translation': 'La fecha es el viernes.'})}}).encode()))
        answer = brain.document_chat('Translate all supplied source text faithfully into Spanish.', '[S1] Deadline Friday')
        self.assertIn('La fecha', answer)
        self.assertIn('[S1]', answer)

    def test_uncited_answer_retains_validated_source_list(self):
        brain=OllamaBrain()
        brain.http.open=Mock(return_value=io.BytesIO(json.dumps({'message':{'content':json.dumps({'answer':'Due Friday','sources':['S1']})}}).encode()))
        self.assertIn('[S1]',brain.document_chat('When is it due?','[S1] Deadline Friday'))

    def test_planning_and_unknown_citations_are_rejected(self):
        for answer, sources in [('I need to answer this', ['S1']), ('Invented', ['S99'])]:
            brain=OllamaBrain()
            brain.http.open=Mock(side_effect=lambda *a, **k:io.BytesIO(json.dumps({'message':{'content':json.dumps({'answer':answer,'sources':sources})}}).encode()))
            with self.assertRaises(BrainError):
                brain.document_chat('Question','[S1] Source')

    def test_quiz_has_requested_question_count_and_separate_answers(self):
        brain=OllamaBrain()
        data={'cards':[{'question':'When is delivery?', 'answer':'Friday', 'source':'S1'}, {'question':'What is the budget?', 'answer':'500', 'source':'S1'}]}
        brain.http.open=Mock(return_value=io.BytesIO(json.dumps({'message':{'content':json.dumps(data)}}).encode()))
        answer=brain.document_chat('Create practice questions. Create two questions','[S1] Delivery Friday. Budget 500.')
        self.assertIn('Answer key',answer)
        self.assertIn('2. What is the budget?',answer)
        self.assertIn('500 [S1]',answer)
        request=json.loads(brain.http.open.call_args.args[0].data)
        self.assertEqual(request['format']['properties']['cards']['minItems'],2)
