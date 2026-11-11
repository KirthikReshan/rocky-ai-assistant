"""Download the public English model once; runtime stays offline."""
import tempfile
import urllib.request
import zipfile
from pathlib import Path

URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
TARGET = Path(__file__).resolve().parents[1] / "models"


def main():
    model = TARGET / "vosk-model-small-en-us-0.15"
    if model.exists():
        raise SystemExit(f"Already exists: {model}; refusing to overwrite.")
    TARGET.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TARGET) as staging:
        archive = Path(staging) / "model.zip"
        print("Downloading approximately 40 MB from", URL, flush=True)
        with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as output:
            import shutil
            shutil.copyfileobj(response, output)
        with zipfile.ZipFile(archive) as contents:
            for name in contents.namelist():
                resolved = (Path(staging) / name).resolve()
                if not resolved.is_relative_to(Path(staging).resolve()):
                    raise ValueError("Unsafe archive path")
            contents.extractall(staging)
        extracted = Path(staging) / model.name
        if not (extracted / "am/final.mdl").is_file():
            raise ValueError("Incomplete model archive")
        extracted.rename(model)
    print("Model ready:", model)


if __name__ == "__main__":
    main()
