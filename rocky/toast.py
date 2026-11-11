"""Small, non-activating Windows message cards, shared by Rocky and Calendar."""
from collections import deque
import tkinter as tk

TOAST_DURATION_MS = 4000


def toast_position(work_area, width, height, margin=16):
    left, top, right, bottom = work_area
    return max(left, right-width-margin), max(top, bottom-height-margin)


def show_without_activation(popup, root, width, height):
    import win32api
    import win32con
    import win32gui
    owner = win32gui.GetAncestor(root.winfo_id(), 2)
    work = win32api.GetMonitorInfo(win32api.MonitorFromWindow(owner, 2))['Work']
    x,y = toast_position(work,width,height)
    def styles():
        hwnd = win32gui.GetAncestor(popup.winfo_id(), 2)
        flags = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        flags = (flags | 0x08000000 | win32con.WS_EX_TOOLWINDOW) & ~win32con.WS_EX_APPWINDOW
        win32gui.SetWindowLong(hwnd,win32con.GWL_EXSTYLE,flags)
        return hwnd
    styles()  # Set NOACTIVATE before mapping, so showing the card cannot take focus.
    popup.deiconify()
    hwnd = styles()
    win32gui.SetWindowPos(hwnd,win32con.HWND_TOPMOST,x,y,width,height,
                         win32con.SWP_NOACTIVATE | win32con.SWP_NOOWNERZORDER | win32con.SWP_SHOWWINDOW)


def show_toast(root, message, *, title='Rocky', actions=(), duration_ms=TOAST_DURATION_MS, error=False):
    popup = tk.Toplevel(root)
    popup.withdraw()
    popup.title(title)
    popup.overrideredirect(True)
    popup.attributes('-toolwindow',True)
    popup.attributes('-topmost',True)
    background, foreground = '#191929', '#f4f2fc'
    accent = '#ffa7b2' if error else '#72eac8'
    popup.configure(background=accent, padx=1, pady=1)
    card = tk.Frame(popup,background=background,padx=16,pady=12)
    card.pack(fill='both',expand=True)
    heading = tk.Frame(card,background=background)
    heading.pack(fill='x')
    timer = None
    def dismiss():
        if popup.winfo_exists():
            popup.destroy()
    tk.Button(heading,text='×',command=dismiss,borderwidth=0,background=background,
              foreground=foreground,activebackground='#303046',activeforeground=foreground,
              font=('Segoe UI',13),takefocus=False).pack(side='right')
    tk.Label(heading,text=title[:70],background=background,foreground=accent,
             font=('Segoe UI',11,'bold'),anchor='w').pack(side='left',fill='x',expand=True)
    message = ' '.join(str(message).split())
    visible = message[:280] + ('…' if len(message)>280 else '')
    tk.Label(card,text=visible,background=background,foreground=foreground,wraplength=380,
             font=('Segoe UI',11),anchor='w',justify='left').pack(fill='x',pady=(5,8))
    if actions:
        controls = tk.Frame(card,background=background)
        controls.pack(fill='x')
        for label, callback in actions:
            def invoke(action=callback):
                if action():
                    dismiss()
            tk.Button(controls,text=label,command=invoke,background='#303046',foreground=foreground,
                      activebackground='#4c3676',activeforeground=foreground,borderwidth=0,
                      padx=9,pady=5,takefocus=False,font=('Segoe UI',9)).pack(side='left',padx=(0,6))
    popup.update_idletasks()
    width = 420
    height = popup.winfo_reqheight()
    popup.geometry(f'{width}x{height}')
    try:
        show_without_activation(popup,root,width,height)
    except Exception:
        popup.destroy()
        raise
    def destroyed(event):
        if event.widget is popup and timer is not None:
            popup.after_cancel(timer)
    popup.bind('<Destroy>',destroyed)
    timer = popup.after(duration_ms,dismiss)
    return popup


class ToastQueue:
    """Queue ordinary status cards. Durable reminders are shown only when idle."""
    def __init__(self,root):
        self.root = root
        self.pending = deque(maxlen=8)
        self.current = None
        self.current_key = None
        self.timer = root.after(100,self.poll)
        self.closed = False

    def idle(self):
        return not self.pending and (self.current is None or not self.current.winfo_exists())

    def post(self,message,*,title='Rocky',error=False):
        item = (str(message),title,error)
        if self.closed or item in self.pending:
            return
        if self.current is not None and self.current.winfo_exists() and item == self.current_key:
            return
        self.pending.append(item)

    def show_now(self,message,**options):
        if not self.idle():
            raise RuntimeError('A notification is already visible.')
        self.current = show_toast(self.root,message,**options)
        self.current_key = None
        return self.current

    def poll(self):
        if self.closed:
            return
        if (self.current is None or not self.current.winfo_exists()) and self.pending:
            item = self.pending.popleft()
            try:
                self.current = show_toast(self.root,item[0],title=item[1],error=item[2])
                self.current_key = item
            except Exception:
                # The owning application already keeps the full message in its log.
                self.current = None
        self.timer = self.root.after(100,self.poll)

    def close(self):
        self.closed = True
        self.root.after_cancel(self.timer)
        self.pending.clear()
        if self.current is not None and self.current.winfo_exists():
            self.current.destroy()
