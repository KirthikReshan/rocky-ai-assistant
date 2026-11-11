"""Hybrid passage ranking: semantic similarity plus exact words and identifiers."""
from collections import Counter
import math
import re

STOP = set('a an the is are was were be been to of for in on at and or what which when where how why '
           'does do did can could would should please tell me this that these those document file pdf'.split())


def terms(text):
    return [word for word in re.findall(r'[^\W_]+(?:[-./][^\W_]+)*(?:\+\+|#)?', text.casefold()) if word not in STOP]


def lexical_scores(query, passages):
    question = set(terms(query))
    rows = [Counter(terms(text)) for text in passages]
    average = sum(sum(row.values()) for row in rows) / max(1, len(rows)) or 1
    frequency = Counter(word for row in rows for word in question if word in row)
    scores = []
    for row in rows:
        length = sum(row.values())
        score = 0
        for word in question:
            count = row[word]
            if count:
                idf = math.log(1 + (len(rows) - frequency[word] + .5) / (frequency[word] + .5))
                score += idf * count * 2.2 / (count + 1.2 * (.25 + .75 * length / average))
        scores.append(score)
    return scores


def hybrid_order(query, chunks, dense_scores):
    lexical = lexical_scores(query, [hit.section + '\n' + hit.excerpt for hit in chunks])
    dense_order = sorted(range(len(chunks)), key=lambda index: -float(dense_scores[index]))
    word_order = sorted((index for index, score in enumerate(lexical) if score > 0), key=lambda index: -lexical[index])
    combined = [0.] * len(chunks)
    for order in (dense_order, word_order):
        for rank, index in enumerate(order):
            combined[index] += 1 / (40 + rank)
    return sorted(range(len(chunks)), key=lambda index: (-combined[index], index))
