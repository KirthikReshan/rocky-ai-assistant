"""Human approval UI: the only application entry point that applies source edits."""
import tkinter as tk
from tkinter import ttk
from rocky.text_view import TextView

from rocky.editing import digest


def show_edit_preview(root, proposal, editor, events):
    popup = tk.Toplevel(root)
    popup.title(f'Rocky edit preview — {proposal.relative}')
    popup.geometry('900x650')
    ttk.Label(popup, text=f'Proposed change: {proposal.identity[0] / proposal.relative}',
              wraplength=850).pack(anchor='w', padx=12, pady=8)
    ttk.Label(popup, text=proposal.summary, wraplength=850).pack(anchor='w', padx=12)
    ttk.Label(popup, text=f'Preview ID: {proposal.id}\nOriginal SHA-256: {digest(proposal.original)}',
              wraplength=850).pack(anchor='w', padx=12, pady=8)
    diff_view = TextView(popup, wrap='none', font=('Consolas', 10))
    diff = diff_view.text
    diff.insert('1.0', proposal.diff)
    diff.configure(state='disabled')
    diff_view.pack(fill='both', expand=True, padx=12)
    status = tk.StringVar(value='No source file has changed. Review this diff before approving. Preview expires in 10 minutes.')
    ttk.Label(popup, textvariable=status, wraplength=850).pack(fill='x', padx=12, pady=8)
    controls = ttk.Frame(popup)
    controls.pack(fill='x', padx=12, pady=10)

    def reject():
        editor.discard(proposal.id)
        popup.destroy()

    def approve():
        apply_button.configure(state='disabled')
        try:
            backup = editor.apply(proposal.id)
        except (ValueError, OSError) as exc:
            message = f'Edit could not complete: {exc}'
            status.set(message)
            events.put(('error', message))
        else:
            message = f'Applied approved edit to {proposal.relative}. Original backup: {backup}'
            status.set(message)
            events.put(('info', message))
        reject_button.configure(text='Close', command=popup.destroy)

    apply_button = ttk.Button(controls, text='Approve and apply', command=approve)
    apply_button.pack(side='right')
    reject_button = ttk.Button(controls, text='Reject', command=reject)
    reject_button.pack(side='right', padx=8)
    popup.protocol('WM_DELETE_WINDOW', reject)
    return popup
