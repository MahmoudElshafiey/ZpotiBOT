import json
import logging
import os
import threading
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from typing import Optional, Dict, Any

from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

logger = logging.getLogger(__name__)

# File path for storing the token cache
DEFAULT_CACHE_PATH = os.path.join(os.path.dirname(__file__), ".spotify_cache")

# Default Spotify scopes required for controlling playback
SPOTIFY_SCOPES = [
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "playlist-read-private",
]

SUCCESS_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Authentication Successful</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background-color: #121212; color: #FFFFFF; display: flex;
               align-items: center; justify-content: center; height: 100vh; margin: 0; }
        .card { background: #282828; padding: 2rem 3rem; border-radius: 8px; text-align: center;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5); }
        h1 { color: #1DB954; margin-bottom: 0.5rem; }
        p { color: #B3B3B3; }
    </style>
</head>
<body>
    <div class="card">
        <h1>Spotify Connected!</h1>
        <p>Authorization code exchanged and refresh token cached successfully.</p>
        <p>You can close this tab and return to the application.</p>
    </div>
</body>
</html>
"""


def build_html_page(title: str, message: str) -> str:
    """Builds a small HTML error/info page without pulling in a web framework."""
    safe_title = escape(title)
    safe_message = escape(message)
    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>{safe_title}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background-color: #121212; color: #FFFFFF; display: flex;
               align-items: center; justify-content: center; height: 100vh; margin: 0; }}
        .card {{ background: #282828; padding: 2rem 3rem; border-radius: 8px; text-align: center;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5); }}
        h1 {{ color: #E22134; margin-bottom: 0.5rem; }}
        p {{ color: #B3B3B3; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>{safe_title}</h1>
        <p>{safe_message}</p>
    </div>
</body>
</html>
"""


def get_spotify_oauth(
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    cache_path: str = DEFAULT_CACHE_PATH,
) -> SpotifyOAuth:
    """Creates and returns a SpotifyOAuth instance configured with application credentials."""
    client_id = (client_id or os.getenv("SPOTIFY_CLIENT_ID") or "").strip().strip("'\"")
    client_secret = (client_secret or os.getenv("SPOTIFY_CLIENT_SECRET") or "").strip().strip("'\"")
    redirect_uri = (redirect_uri or os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")).strip().strip("'\"")

    if not client_id or not client_secret or client_id == "your_spotify_client_id_here" or client_secret == "your_spotify_client_secret_here":
        raise ValueError(
            "Spotify client credentials are unset. Please fill in SPOTIFY_CLIENT_ID "
            "and SPOTIFY_CLIENT_SECRET with your Spotify Developer app credentials in the .env file."
        )

    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=" ".join(SPOTIFY_SCOPES),
        cache_path=cache_path,
        open_browser=False,
    )


def save_token_to_cache(token_info: Dict[str, Any], cache_path: str = DEFAULT_CACHE_PATH) -> None:
    """Ensures token information (including refresh_token) is persisted to the cache file."""
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(token_info, f, indent=4)


def get_cached_token(cache_path: str = DEFAULT_CACHE_PATH) -> Optional[Dict[str, Any]]:
    """Loads token information from the cache file if available."""
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as exc:
        logger.warning("Could not read token cache from %s: %s", cache_path, exc)
        return None


def run_auth_server(
    sp_oauth: Optional[SpotifyOAuth] = None,
    cache_path: str = DEFAULT_CACHE_PATH,
    auto_open_browser: bool = True,
) -> Dict[str, Any]:
    """Runs a small local HTTP server to capture the OAuth callback and exchanges code for tokens.

    Uses only the Python standard library so the always-on bot does not need Flask/Werkzeug.

    Returns:
        dict: The retrieved token info containing access_token, refresh_token, etc.
    """
    if sp_oauth is None:
        sp_oauth = get_spotify_oauth(cache_path=cache_path)

    parsed_redirect = urlparse(sp_oauth.redirect_uri)
    host = parsed_redirect.hostname or "127.0.0.1"
    port = parsed_redirect.port or 8888
    callback_path = parsed_redirect.path or "/callback"

    auth_result: Dict[str, Any] = {}
    auth_event = threading.Event()
    server: ThreadingHTTPServer

    def finish_request_handling() -> None:
        auth_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            request_url = urlparse(self.path)
            if request_url.path != callback_path:
                self._respond(404, build_html_page("Not Found", "Unknown callback path."))
                return

            params = parse_qs(request_url.query)
            error = params.get("error", [None])[0]
            if error:
                auth_result["error"] = error
                self._respond(400, build_html_page("Authentication failed", f"Error: {error}"))
                finish_request_handling()
                return

            code = params.get("code", [None])[0]
            if not code:
                auth_result["error"] = "No authorization code provided in callback."
                self._respond(400, build_html_page("Authentication failed", "No authorization code received."))
                finish_request_handling()
                return

            try:
                token_info = sp_oauth.get_access_token(code, as_dict=True)
                if not token_info:
                    raise ValueError("Failed to retrieve access token from Spotify.")

                # Explicitly persist token info (including refresh_token) to the cache file
                save_token_to_cache(token_info, cache_path=cache_path)
                auth_result["token_info"] = token_info
                self._respond(200, SUCCESS_HTML)
            except Exception as exc:
                auth_result["error"] = str(exc)
                self._respond(500, build_html_page("Token Exchange Error", str(exc)))
            finally:
                finish_request_handling()

        def _respond(self, status: int, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("OAuth callback: " + fmt, *args)

    server = ThreadingHTTPServer((host, port), CallbackHandler)
    server.daemon_threads = True

    auth_url = sp_oauth.get_authorize_url()
    print("=" * 70)
    print("Spotify Authorization Required:")
    print(auth_url)
    print("=" * 70)

    # Start the server on a background thread
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    if auto_open_browser:
        webbrowser.open(auth_url)

    # Wait for the callback to complete
    if not auth_event.wait(timeout=300):
        server.shutdown()
        server.server_close()
        raise RuntimeError("Timed out waiting for the Spotify OAuth callback.")

    server_thread.join(timeout=3)
    server.server_close()

    if "error" in auth_result:
        raise RuntimeError(f"Authentication failed: {auth_result['error']}")

    return auth_result.get("token_info", {})


def get_authenticated_spotify(cache_path: str = DEFAULT_CACHE_PATH) -> spotipy.Spotify:
    """Returns an authenticated spotipy.Spotify client instance using cached or newly authorized tokens."""
    sp_oauth = get_spotify_oauth(cache_path=cache_path)
    token_info = sp_oauth.validate_token(sp_oauth.cache_handler.get_cached_token())

    if not token_info:
        print("No valid cached token found. Starting interactive authentication flow...")
        token_info = run_auth_server(sp_oauth=sp_oauth, cache_path=cache_path)

    return spotipy.Spotify(auth_manager=sp_oauth)


if __name__ == "__main__":
    print("Initializing Spotify Authorization Flow...")
    try:
        tokens = run_auth_server()
        refresh_token = tokens.get("refresh_token")
        print("\n" + "=" * 70)
        print("SPOTIFY AUTHENTICATION SUCCESSFUL!")
        print("=" * 70)
        if refresh_token:
            print("Refresh token saved to .spotify_cache successfully.")
            print("\n📋 FOR BOT-HOSTING.NET / CLOUD DEPLOYMENTS:")
            print("Copy and paste this line into your hosting Environment Variables:\n")
            print(f"SPOTIFY_REFRESH_TOKEN={refresh_token}\n")
            print("=" * 70)
        else:
            print("Token cached, but no refresh token was included in the response.")
    except Exception as e:
        print(f"\nAuthentication process failed: {e}")
