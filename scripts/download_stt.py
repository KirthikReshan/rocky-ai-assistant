"""One-time download; Rocky never downloads models during normal startup."""
from pathlib import Path
from huggingface_hub import snapshot_download

if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "models/whisper-small.en"
    snapshot_download("Systran/faster-whisper-small.en", local_dir=str(target),
                      allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"])
    print("Local transcription model ready:", target)
