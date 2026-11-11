"""Match saved audio devices across Windows device-index changes."""

PAUSE_CHOICES = (0.8, 1.2, 1.5, 2.0, 2.5)
VOCABULARY_LIMIT = 200


def device_key(device, hostapis):
    return {'name': device['name'], 'hostapi': hostapis[device['hostapi']]['name']}


def resolve_device(preference, devices, hostapis, direction):
    if not isinstance(preference, dict):
        return None
    matches = [index for index, device in enumerate(devices)
               if device[f'max_{direction}_channels'] > 0
               and device_key(device, hostapis) == preference]
    return matches[0] if len(matches) == 1 else None


def speech_pause(value):
    return value if type(value) in (int, float) and value in PAUSE_CHOICES else 1.5


def speech_vocabulary(value):
    if not isinstance(value, str) or len(value) > VOCABULARY_LIMIT:
        return ''
    return ', '.join(dict.fromkeys(' '.join(part.split()) for part in value.split(',') if part.strip()))
