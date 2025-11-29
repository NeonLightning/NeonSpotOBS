#!/usr/bin/env python3
import base64, json, os, queue, threading, time, urllib.parse, requests, logging, sys, warnings, tkinter as tk, pystray, shutil, webbrowser
from datetime import datetime, timedelta, timezone
from flask import Flask, redirect, render_template_string, request
from tkinter import ttk, colorchooser, messagebox, filedialog
from werkzeug.serving import make_server
from PIL import Image, ImageDraw, ImageTk

warnings.filterwarnings('ignore', category=UserWarning, module='PIL')

TOKENS_FILE = "spotify_tokens.json"
CLIENT_FILE = "spotify_client.json"
CSS_FILE = "settings.css"
REDIRECT_URI = "http://127.0.0.1:5000/callback"
SCOPE = "user-read-currently-playing user-read-playback-state"
POLL_INTERVAL = 2
FADE_AFTER_SECONDS = 10

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app.logger.disabled = True
logging.getLogger('werkzeug').disabled = True
current_track_data = None
data_lock = threading.Lock()
auth_code_q = queue.Queue()
last_track_time = None
gui = None

def create_default_css():
    default_css = """:root {
    --bg-color: #00ff00;
    --bg-image: none;

    --text-primary: #eee;
    --text-secondary: #cfcfcf;
    --text-tertiary: #9a9a9a;

    --progress-bg: rgba(255, 0, 0);
    --progress-start: rgba(94, 255, 155);
    --progress-end: rgba(0, 176, 255);

    --card-bg: rgba(30, 30, 30);
    --card-shadow: 0 8px 32px rgba(0, 0, 0);

    --fade-wait: 10s;
    --fade-duration: 2s;
    --fade-ease: ease-out;

    --card-display: flex;
}
"""
    if not os.path.exists(CSS_FILE):
        with open(CSS_FILE, "w") as f:
            f.write(default_css)

def load_css():
    if not os.path.exists(CSS_FILE):
        create_default_css()
    with open(CSS_FILE, "r") as f:
        return f.read()

def save_css(css_content):
    with open(CSS_FILE, "w") as f:
        f.write(css_content)

def save_tokens(data: dict):
    with open(TOKENS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_tokens() -> dict:
    if not os.path.exists(TOKENS_FILE):
        return {}
    with open(TOKENS_FILE, "r") as f:
        return json.load(f)

def save_client_credentials(client_id: str, client_secret: str):
    with open(CLIENT_FILE, "w") as f:
        json.dump({"client_id": client_id, "client_secret": client_secret}, f, indent=2)

def load_client_credentials() -> tuple:
    if not os.path.exists(CLIENT_FILE):
        return None, None
    with open(CLIENT_FILE, "r") as f:
        data = json.load(f)
        return data.get("client_id"), data.get("client_secret")

def build_auth_url(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "show_dialog": "true",
    }
    return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)

def exchange_code_for_token(client_id: str, client_secret: str, code: str) -> dict:
    url = "https://accounts.spotify.com/api/token"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}
    r = requests.post(url, data=payload, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()

def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    url = "https://accounts.spotify.com/api/token"
    payload = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}
    r = requests.post(url, data=payload, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()

def get_current_playback(access_token: str):
    url = "https://api.spotify.com/v1/me/player/currently-playing"
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(url, headers=headers, timeout=5)
    if r.status_code == 204:
        return None
    if r.status_code == 429:
        time.sleep(2)
        return None
    r.raise_for_status()
    return r.json()

def token_manager_loop(client_id, client_secret, tokens_container: dict, stop_event: threading.Event):
    refresh_token = tokens_container.get("refresh_token")
    if not refresh_token:
        return
    while not stop_event.is_set():
        access_token = tokens_container.get("access_token")
        expires_at = tokens_container.get("expires_at")
        needs_refresh = True
        if access_token and expires_at:
            exp_dt = datetime.fromisoformat(expires_at)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < exp_dt - timedelta(seconds=30):
                needs_refresh = False
        if needs_refresh:
            try:
                resp = refresh_access_token(client_id, client_secret, refresh_token)
                tokens_container["access_token"] = resp["access_token"]
                if "refresh_token" in resp:
                    tokens_container["refresh_token"] = resp["refresh_token"]
                    refresh_token = resp["refresh_token"]
                expires_in = resp.get("expires_in", 3600)
                tokens_container["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
                save_tokens(tokens_container)
            except Exception as e:
                print("Token refresh error:", e)
        time.sleep(10)

def playback_poll_loop(tokens_container: dict, stop_event: threading.Event):
    global last_track_time
    last_update = 0
    while not stop_event.is_set():
        access_token = tokens_container.get("access_token")
        if not access_token:
            time.sleep(1)
            continue
        current_time = time.time()
        if current_time - last_update >= POLL_INTERVAL:
            try:
                data = get_current_playback(access_token)
                with data_lock:
                    global current_track_data
                    current_track_data = data
                    if data and data.get('item'):
                        last_track_time = time.time()
                last_update = current_time
            except Exception:
                pass
        time.sleep(0.1)

def format_track_display(data):
    if not data or not data.get('item'):
        return ''
    item = data['item']
    title = item.get('name', '')
    artists = ', '.join(artist['name'] for artist in item.get('artists', []))
    album = item.get('album', {}).get('name', '')
    images = item.get('album', {}).get('images', [])
    img_url = images[0]['url'] if images else ''
    progress = data.get('progress_ms', 0)
    duration = item.get('duration_ms', 1)
    pct = min(100, (progress / duration) * 100)
    def format_time(ms):
        seconds = int(ms / 1000)
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins}:{secs:02d}"
    progress_time = format_time(progress)
    duration_time = format_time(duration)
    return f'''
    <div class="meta">
        <img class="art" src="{img_url}" alt="album art"/>
        <div class="text">
            <div class="title">{title}</div>
            <div class="artist">{artists}</div>
            <div class="album">Album: {album}</div>
            <div class="progress-container">
                <div class="progress"><div class="bar" style="width:{pct}%"></div></div>
                <div class="time-display">{progress_time} / {duration_time}</div>
            </div>
        </div>
    </div>
    '''

INDEX_HTML = """
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>Spotify Now Playing</title>
    <style>
        {{ css_content }}
        body {
            font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
            padding: 0;
            background: var(--bg-color);
            color: var(--text-primary);
            margin: 0;
            min-height: 100vh;
        }
        .card {
            padding: 24px;
            width: 100%;
            box-sizing: border-box;
            position: relative;
            background: var(--card-bg);
            border-radius: 12px;
            box-shadow: var(--card-shadow);
            backdrop-filter: blur(10px);
            transition: opacity var(--fade-duration) var(--fade-ease, ease-out);
        }
        .card.has-bg-image {
            background-image: url('/background_image.jpg');
            background-size: cover;
            background-position: center;
            background-repeat: no-repeat;
        }
        .card.fade-out {
            opacity: 0;
        }
        .meta {
            display: flex;
            gap: 14px;
            align-items: flex-start;
        }
        img.art {
            width: 140px;
            height: 140px;
            object-fit: cover;
            border-radius: 6px;
        }
        .text {
            flex: 1;
        }
        .title {
            font-size: 1.05rem;
            font-weight: 600;
            margin-bottom: 6px;
            color: var(--text-primary);
        }
        .artist {
            color: var(--text-secondary);
            margin-bottom: 6px;
        }
        .album {
            color: var(--text-tertiary);
            font-size: 0.9rem;
        }
        .progress {
            width: 100%;
            height: 10px;
            background: var(--progress-bg);
            border-radius: 999px;
            overflow: hidden;
        }
        .bar {
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg, var(--progress-start), var(--progress-end));
            transition: width 0.3s linear;
        }
        .none {
            text-align: center;
            padding: 36px 12px;
            color: var(--text-tertiary);
        }
        .progress-container {
            margin-top: 12px;
        }
        .progress {
            width: 100%;
            height: 10px;
            background: var(--progress-bg);
            border-radius: 999px;
            overflow: hidden;
            margin-bottom: 6px;
        }
        .time-display {
            font-size: 0.85rem;
            color: var(--text-tertiary);
            text-align: right;
        }
    </style>
</head>
<body>
    <div class="card" id="card">
        <div id="content">{{ track_html|safe }}</div>
    </div>
<script>
(function() {
    const cardDisplay = getComputedStyle(document.documentElement).getPropertyValue('--card-display').trim();
    const card = document.querySelector('.card');
    if (cardDisplay === 'none') {
        card.style.background = 'transparent';
        card.style.borderRadius = '0';
        card.style.boxShadow = 'none';
        card.style.backdropFilter = 'none';
    }
})();
let isUpdating = false;
let lastTrackTime = null;
let fadeTimeout = null;
function getFadeDelay() {
    const rootStyles = getComputedStyle(document.documentElement);
    return parseFloat(rootStyles.getPropertyValue('--fade-wait').trim().replace("s","")) * 1000;
}
function checkFadeOut() {
    if (lastTrackTime === null) {
        return;
    }
    const FADE_DELAY = getFadeDelay();
    const timeSinceLastTrack = Date.now() - lastTrackTime;
    if (timeSinceLastTrack >= FADE_DELAY) {
        card.classList.add('fade-out');
    } else {
        card.classList.remove('fade-out');
        if (fadeTimeout) clearTimeout(fadeTimeout);
        fadeTimeout = setTimeout(checkFadeOut, FADE_DELAY - timeSinceLastTrack + 100);
    }
}
async function updateTrack() {
    if (isUpdating) return;
    isUpdating = true;
    try {
        const response = await fetch('/track-data');
        const data = await response.json();
        const content = document.getElementById('content');
        const card = document.getElementById('card');
        content.innerHTML = data.html;
        if (data.html.trim() === '' || !data.has_track) {
            card.style.display = 'none';
        } else {
            card.style.display = 'block';
            if (data.is_playing) {
                lastTrackTime = Date.now();
                card.classList.remove('fade-out');
                if (fadeTimeout) clearTimeout(fadeTimeout);
            } else {
                if (!lastTrackTime) lastTrackTime = Date.now();
                checkFadeOut();
            }
        }
    } catch (error) {
    }
    isUpdating = false;
}
updateTrack();
setInterval(updateTrack, {{ poll_interval }});
async function checkCSSUpdates() {
    try {
        const response = await fetch('/css-vars');
        const vars = await response.json();
        const root = document.documentElement;
        const card = document.getElementById('card');
        for (const [varName, varValue] of Object.entries(vars)) {
            root.style.setProperty(varName, varValue);
            if (varName === '--bg-image') {
                if (varValue === 'none') {
                    card.classList.remove('has-bg-image');
                } else if (varValue.includes('url(')) {
                    card.classList.add('has-bg-image');
                }
            }
        }
    } catch (error) {
        console.error('Error updating CSS:', error);
    }
}
setInterval(checkCSSUpdates, 2000);
</script>
</body>
</html>
"""

@app.route("/")
def index():
    with data_lock:
        data = current_track_data
    track_html = format_track_display(data)
    css_content = load_css()
    has_content = bool(track_html.strip())
    script_dir = os.path.dirname(os.path.abspath(__file__))
    bg_path = os.path.join(script_dir, "background_image.jpg")
    has_bg = os.path.exists(bg_path)
    css_lines = css_content.split('\n')
    bg_enabled = False
    for line in css_lines:
        if '--bg-image' in line and ':' in line:
            value = line.split(':')[1].split(';')[0].strip()
            bg_enabled = value != "none"
            break
    card_classes = "card"
    if has_bg and bg_enabled:
        card_classes += " has-bg-image"
    card_style = '' if has_content else 'display: none;'
    html = INDEX_HTML.replace('class="card" id="card">', f'class="{card_classes}" id="card" style="{card_style}">')
    return render_template_string(
        html, 
        track_html=track_html, 
        poll_interval=POLL_INTERVAL * 1000,
        css_content=css_content
    )

@app.route("/css-vars")
def css_vars():
    css_content = load_css()
    lines = css_content.split('\n')
    vars_dict = {}
    for line in lines:
        if '--' in line and ':' in line:
            parts = line.strip().split(':', 1)
            if len(parts) == 2:
                var_name = parts[0].strip()
                var_value = parts[1].split(';')[0].strip()
                vars_dict[var_name] = var_value
    return vars_dict

@app.route("/track-html")
def track_html():
    with data_lock:
        data = current_track_data
    return format_track_display(data)

@app.route("/track-data")
def track_data():
    with data_lock:
        data = current_track_data
        has_track = data is not None and data.get('item') is not None
        is_playing = False
        if data and data.get("is_playing") is not None:
            is_playing = data["is_playing"]
        return {
            'html': format_track_display(data),
            'has_track': has_track,
            'is_playing': data.get("is_playing", False)
        }

@app.route("/callback")
def callback():
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"Authentication error: {error}", 400
    if not code:
        return "Missing code.", 400
    try:
        auth_code_q.put_nowait(code)
    except Exception:
        pass
    return """<html><body>
    <h2>Authorization complete</h2>
    <p>You can close this tab.</p>
    </body></html>"""

@app.route("/background_image.jpg")
def background_image():
    from flask import send_file
    import os
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "background_image.jpg")
    if os.path.exists(file_path):
        response = send_file(file_path, mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    return "Image not found", 404

class StoppableServer:
    def __init__(self, app, host, port):
        self.server = make_server(host, port, app)
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
        if self.thread:
            self.thread.join(timeout=5)

class SpotifyGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Spotify Now Playing")
        self.root.geometry("500x10")
        self.root.resizable(False, False)
        self.client_id = None
        self.client_secret = None
        self.tokens = {}
        self.stop_event = threading.Event()
        self.tray_icon = None
        self.server_running = False
        self.flask_server = None
        self.setup_ui()
        self.load_settings()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_ui(self):
            main_frame = ttk.Frame(self.root, padding="20")
            main_frame.pack(fill=tk.BOTH, expand=True)
            title_label = ttk.Label(main_frame, text="Spotify Now Playing", font=("Arial", 16, "bold"))
            title_label.pack(pady=(0, 20))
            self.status_frame = ttk.LabelFrame(main_frame, text="Status", padding="10")
            self.status_frame.pack(fill=tk.X, pady=(0, 15))
            self.status_label = ttk.Label(self.status_frame, text="Not running", foreground="red")
            self.status_label.pack()
            auth_frame = ttk.LabelFrame(main_frame, text="Authentication", padding="10")
            auth_frame.pack(fill=tk.X, pady=(0, 15))
            auth_frame.columnconfigure(0, weight=0)
            auth_frame.columnconfigure(1, weight=1)
            ttk.Label(auth_frame, text="Client ID:").grid(row=0, column=0, sticky=tk.W, pady=5)
            self.client_id_entry = ttk.Entry(auth_frame)
            self.client_id_entry.grid(row=0, column=1, sticky=tk.EW, pady=5, padx=(5, 0))
            ttk.Label(auth_frame, text="Client Secret:").grid(row=1, column=0, sticky=tk.W, pady=5)
            self.client_secret_entry = ttk.Entry(auth_frame, show="*")
            self.client_secret_entry.grid(row=1, column=1, sticky=tk.EW, pady=5, padx=(5, 0))
            button_container = ttk.Frame(auth_frame)
            button_container.grid(row=2, column=0, columnspan=2, pady=(10, 0))
            self.auth_button = ttk.Button(button_container, text="Authenticate", command=self.authenticate)
            self.auth_button.pack()
            help_button = ttk.Button(button_container, text="Help", command=self.show_help)
            help_button.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
            fade_frame = ttk.LabelFrame(main_frame, text="Fade Settings", padding="10")
            fade_frame.pack(fill=tk.X, pady=(0, 15))
            ttk.Label(fade_frame, text="Disappear wait time (seconds):").grid(row=0, column=0, sticky=tk.W)
            self.fade_wait_var = tk.DoubleVar(value=float(FADE_AFTER_SECONDS))
            self.fade_wait_entry = ttk.Entry(fade_frame, textvariable=self.fade_wait_var, width=10)
            self.fade_wait_entry.grid(row=0, column=1, padx=5, pady=5)
            self.fade_wait_entry.bind('<FocusOut>', lambda e: self.on_fade_change())
            self.fade_wait_entry.bind('<Return>', lambda e: self.on_fade_change())
            ttk.Label(fade_frame, text="Fade duration (seconds):").grid(row=1, column=0, sticky=tk.W)
            self.fade_duration_var = tk.DoubleVar(value=2.0)
            self.fade_duration_entry = ttk.Entry(fade_frame, textvariable=self.fade_duration_var, width=10)
            self.fade_duration_entry.grid(row=1, column=1, padx=5, pady=5)
            self.fade_duration_entry.bind('<FocusOut>', lambda e: self.on_fade_change())
            self.fade_duration_entry.bind('<Return>', lambda e: self.on_fade_change())
            color_frame = ttk.LabelFrame(main_frame, text="Color Customization", padding="10")
            color_frame.pack(fill=tk.X, pady=(0, 15))
            color_frame.columnconfigure(0, weight=1)
            color_frame.columnconfigure(1, weight=0)
            self.color_buttons = []
            self.color_previews = {}
            colors = [
                ("Background Color", "--bg-color"),
                ("Primary Text", "--text-primary"),
                ("Progress Bar Start", "--progress-start"),
                ("Progress Bar End", "--progress-end"),
                ("Card Background", "--card-bg"),
            ]
            for i, (label, var_name) in enumerate(colors):
                btn = ttk.Button(color_frame, text=label, command=lambda v=var_name: self.choose_color(v))
                btn.grid(row=i, column=0, pady=5, sticky=tk.W+tk.E, padx=(0, 10))
                preview = tk.Canvas(color_frame, width=60, height=25, highlightthickness=1, highlightbackground="gray")
                preview.grid(row=i, column=1, pady=5)
                self.color_previews[var_name] = preview
                self.color_buttons.append(btn)
            self.card_var = tk.BooleanVar(value=True)
            self.bg_image_var = tk.BooleanVar(value=False)
            bg_image_check = ttk.Checkbutton(color_frame, text="Use background image", variable=self.bg_image_var, command=self.toggle_bg_image)
            bg_image_check.grid(row=len(colors)+1, column=0, columnspan=2, pady=5, sticky=tk.W)
            bg_image_frame = ttk.Frame(color_frame)
            bg_image_frame.grid(row=len(colors)+2, column=0, columnspan=2, pady=5, sticky=tk.W+tk.E)
            self.bg_image_button = ttk.Button(bg_image_frame, text="Choose Background Image", command=self.choose_bg_image, state=tk.DISABLED)
            self.bg_image_button.pack(side=tk.LEFT, padx=(0, 10))
            self.bg_preview_canvas = tk.Canvas(bg_image_frame, width=400, height=150, bg='white', highlightthickness=1, highlightbackground="gray")
            self.bg_preview_canvas.pack(side=tk.RIGHT)
            button_frame = ttk.Frame(main_frame)
            button_frame.pack(fill=tk.X, pady=(10, 0))
            self.start_button = ttk.Button(button_frame, text="Start Server", command=self.start_server, state=tk.DISABLED)
            self.stop_button = ttk.Button(button_frame, text="Stop Server", command=self.stop_server, state=tk.DISABLED)
            self.open_button = ttk.Button(button_frame, text="Open in Browser", command=self.open_browser, state=tk.DISABLED)
            self.minimize_button = ttk.Button(button_frame, text="Minimize to Tray", command=self.minimize_to_tray)
            quit_button = ttk.Button(button_frame, text="Quit", command=self.quit_app)
            self.start_button.pack(side=tk.LEFT, padx=(0, 5), expand=True, fill=tk.X)
            self.stop_button.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
            self.open_button.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
            self.minimize_button.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
            quit_button.pack(side=tk.LEFT, padx=(5, 0), expand=True, fill=tk.X)
            self.load_existing_credentials()
            self.root.after(100, lambda: self.root.geometry(f"500x{self.root.winfo_reqheight()}"))
            self.root.after(100, self.load_settings_on_startup)

    def start_server(self):
        if not self.tokens.get("refresh_token"):
            messagebox.showerror("Error", "Please authenticate first")
            return
        if self.server_running:
            return
        cli = sys.modules.get('flask.cli')
        if cli:
            cli.show_server_banner = lambda *x: None
        self.stop_event.clear()
        token_thread = threading.Thread(target=token_manager_loop, args=(self.client_id, self.client_secret, self.tokens, self.stop_event), daemon=True)
        token_thread.start()
        poll_thread = threading.Thread(target=playback_poll_loop, args=(self.tokens, self.stop_event), daemon=True)
        poll_thread.start()
        self.flask_server = StoppableServer(app, "127.0.0.1", 5000)
        self.flask_server.start()
        self.server_running = True
        self.update_button_states()
        self.status_label.config(text="Server running at http://127.0.0.1:5000", foreground="green")

    def load_settings_on_startup(self):
        self.load_settings()
        self.update_color_previews()
        self.root.after(500, self.update_bg_preview)
        self.root.geometry(f"500x{self.root.winfo_reqheight()}")

    def load_existing_credentials(self):
        self.client_id, self.client_secret = load_client_credentials()
        if self.client_id and self.client_secret:
            self.client_id_entry.insert(0, self.client_id)
            self.client_secret_entry.insert(0, self.client_secret)
            self.tokens = load_tokens()
            if "refresh_token" in self.tokens:
                self.status_label.config(text="Authenticated", foreground="green")
                self.auth_button.config(text="Re-authenticate")
                if not self.server_running:
                    self.start_button.config(state=tk.NORMAL)
            else:
                self.status_label.config(text="Authentication required", foreground="orange")
        self.update_button_states()

    def update_button_states(self):
        if self.server_running:
            self.start_button.config(state=tk.DISABLED)
            self.stop_button.config(state=tk.NORMAL)
            self.open_button.config(state=tk.NORMAL)
        else:
            self.start_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)
            self.open_button.config(state=tk.DISABLED)

    def authenticate(self):
        client_id = self.client_id_entry.get().strip()
        client_secret = self.client_secret_entry.get().strip()
        if not client_id or not client_secret:
            messagebox.showerror("Error", "Please enter both Client ID and Client Secret")
            return
        save_client_credentials(client_id, client_secret)
        self.client_id = client_id
        self.client_secret = client_secret
        flask_thread = threading.Thread(
            target=lambda: app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False), daemon=True)
        flask_thread.start()
        time.sleep(1)
        url = build_auth_url(client_id)
        import webbrowser
        webbrowser.open(url)
        self.status_label.config(text="Waiting for authorization...", foreground="orange")
        self.root.update()
        threading.Thread(target=self.complete_auth, daemon=True).start()

    def complete_auth(self):
        try:
            code = auth_code_q.get(timeout=120)
            token_response = exchange_code_for_token(self.client_id, self.client_secret, code)
            self.tokens = {
                "access_token": token_response["access_token"],
                "refresh_token": token_response.get("refresh_token"),
                "expires_at": (datetime.now(timezone.utc) + 
                            timedelta(seconds=int(token_response.get("expires_in", 3600)))).isoformat(),
            }
            save_tokens(self.tokens)
            self.root.after(0, lambda: self.status_label.config(text="Authenticated successfully!", foreground="green"))
            self.root.after(0, lambda: self.start_button.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.auth_button.config(text="Re-authenticate"))
            self.root.after(0, lambda: messagebox.showinfo("Success", "Authentication successful! You can now start the server."))
        except Exception as e:
            self.root.after(0, lambda: self.status_label.config(text=f"Auth failed: {str(e)}", foreground="red"))
            self.root.after(0, lambda: messagebox.showerror("Error", f"Authentication failed: {str(e)}"))

    def show_help(self):
            help_window = tk.Toplevel(self.root)
            help_window.title("Help - Spotify Now Playing")
            help_window.geometry("600x700")
            help_window.resizable(True, True)
            
            text_frame = ttk.Frame(help_window, padding="10")
            text_frame.pack(fill=tk.BOTH, expand=True)
            
            scrollbar = ttk.Scrollbar(text_frame)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            
            help_text = tk.Text(text_frame, wrap=tk.WORD, yscrollcommand=scrollbar.set, 
                            font=("Arial", 10), padx=10, pady=10)
            help_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scrollbar.config(command=help_text.yview)
            
            content = """SPOTIFY NOW PLAYING OVERLAY
    Created by NeonLightning
    GitHub: https://github.com/neonlightning/neonspotobs/

    ═══════════════════════════════════════════════════════

    SETUP INSTRUCTIONS

    1. Create a Spotify Developer App:
    • Go to https://developer.spotify.com/dashboard
    • Log in with your Spotify account
    • Click "Create app"
    • Fill in the app name and description (can be anything)
    
    2. Configure the Redirect URI:
    • In your app settings, click "Edit Settings"
    • Under "Redirect URIs", add: http://127.0.0.1:5000/callback
    • Click "Add" then "Save"
    
    3. Get Your Credentials:
    • Copy your "Client ID" from the app dashboard
    • Click "View client secret" and copy your "Client Secret"
    • Paste both into this application

    4. Authenticate:
    • Click the "Authenticate" button
    • Your browser will open to authorize the app
    • After authorization, you'll be redirected back
    
    5. Start the Server:
    • Click "Start Server" to begin streaming your now playing data
    • Click "Open in Browser" to view the overlay
    • Add the URL (http://127.0.0.1:5000) as a Browser Source in OBS

    ═══════════════════════════════════════════════════════

    SETTINGS EXPLAINED

    AUTHENTICATION
    - Client ID: Your Spotify app's unique identifier
    - Client Secret: Your app's private key (keep this secret!)

    FADE SETTINGS
    - Disappear wait time: How many seconds after music stops before the overlay fades out
    - Fade duration: How long the fade out animation takes

    COLOR CUSTOMIZATION
    - Background Color: The page background color
    - Primary Text: Main text color (song title)
    - Progress Bar Start: Left side color of the progress bar gradient
    - Progress Bar End: Right side color of the progress bar gradient
    - Card Background: The background color of the now playing card

    - Show card background: Toggle the card background on/off
    - Use background image: Enable/disable a custom background image
    - Choose Background Image: Select an image file to use as the background

    ═══════════════════════════════════════════════════════

    USAGE TIPS

    - The overlay automatically updates every 2 seconds
    - When music is paused, the overlay will fade out after the configured wait time
    - You can customize colors to match your stream theme
    - The overlay works with any browser source in OBS, Streamlabs, etc.
    - Minimize to tray to keep the server running in the background

    ═══════════════════════════════════════════════════════

    TROUBLESHOOTING

    - If authentication fails, double-check your Client ID and Secret
    - Make sure the redirect URI is exactly: http://127.0.0.1:5000/callback
    - If the overlay doesn't update, ensure Spotify is playing and you're logged in
    - Port 5000 must be available (not used by another application)

    ═══════════════════════════════════════════════════════

    For more information, issues, or updates:
    https://github.com/neonlightning/neonspotobs/
    """
            
            help_text.insert("1.0", content)
            help_text.config(state=tk.DISABLED)
            
            def open_github(event=None):
                webbrowser.open("https://github.com/neonlightning/neonspotobs/")
            
            def open_spotify_dashboard(event=None):
                webbrowser.open("https://developer.spotify.com/dashboard")
            
            def copy_callback_uri(event=None):
                self.root.clipboard_clear()
                self.root.clipboard_append("http://127.0.0.1:5000/callback")
                messagebox.showinfo("Copied", "Callback URI copied to clipboard!\nhttp://127.0.0.1:5000/callback")
            
            # Button frame
            button_frame = ttk.Frame(help_window, padding="10")
            button_frame.pack(fill=tk.X)
            
            github_button = ttk.Button(button_frame, text="Open GitHub", command=open_github)
            github_button.pack(side=tk.LEFT, padx=5)
            
            spotify_button = ttk.Button(button_frame, text="Spotify Dashboard", command=open_spotify_dashboard)
            spotify_button.pack(side=tk.LEFT, padx=5)
            
            copy_button = ttk.Button(button_frame, text="Copy Callback URI", command=copy_callback_uri)
            copy_button.pack(side=tk.LEFT, padx=5)
            
            close_button = ttk.Button(button_frame, text="Close", command=help_window.destroy)
            close_button.pack(side=tk.RIGHT, padx=5)

    def update_color_previews(self):
        if not hasattr(self, 'color_previews'):
            return
        css_content = load_css()
        lines = css_content.split('\n')
        for var_name, canvas in self.color_previews.items():
            color = self.extract_color_from_css(lines, var_name)
            if color:
                try:
                    canvas.delete("all")
                    canvas.create_rectangle(0, 0, 60, 25, fill=color, outline="")
                except:
                    pass

    def extract_color_from_css(self, css_lines, var_name):
        for line in css_lines:
            if var_name in line and ':' in line:
                parts = line.split(':')
                if len(parts) >= 2:
                    color = parts[1].split(';')[0].strip()
                    if 'rgba' in color:
                        try:
                            rgba_parts = color.replace('rgba(', '').replace(')', '').split(',')
                            if len(rgba_parts) >= 3:
                                r, g, b = [int(x.strip()) for x in rgba_parts[:3]]
                                return f'#{r:02x}{g:02x}{b:02x}'
                        except:
                            return None
                    return color
        return None

    def choose_color(self, var_name):
        current_color = self.extract_color_from_css(load_css().split('\n'), var_name)
        color = colorchooser.askcolor(title=f"Choose color for {var_name}", initialcolor=current_color if current_color and current_color.startswith('#') else None)
        if color[1]:
            self.update_css_color(var_name, color[1])
            try:
                canvas = self.color_previews[var_name]
                canvas.delete("all")
                canvas.create_rectangle(0, 0, 60, 25, fill=color[1], outline="")
            except:
                pass

    def update_css_color(self, var_name, color_value):
        css_content = load_css()
        lines = css_content.split('\n')
        for i, line in enumerate(lines):
            if var_name in line:
                lines[i] = f"    {var_name}: {color_value};"
                break
        new_css = '\n'.join(lines)
        save_css(new_css)

    def toggle_card(self):
        display_value = "flex" if self.card_var.get() else "none"
        self.update_css_color("--card-display", display_value)

    def toggle_bg_image(self):
        if self.bg_image_var.get():
            self.bg_image_button.config(state=tk.NORMAL)
            timestamp = int(time.time())
            self.update_css_color("--bg-image", f"url('/background_image.jpg?t={timestamp}')")
            self.update_bg_preview()
        else:
            self.bg_image_button.config(state=tk.DISABLED)
            self.update_css_color("--bg-image", "none")

    def choose_bg_image(self):
        filename = filedialog.askopenfilename(
            title="Select Background Image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("All files", "*.*")]
        )
        if filename:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            dest_path = os.path.join(script_dir, "background_image.jpg")
            try:
                shutil.copy2(filename, dest_path)
                timestamp = int(time.time())
                self.update_css_color("--bg-image", f"url('/background_image.jpg?t={timestamp}')")
                self.update_bg_preview()
            except Exception as e:
                print(f"Error copying file: {e}")
                messagebox.showerror("Error", f"Failed: {str(e)}")

    def update_bg_preview(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        bg_path = os.path.join(script_dir, "background_image.jpg")
        self.bg_preview_canvas.delete("all")
        self.bg_preview_canvas.update_idletasks()
        canvas_width = self.bg_preview_canvas.winfo_width()
        canvas_height = self.bg_preview_canvas.winfo_height()
        if canvas_width <= 1 or canvas_height <= 1:
            canvas_width = 400
            canvas_height = 150
            self.bg_preview_canvas.config(width=canvas_width, height=canvas_height)
            self.bg_preview_canvas.update_idletasks()
        if os.path.exists(bg_path):
            try:
                img = Image.open(bg_path)
                img_width, img_height = img.size
                width_ratio = canvas_width / img_width
                height_ratio = canvas_height / img_height
                scale_factor = min(width_ratio, height_ratio)
                new_width = int(img_width * scale_factor)
                new_height = int(img_height * scale_factor)
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self.bg_preview_photo = photo
                x = canvas_width // 2
                y = canvas_height // 2
                self.bg_preview_canvas.create_image(x, y, image=photo, anchor=tk.CENTER)
                self.bg_preview_canvas.create_rectangle(2, 2, canvas_width-2, canvas_height-2, outline="black", width=1)
            except Exception as e:
                self.bg_preview_canvas.create_text(canvas_width//2, canvas_height//2, text=f"Error\n{str(e)}", fill="red", font=('Arial', 10), justify=tk.CENTER)
        else:
            self.bg_preview_canvas.create_text(canvas_width//2, canvas_height//2, text="No Background Image", fill="gray", font=('Arial', 12), justify=tk.CENTER)

    def on_fade_change(self):
        try:
            wait_val = float(self.fade_wait_var.get())
            duration_val = float(self.fade_duration_var.get())
            css = load_css().split("\n")
            for i, line in enumerate(css):
                if '--fade-wait' in line:
                    css[i] = f"    --fade-wait: {wait_val}s;"
                elif '--fade-duration' in line:
                    css[i] = f"    --fade-duration: {duration_val}s;"
            save_css("\n".join(css))
        except ValueError:
            pass

    def load_settings(self):
        css_content = load_css()
        lines = css_content.split('\n')
        for line in lines:
            if '--fade-wait' in line and ':' in line:
                try:
                    value = line.split(':')[1].split(';')[0].strip().replace('s', '')
                    self.fade_wait_var.set(float(value))
                except:
                    pass
            elif '--fade-duration' in line and ':' in line:
                try:
                    value = line.split(':')[1].split(';')[0].strip().replace('s', '')
                    self.fade_duration_var.set(float(value))
                except:
                    pass
            elif '--card-display' in line and ':' in line:
                try:
                    value = line.split(':')[1].split(';')[0].strip()
                    self.card_var.set(value == "flex")
                except:
                    pass
            elif '--bg-image' in line and ':' in line:
                try:
                    value = line.split(':')[1].split(';')[0].strip()
                    has_image = value != "none"
                    self.bg_image_var.set(has_image)
                    if hasattr(self, 'bg_image_button'):
                        self.bg_image_button.config(state=tk.NORMAL if has_image else tk.DISABLED)
                    self.root.after(500, self.update_bg_preview)
                except:
                    pass

    def stop_server(self):
        if not self.server_running:
            return
        self.stop_event.set()
        if self.flask_server:
            self.flask_server.stop()
            self.flask_server = None
        self.server_running = False
        self.update_button_states()
        self.status_label.config(text="Server stopped", foreground="red")

    def open_browser(self):
        webbrowser.open("http://127.0.0.1:5000")

    def minimize_to_tray(self):
        self.root.withdraw()
        if not self.tray_icon:
            self.create_tray_icon()

    def create_tray_icon(self):
        image = Image.new('RGB', (64, 64), color='green')
        draw = ImageDraw.Draw(image)
        draw.rectangle([16, 16, 48, 48], fill='white')
        menu = pystray.Menu(
            pystray.MenuItem("Show", self.show_window),
            pystray.MenuItem("Open Browser", self.open_browser),
            pystray.MenuItem("Quit", self.quit_app)
        )
        self.tray_icon = pystray.Icon("spotify_now_playing", image, "Spotify Now Playing", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def show_window(self):
        self.root.deiconify()

    def on_closing(self):
        if messagebox.askokcancel("Quit", "Do you want to minimize to tray instead?"):
            self.minimize_to_tray()
        else:
            self.quit_app()

    def quit_app(self):
        self.stop_event.set()
        self.server_running = False
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.quit()
        self.root.destroy()

    def run(self):
        self.root.mainloop()

def main():
    global gui
    create_default_css()
    gui = SpotifyGUI()
    gui.run()

if __name__ == "__main__":
    main()