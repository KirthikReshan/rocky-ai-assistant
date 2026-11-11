"""Conservative recommendations from the user's measured microphone samples."""
from collections import defaultdict
from rocky.voice_benchmark import words


def advise(rows, device=None, current_pause=1.5):
    rows = [dict(row) for row in rows]
    device = device or (rows[0]['device'] if rows else None)
    rows = [row for row in rows if row['device'] == device]
    changes, lines = {}, [f'Microphone: {device or "No recordings yet"}']
    clean = [r for r in rows if not r['overflow'] and (r.get('clipped') is None or r['clipped'] <= .01)
             and (r.get('peak') is None or r['peak'] >= .02)]
    commands = [r for r in clean if r['test_kind'] == 'command' and r['reference_words']]
    lines.append(f'{len(commands)} usable transcript samples. Aim for at least five distinct phrases in each room condition.')
    if len({r['expected'].casefold() for r in commands}) >= 5:
        accuracy = max(0, 1 - sum(r['word_errors'] for r in commands) / sum(r['reference_words'] for r in commands))
        lines.append(f'Measured word accuracy: {accuracy:.0%}. Target: at least 80%.')
        if current_pause < 2 and sum(len(words(r['heard'])) < len(words(r['expected'])) for r in commands) >= 3:
            changes['speech_pause'] = 2.0
            lines.append('Several sentences lost words. Try a 2.0 second speech pause, then repeat the same samples.')
    for room in sorted({r['environment'] for r in commands}):
        sample = [r for r in commands if r['environment'] == room]
        room_score = max(0,1-sum(r['word_errors'] for r in sample)/sum(r['reference_words'] for r in sample))
        lines.append(f'{room}: {room_score:.0%} word accuracy from {len(sample)} samples.')
    if len(clean) != len(rows):
        lines.append('Interrupted, clipped or very quiet recordings were excluded. Check microphone gain/input and repeat those samples.')
    grouped = defaultdict(list)
    for r in commands:
        grouped[(r['expected'].casefold(), r['environment'])].append(r)
    matched = [group for group in grouped.values() if {'CPU', 'NVIDIA GPU'} <= {r['engine'] for r in group}]
    if len(matched) >= 3:
        comparison = {}
        for engine in ('CPU', 'NVIDIA GPU'):
            sample = [next(r for r in group if r['engine'] == engine) for group in matched]
            comparison[engine] = (sum(r['word_errors'] for r in sample) / sum(r['reference_words'] for r in sample),
                                  sum(r['latency'] for r in sample) / len(sample))
        fastest = min(comparison, key=lambda name: comparison[name][1])
        if comparison[fastest][0] <= .2 and comparison[fastest][0] <= min(v[0] for v in comparison.values()) + .02:
            changes['recognition_device'] = 'cuda' if fastest == 'NVIDIA GPU' else 'cpu'
            lines.append(f'{fastest} was faster on {len(matched)} matched phrases with comparable accuracy. Processor changes apply after restart.')
    else:
        lines.append('For processor advice, record at least three identical phrases on CPU and GPU in the same room condition.')
    configs = defaultdict(list)
    for r in clean:
        if r['test_kind'].startswith('wake_'):
            configs[r['wake_config']].append(r)
    for config, sample in configs.items():
        positive = [r for r in sample if r['test_kind'] == 'wake_positive']
        negative = [r for r in sample if r['test_kind'] == 'wake_negative']
        if len(positive) >= 4 and len(negative) >= 4:
            lines.append(f'{config}: wake detected {sum(r["wake_detected"] or 0 for r in positive)}/{len(positive)}; '
                         f'false activations {sum(r["wake_detected"] or 0 for r in negative)}/{len(negative)}.')
            if config == next(iter(configs)):
                false = sum(r['wake_detected'] or 0 for r in negative)
                detected = sum(r['wake_detected'] or 0 for r in positive)/len(positive)
                if false:
                    changes['wake_sensitivity'] = 'Strict'
                    lines.append('False activations occurred. Try Strict sensitivity; if it is already Strict, compare Whisper verification.')
                elif detected < .8 and 'threshold 0.35' not in config:
                    changes['wake_sensitivity'] = 'Relaxed'
                    lines.append('Wake words were missed with no false activation in these samples. Try Relaxed sensitivity and repeat both wake tests.')
    lines.append('Test wake detection with four Hey Rocky recordings and four unrelated sentences per setting. '
                 'Keep the headset close, avoid clipping, and compare quiet and noisy conditions separately. '
                 'Advice is based on these samples; repeat the test after applying it.')
    return '\n\n'.join(lines), changes
