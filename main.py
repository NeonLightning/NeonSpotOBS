#!/usr/bin/env python3
import base64, json, os, queue, threading, time, urllib.parse, requests, logging, sys
from datetime import datetime, timedelta, timezone
from flask import Flask, redirect, render_template_string, request
import tkinter as tk
from tkinter import ttk, colorchooser, messagebox
import pystray
from PIL import Image, ImageDraw

TOKENS_FILE = "spotify_tokens.json"
CLIENT_FILE = "spotify_client.json"
CSS_FILE = "styles.css"
REDIRECT_URI = "http://127.0.0.1:5000/callback"
SCOPE = "user-read-currently-playing user-read-playback-state"
POLL_INTERVAL = 2

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app.logger.disabled = True
logging.getLogger('werkzeug').disabled = True
current_track_data = None
data_lock = threading.Lock()
auth_code_q = queue.Queue()

def create_default_css():
    default_css = """:root {
    /* Color Variables */
    --bg-color: #00ff00;
    --text-primary: #eee;
    --text-secondary: #cfcfcf;
    --text-tertiary: #9a9a9a;
    --progress-bg: rgba(255, 0, 0);
    --progress-start: rgba(94, 255, 155);
    --progress-end: rgba(0, 176, 255);
    --card-bg: rgba(30, 30, 30);
    --card-shadow: 0 8px 32px rgba(0, 0, 0);

    /* Card Toggle: set to 'none' to display directly on background, 'flex' to show card */
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
                last_update = current_time
            except Exception:
                pass
        time.sleep(0.1)

def format_track_display(data):
    if not data or not data.get('item'):
        return '<div class="none">Nothing is playing or no active device.</div>'
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
        }
        @supports (display: var(--card-display)) {
            .card {
                background: none;
                border-radius: 0;
                box-shadow: none;
                backdrop-filter: none;
            }
            .card::before {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background: var(--card-bg);
                border-radius: 12px;
                box-shadow: var(--card-shadow);
                backdrop-filter: blur(10px);
                display: var(--card-display);
                z-index: -1;
            }
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
    <div class="card">
        <div id="content">{{ track_html|safe }}</div>
    </div>
<script>
(function() {
    const cardDisplay = getComputedStyle(document.documentElement).getPropertyValue('--card-display').trim();
    if (cardDisplay === 'none') {
        const card = document.querySelector('.card');
        card.style.background = 'transparent';
        card.style.borderRadius = '0';
        card.style.boxShadow = 'none';
        card.style.backdropFilter = 'none';
    }
})();
let isUpdating = false;
async function updateTrack() {
    if (isUpdating) return;
    isUpdating = true;
    try {
        const response = await fetch('/track-html');
        const html = await response.text();
        document.getElementById('content').innerHTML = html;
    } catch (error) {
    }
    isUpdating = false;
}
updateTrack();
setInterval(updateTrack, {{ poll_interval }});
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
    return render_template_string(
        INDEX_HTML, 
        track_html=track_html, 
        poll_interval=POLL_INTERVAL * 1000,
        css_content=css_content
    )

@app.route("/track-html")
def track_html():
    with data_lock:
        data = current_track_data
    return format_track_display(data)

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

class SpotifyGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Spotify Now Playing")
        self.root.geometry("500x620")
        self.root.resizable(False, False)
        self.client_id = None
        self.client_secret = None
        self.tokens = {}
        self.stop_event = threading.Event()
        self.tray_icon = None
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.setup_ui()

    def setup_ui(self):
        main_frame = ttk.Frame(self.root, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)
        title_label = ttk.Label(main_frame, text="Spotify Now Playing", 
                                font=("Arial", 16, "bold"))
        title_label.pack(pady=(0, 20))
        self.status_frame = ttk.LabelFrame(main_frame, text="Status", padding="10")
        self.status_frame.pack(fill=tk.X, pady=(0, 15))
        self.status_label = ttk.Label(self.status_frame, text="Not running", 
                                    foreground="red")
        self.status_label.pack()
        auth_frame = ttk.LabelFrame(main_frame, text="Authentication", padding="10")
        auth_frame.pack(fill=tk.X, pady=(0, 15))
        ttk.Label(auth_frame, text="Client ID:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.client_id_entry = ttk.Entry(auth_frame, width=40)
        self.client_id_entry.grid(row=0, column=1, pady=5, padx=(5, 0))
        ttk.Label(auth_frame, text="Client Secret:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.client_secret_entry = ttk.Entry(auth_frame, width=40, show="*")
        self.client_secret_entry.grid(row=1, column=1, pady=5, padx=(5, 0))
        self.auth_button = ttk.Button(auth_frame, text="Authenticate", 
                                    command=self.authenticate)
        self.auth_button.grid(row=2, column=0, columnspan=2, pady=(10, 0))
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
            btn = ttk.Button(color_frame, text=label, 
                        command=lambda v=var_name: self.choose_color(v))
            btn.grid(row=i, column=0, pady=5, sticky=tk.W+tk.E, padx=(0, 10))
            preview = tk.Canvas(color_frame, width=60, height=25, highlightthickness=1, 
                            highlightbackground="gray")
            preview.grid(row=i, column=1, pady=5)
            self.color_previews[var_name] = preview
            self.color_buttons.append(btn)
        self.update_color_previews()
        self.card_var = tk.BooleanVar(value=True)
        card_check = ttk.Checkbutton(color_frame, text="Show card background", 
                                    variable=self.card_var,
                                    command=self.toggle_card)
        card_check.grid(row=len(colors), column=0, columnspan=2, pady=(10, 5), sticky=tk.W)
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=(10, 0))
        self.start_button = ttk.Button(button_frame, text="Start Server", 
                                    command=self.start_server, state=tk.DISABLED)
        self.start_button.pack(side=tk.LEFT, padx=(0, 5), expand=True, fill=tk.X)
        self.open_button = ttk.Button(button_frame, text="Open in Browser", 
                                    command=self.open_browser, state=tk.DISABLED)
        self.open_button.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        self.minimize_button = ttk.Button(button_frame, text="Minimize to Tray", 
                                        command=self.minimize_to_tray)
        self.minimize_button.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        quit_button = ttk.Button(button_frame, text="Quit", command=self.quit_app)
        quit_button.pack(side=tk.LEFT, padx=(5, 0), expand=True, fill=tk.X)
        self.load_existing_credentials()

    def load_existing_credentials(self):
        self.client_id, self.client_secret = load_client_credentials()
        if self.client_id and self.client_secret:
            self.client_id_entry.insert(0, self.client_id)
            self.client_secret_entry.insert(0, self.client_secret)
            self.tokens = load_tokens()
            if "refresh_token" in self.tokens:
                self.status_label.config(text="Authenticated", foreground="green")
                self.start_button.config(state=tk.NORMAL)
                self.auth_button.config(text="Re-authenticate")
            else:
                self.status_label.config(text="Authentication required", foreground="orange")

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
            target=lambda: app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False), 
            daemon=True
        )
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
            self.root.after(0, lambda: self.status_label.config(
                text="Authenticated successfully!", foreground="green"))
            self.root.after(0, lambda: self.start_button.config(state=tk.NORMAL))
            self.root.after(0, lambda: messagebox.showinfo(
                "Success", "Authentication successful! You can now start the server."))
        except Exception as e:
            self.root.after(0, lambda: self.status_label.config(
                text=f"Auth failed: {str(e)}", foreground="red"))
            self.root.after(0, lambda: messagebox.showerror(
                "Error", f"Authentication failed: {str(e)}"))

    def update_color_previews(self):
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
        color = colorchooser.askcolor(title=f"Choose color for {var_name}", 
                                    initialcolor=current_color if current_color and current_color.startswith('#') else None)
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
        messagebox.showinfo("Success", f"Color updated! Refresh browser to see changes.")

    def toggle_card(self):
        display_value = "flex" if self.card_var.get() else "none"
        self.update_css_color("--card-display", display_value)

    def start_server(self):
        if not self.tokens.get("refresh_token"):
            messagebox.showerror("Error", "Please authenticate first")
            return
        cli = sys.modules.get('flask.cli')
        if cli:
            cli.show_server_banner = lambda *x: None
        token_thread = threading.Thread(
            target=token_manager_loop, 
            args=(self.client_id, self.client_secret, self.tokens, self.stop_event), 
            daemon=True
        )
        token_thread.start()
        poll_thread = threading.Thread(
            target=playback_poll_loop, 
            args=(self.tokens, self.stop_event), 
            daemon=True
        )
        poll_thread.start()
        flask_thread = threading.Thread(
            target=lambda: app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False), 
            daemon=True
        )
        flask_thread.start()
        self.status_label.config(text="Server running at http://127.0.0.1:5000", 
                                foreground="green")
        self.start_button.config(state=tk.DISABLED)
        self.open_button.config(state=tk.NORMAL)

    def open_browser(self):
        import webbrowser
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
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.quit()
        self.root.destroy()

    def run(self):
        self.root.mainloop()

def main():
    create_default_css()
    gui = SpotifyGUI()
    gui.run()

if __name__ == "__main__":
    main()