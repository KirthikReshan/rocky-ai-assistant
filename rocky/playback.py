"""Prepare fully generated speech and play through a buffered output stream."""
def shorten_long_silences(samples, rate, max_pause=.45):
    import numpy as np
    window = max(1, round(rate * .01))
    blocks = len(samples) // window
    if not blocks:
        return samples
    peaks = np.max(np.abs(samples[:blocks * window].astype(np.int32).reshape(-1, window)), axis=1)
    quiet = peaks <= 40
    cuts = []
    start = None
    for i in range(blocks + 1):
        if i < blocks and quiet[i]:
            if start is None:
                start = i
        elif start is not None:
            # Short pauses stay intact; only cap unusually long near-silent spans.
            if (i - start) * window > rate * .7:
                keep = round(max_pause * rate / window)
                cuts.append(((start + keep) * window, i * window))
            start = None
    if not cuts:
        return samples
    chunks = []
    position = 0
    for left, right in cuts:
        chunks.append(samples[position:left])
        position = right
    chunks.append(samples[position:])
    return np.concatenate(chunks)


def play_pcm(pcm, sample_rate, report, device=None, cancelled=None):
    import numpy as np
    import sounddevice as sd
    if cancelled and cancelled():
        return
    samples = np.frombuffer(pcm, dtype=np.int16)
    samples = shorten_long_silences(samples, sample_rate)
    output = sd.query_devices(device, "output")
    output_rate = round(output['default_samplerate'])
    if output_rate != sample_rate and len(samples):
        # Use the device's native rate, particularly for Bluetooth output.
        count = round(len(samples) * output_rate / sample_rate)
        samples = np.interp(np.arange(count) * sample_rate / output_rate,
                            np.arange(len(samples)), samples).astype(np.int16)
    # Pre-roll gives a newly opened output device a moment to settle.
    samples = np.concatenate((np.zeros(round(output_rate * .15), dtype=np.int16), samples))
    if cancelled and cancelled():
        return
    with sd.RawOutputStream(samplerate=output_rate, channels=1, dtype="int16",
                            device=device, blocksize=0, latency="high") as stream:
        underflowed = False
        chunk_frames = max(1, round(output_rate * .1))
        for offset in range(0, len(samples), chunk_frames):
            if cancelled and cancelled():
                stream.abort()
                return
            underflowed |= bool(stream.write(samples[offset:offset + chunk_frames].tobytes()))
        # A stop can arrive during the final blocking write. Abort that buffered
        # tail instead of allowing context-manager shutdown to drain it aloud.
        if cancelled and cancelled():
            stream.abort()
            return
        if underflowed:
            report("info", "Speaker buffer underrun detected. Try another speaker output.")
