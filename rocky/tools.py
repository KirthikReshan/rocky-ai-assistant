"""Deterministic desktop/web actions. Model text never enters this executor."""
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit
import json
import os
import re
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import webbrowser


@dataclass(frozen=True)
class Action:
    kind: str
    target: str


def parse_action(text):
    text = text.strip().rstrip(".!?")
    text = re.sub(r"^(?:(?:hey )?rocky[, ]+)?(?:(?:please|can you|could you|would you|will you)\s+)*",
                  "", text, flags=re.I)
    patterns = [
        (r"(?:find|search|look for) (?:on |in )?youtube(?: for)? (.+)", "youtube_search"),
        (r"(?:find|search for|look for) (.+?) (?:on|in) youtube", "youtube_search"),
        (r"youtube search(?: for)? (.+)", "youtube_search"),
        (r"(?:play song|play video|play) (.+?)(?: on youtube)?", "youtube_play"),
        (r"(?:google|search google for|search for|search) (.+?)(?: on google)?", "google"),
        (r"(?:open|launch|start|bring up|pull up) (.+?)(?: for me)?(?: please)?", "open"),
        (r"(?:close|terminate|kill|shut down|shutdown|restart|reboot|edit|delete|remove|change code)(?: (.*))?", "blocked"),
    ]
    for pattern, kind in patterns:
        match = re.fullmatch(pattern, text, re.I)
        if match:
            return Action(kind, (match.group(1) or "").strip())
    return None


def website_url(target):
    if any(ord(c) < 33 for c in target) or "\\" in target:
        raise ValueError("Invalid website address")
    value = target if "://" in target else "https://" + target
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Only HTTP and HTTPS website addresses are supported")
    if "." not in parts.hostname and parts.hostname != "localhost":
        raise ValueError("Please include a website address, such as example.com")
    # Accessing port validates malformed ports before opening the browser.
    _ = parts.port
    return urlunsplit(parts)


SITES = {"google": "https://www.google.com", "youtube": "https://www.youtube.com",
         "github": "https://github.com", "gmail": "https://mail.google.com"}
APPS = {"notepad": "notepad.exe", "calculator": "calc.exe", "paint": "mspaint.exe",
        "file explorer": "explorer.exe", "explorer": "explorer.exe",
        "chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe",
        "microsoft edge": "msedge.exe", "vs code": "Code.exe", "vscode": "Code.exe",
        "visual studio code": "Code.exe"}
FOLDERS = {"downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
           "documents": "Personal", "desktop": "Desktop", "pictures": "My Pictures",
           "music": "My Music", "videos": "My Video"}


def open_target(target, nicknames=None):
    """Normalize the same aliases for execution and offline command scoring."""
    target = target.strip()
    if len(target) >= 2 and target[0] == target[-1] == '"':
        target = target[1:-1].strip()
    aliases = {'my code editor': 'vs code', 'my editor': 'vs code'}
    if isinstance(nicknames, dict):
        aliases.update(nicknames)
    saved = aliases.get(target.casefold())
    if isinstance(saved, str):
        target = saved.strip()
    return normalize_app_target(target)


def folder_alias(target):
    name = re.sub(r'^(?:my |the )', '', target.strip().casefold())
    name = re.sub(r' folder$', '', name)
    return name if name in FOLDERS else None


def is_website_target(target):
    # Quoted drive paths and UNC folders can contain dots, just like domains.
    if Path(target).is_absolute():
        return False
    return target.casefold().startswith(('http://', 'https://')) or (
        '.' in target and not re.match(r'^[A-Za-z]:', target)
        and '\\' not in target and not any(char.isspace() for char in target))


def normalize_app_target(target):
    value = re.sub(r'^(?:the|my)\s+', '', target.strip().casefold())
    candidate = re.sub(r'\s+(?:app|application)$', '', value)
    # Strip conversational wrappers only for known names. An installed app or
    # a folder can legitimately contain "The" or "App" in its actual name.
    if candidate in APPS or candidate in SITES or candidate in {'browser', 'codex', 'openai codex'}:
        return candidate
    compact = {re.sub(r"[^a-z0-9]", '', phrase) for phrase in (value, candidate)}
    if compact & {'whatsapp', 'whatsapps', 'wahtapp', 'whatapp', 'whatapps',
                   'whatsup', 'whatup', 'whatsappdesktop', 'whatsappexe', 'whatsappapp'}:
        return 'WhatsApp'
    return target


def installed_app_id(name):
    """Look up exact display names and known aliases in the Windows Apps folder."""
    from pywintypes import error as WindowsError
    from win32com.client import Dispatch

    name = re.sub(r'\s+', ' ', normalize_app_target(name).strip().casefold())
    codex = name in {'codex', 'openai codex'}
    try:
        folder = Dispatch('Shell.Application').NameSpace('shell:AppsFolder')
        if folder is None:
            raise OSError('Windows could not list installed apps.')
        items = folder.Items()
        matches = set()
        for item in items:
            app_id = item.Path
            if codex and re.fullmatch(r'OpenAI\.Codex_[a-z0-9]+!App', app_id, re.I):
                return app_id
            if item.Name.casefold() == name:
                matches.add(app_id)
    except WindowsError as exc:
        raise OSError('Windows could not list installed apps.') from exc
    if len(matches) == 1:
        return matches.pop()
    if matches:
        raise ValueError(f'More than one app is named {name}. Open it from the Windows Start menu.')
    raise FileNotFoundError(f'Windows could not find an installed app named {name}. Try its Start menu name.')


def app_path(executable):
    """Use OS locations/registered installs, never the current folder or PATH."""
    import win32api
    name = executable.lower()
    if name not in {value.lower() for value in APPS.values()}:
        raise ValueError('That application is not supported')
    system_apps = {'notepad.exe', 'calc.exe', 'mspaint.exe', 'explorer.exe'}
    if name in system_apps:
        directory = win32api.GetWindowsDirectory() if name == 'explorer.exe' else win32api.GetSystemDirectory()
        path = Path(directory) / name
        if path.is_file():
            return str(path.resolve())
        raise FileNotFoundError(f'{executable} could not be found in its Windows installation directory')
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, f"Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{executable}",
                                    0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                    path = Path(os.path.expandvars(value.strip('"')))
                    if path.is_absolute() and path.name.lower() == name and path.is_file():
                        return str(path.resolve())
            except OSError:
                continue
    installs = {'code.exe': 'Programs/Microsoft VS Code/Code.exe',
                'chrome.exe': 'Google/Chrome/Application/chrome.exe',
                'msedge.exe': 'Microsoft/Edge/Application/msedge.exe'}
    for key in ('LOCALAPPDATA', 'ProgramFiles', 'ProgramFiles(x86)'):
        location = os.environ.get(key)
        if location and name in installs:
            relative = 'Microsoft VS Code/Code.exe' if name == 'code.exe' and key != 'LOCALAPPDATA' else installs[name]
            path = Path(location) / relative
            if path.is_absolute() and path.is_file():
                return str(path.resolve())
    raise FileNotFoundError(f"{executable} is not installed or could not be located")


def folder_path(target):
    name = folder_alias(target)
    if name is not None:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            value, _ = winreg.QueryValueEx(key, FOLDERS[name])
        path = Path(os.path.expandvars(value))
    else:
        raw = target.strip('"')
        if not Path(raw).is_absolute():
            raise ValueError("Use a folder name like Downloads or a full folder path")
        path = Path(raw)
    path = path.resolve()
    if not path.is_dir():
        raise FileNotFoundError("That folder does not exist")
    return path


class DesktopTools:
    def __init__(self, store=None):
        self.store = store

    def browser(self, url):
        if not webbrowser.open(url, new=2):
            raise OSError("Windows could not open the browser")

    def youtube_result(self, query):
        with TemporaryDirectory() as directory:
            result_file = Path(directory) / 'result.json'
            frozen = getattr(sys, 'frozen', False)
            if frozen:
                command = [sys.executable, '--youtube-worker', str(result_file), query]
            else:
                command = [sys.executable, '-m', 'rocky.youtube_search', query]
            process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                     timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                     cwd=Path(__file__).resolve().parents[1])
            if process.returncode:
                raise OSError('YouTube lookup failed. Check your internet connection and yt-dlp installation.')
            result = json.loads(result_file.read_text(encoding='utf-8') if frozen else process.stdout)
        if not isinstance(result, dict) or not isinstance(result.get('id'), str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", result['id']):
            raise ValueError("No playable YouTube result found")
        return result

    def execute(self, action):
        target = action.target
        if len(target) > 1000 or any(ord(c) < 32 for c in target):
            return "That command contains an invalid or overly long target."
        try:
            if action.kind == "blocked":
                return "I haven't changed anything. Supported PC actions and code edits need confirmation in their dedicated previews. Try close and an app name, or request a code edit preview. Forced termination and file deletion are not supported."
            if action.kind == "google":
                self.browser("https://www.google.com/search?q=" + quote_plus(target))
                return f"Opening Google results for {target}."
            if action.kind == "youtube_play":
                try:
                    result = self.youtube_result(target)
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    self.browser("https://www.youtube.com/results?search_query=" + quote_plus(target))
                    return f"I couldn't pick a video automatically. Opening YouTube results for {target}."
                else:
                    self.browser(f"https://www.youtube.com/watch?v={result['id']}&autoplay=1")
                    return f"Opening {result.get('title', target)} on YouTube."
            if action.kind == "youtube_search":
                self.browser("https://www.youtube.com/results?search_query=" + quote_plus(target))
                return f"Opening YouTube results for {target}."
            if action.kind == "open":
                nicknames = None
                if self.store is not None:
                    nicknames = self.store.setting('app_nicknames', {})
                target = open_target(target, nicknames)
                if not target or len(target) > 1000 or any(ord(c) < 32 for c in target):
                    raise ValueError('Use an app name, website address, or full folder path.')
                name = target.lower()
                if name in SITES:
                    self.browser(SITES[name])
                elif name == "browser":
                    self.browser("https://www.google.com")
                elif name in APPS:
                    subprocess.Popen([app_path(APPS[name])], shell=False)
                elif is_website_target(target):
                    self.browser(website_url(target))
                else:
                    try:
                        path = str(folder_path(target))
                    except ValueError:
                        path = 'shell:AppsFolder\\' + installed_app_id(target)
                    subprocess.Popen([app_path("explorer.exe"), path], shell=False)
                return f"Opening {target}."
            return "That action is not supported yet."
        except sqlite3.Error:
            return "I couldn't read your saved app settings. Please try again."
        except (OSError, ValueError, KeyError) as exc:
            return f"I couldn't complete that: {exc}"
