"""Answer only from verified source quotations, with a second support check."""
from dataclasses import replace
import json
import re

from rocky.brain import BrainError, clean_answer

NOT_FOUND = "I couldn't find a supported answer in these document passages. Try a more specific question or check the source pages."


def quote_match(source, quote):
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 1800:
        return None
    if len(quote.split()) < 2 and quote.strip() != source.strip():
        return None
    return re.search(r'\s+'.join(re.escape(word) for word in quote.split()), source)


def contextual_quote(source, match):
    # A model quoting "allowed for employees" must not drop the source's "not"
    # or an immediately following exception. Keep the enclosing source sentence.
    boundaries = list(re.finditer(r'[.!?](?:\s+|$)|\n[ \t]*\n', source[:match.start()]))
    start = boundaries[-1].end() if boundaries else 0
    if re.match(r'\s*(?:except|unless|however|but|only|excluding)\b', source[start:], re.I):
        start = boundaries[-2].end() if len(boundaries) > 1 else 0
    end = match.end()
    if end and source[end-1] not in '.!?':
        following = re.search(r'[.!?](?:\s+|$)|\n[ \t]*\n', source[end:])
        end = end + following.end() if following else len(source)
    remaining = source[end:]
    if re.match(r'\s*(?:except|unless|however|but|only|excluding|this excludes)\b', remaining, re.I):
        following = re.search(r'[.!?](?:\s+|$)|\n[ \t]*\n', remaining)
        end += following.end() if following else len(remaining)
    return source[start:end].strip()


def source_groups(hits, limit=6000):
    group, length = [], 0
    for number, hit in enumerate(hits, 1):
        row = {'source': f'S{number}', 'location': hit.label, 'section': hit.section, 'text': hit.excerpt}
        size = len(json.dumps(row, ensure_ascii=False))
        if group and length + size > limit:
            yield group
            group, length = [], 0
        group.append(row)
        length += size
    if group:
        yield group


def clean_cited_answer(answer, evidence):
    from rocky.task_brain import source_numbers
    answer = clean_answer(answer)
    cited = set(re.findall(r'\[S(\d+)\]', answer))
    if not answer or not cited or any(int(number) < 1 or int(number) > len(evidence) for number in cited):
        raise ValueError('Invalid answer citations')
    if re.match(r'(?:I need to|Let me|The user wants|First, I)', answer, re.I):
        raise ValueError('No final answer')
    # Check that an answer did not invent a number absent from its quoted evidence.
    plain = re.sub(r'\[S\d+\]', '', answer)
    plain = re.sub(r'(?m)^\s*\d+[.)]\s+', '', plain)
    supported = source_numbers('\n'.join(evidence[int(number)-1].label + '\n' + evidence[int(number)-1].section + '\n' + evidence[int(number)-1].excerpt for number in cited))
    if set(source_numbers(plain)) - set(supported):
        raise ValueError('An answer number is absent from the cited evidence')
    return answer


class GroundedQA:
    def __init__(self, brain):
        self.brain = brain

    def answer(self, question, candidates, check, progress):
        if not question.strip() or len(question) > 500:
            raise ValueError('Enter a document question of 1–500 characters.')
        evidence, seen = [], set()
        groups = list(source_groups(candidates))
        for index, group in enumerate(groups, 1):
            check()
            progress(f'Checking source evidence {index} of {len(groups)}…')
            matches = self.brain.document_evidence(question, group)
            check()
            if not isinstance(matches, list):
                raise BrainError('The model returned an invalid evidence list. Retry the question.')
            if len(matches) >= 24:
                # A saturated extraction could omit matching items. Retain the
                # complete source group instead of presenting a truncated list.
                matches = [{'source': row['source'], 'quote': row['text']} for row in group]
            allowed = {row['source'] for row in group}
            for item in matches:
                if not isinstance(item, dict) or not isinstance(item.get('source'), str):
                    continue
                reference = item['source'].strip('[]')
                if reference not in allowed:
                    continue
                number = int(reference[1:]) - 1
                match = quote_match(candidates[number].excerpt, item.get('quote'))
                if match is None:
                    continue
                hit = replace(candidates[number], excerpt=contextual_quote(candidates[number].excerpt, match))
                key = (hit.source, hit.label, ' '.join(hit.excerpt.split()))
                if key not in seen:
                    evidence.append(hit)
                    seen.add(key)
        if not evidence:
            return NOT_FOUND, candidates, 'No answer was accepted without verified source evidence.'
        # Large enumerations remain complete: show all verified quotations instead
        # of silently truncating a long evidence set to the model's context limit.
        fallback = ('Relevant source passages:\n\n' + '\n\n'.join(
            f'{hit.excerpt} [S{number}]' for number, hit in enumerate(evidence, 1)))
        if sum(len(hit.excerpt) for hit in evidence) > 6500:
            return fallback, evidence, 'Showing all verified evidence; it exceeds the concise-answer context limit.'
        sources = [{'source': f'S{number}', 'location': hit.label, 'section': hit.section, 'text': hit.excerpt}
                   for number, hit in enumerate(evidence, 1)]
        for attempt in range(2):
            check()
            progress('Writing an answer from the verified passages…' if not attempt else 'Rechecking the answer against its sources…')
            request = question if not attempt else ('Use literal statements from the quotations. If the answer needs '
                'unsupported inference, set found to false. Question: ' + question)
            result = self.brain.document_answer(request, sources)
            check()
            try:
                if not isinstance(result, dict) or result.get('found') is not True:
                    break
                answer = clean_cited_answer(result['answer'], evidence)
                from rocky.documents import needs_full_document
                if needs_full_document(question):
                    cited = set(re.findall(r'\[S(\d+)\]', answer))
                    if cited != {str(number) for number in range(1, len(evidence)+1)}:
                        raise ValueError('A whole-document answer omitted supplied evidence')
            except (ValueError, TypeError, KeyError):
                continue
            progress('Verifying that the answer is supported by the quoted evidence…')
            supported = self.brain.document_answer_supported(question, answer, sources)
            check()
            if supported is True:
                quotes = '\n\nEvidence:\n' + '\n'.join(f'[S{number}] “{hit.excerpt}”'
                    for number, hit in enumerate(evidence, 1) if f'[S{number}]' in answer)
                return answer + quotes, evidence, 'Cited quotations were matched to the document and the answer passed an additional model support check.'
        return fallback, evidence, 'A concise answer could not be verified. Showing the original evidence instead.'
