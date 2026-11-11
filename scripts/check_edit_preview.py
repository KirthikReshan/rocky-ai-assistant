"""Live Ollama edit-preview smoke test. Never applies changes to a source file."""
from pathlib import Path
import queue
import sys
import tempfile
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rocky.brain import OllamaBrain
from rocky.coding import Workspace
from rocky.editing import EditService
from rocky.editing_ui import show_edit_preview


def main():
    with tempfile.TemporaryDirectory(prefix='rocky-edit-demo-') as directory:
        folder = Path(directory).resolve()
        source = folder / 'sample.py'
        original = b'def add(a, b):\n    return a - b\n'
        source.write_bytes(original)
        workspace = Workspace()
        workspace.select(folder)
        editor = EditService(workspace, OllamaBrain(), folder / 'backups')
        proposal = editor.propose('sample.py', 'Correct add so it returns a + b. Keep the rest unchanged.')
        print(proposal.summary, flush=True)
        print(proposal.diff, flush=True)
        assert source.read_bytes() == original
        assert not (folder / 'backups').exists()
        root = tk.Tk()
        root.withdraw()
        popup = show_edit_preview(root, proposal, editor, queue.Queue())
        print('Edit preview window created:', popup.title(), flush=True)
        # This automated demo never presses Approve. Temporary window closes after 8 seconds.
        root.after(8000, root.destroy)
        root.mainloop()
        editor.discard(proposal.id)
        assert source.read_bytes() == original
        print('Live proposal/popup smoke test passed; demo source remained unchanged.', flush=True)


if __name__ == '__main__':
    main()
