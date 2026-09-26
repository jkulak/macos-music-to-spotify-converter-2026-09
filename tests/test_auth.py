import base64
import hashlib
import socket
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from music2spotify import auth
from music2spotify.spotify import NotAuthorised


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url):
    for _ in range(50):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as error:
            with error:
                return error.code
        except urllib.error.URLError:
            time.sleep(0.05)  # the callback server is still starting
    raise AssertionError(f"{url} never answered")


class PkceTest(unittest.TestCase):
    def test_the_challenge_is_the_s256_hash_of_the_verifier(self):
        verifier, challenge = auth.new_pkce()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
        self.assertEqual(challenge, expected.decode())
        self.assertGreaterEqual(len(verifier), 43)

    def test_the_login_link_asks_for_playlist_scopes_and_the_loopback_redirect(self):
        url = auth.authorize_url("https://accounts.example", "cid", "http://127.0.0.1:8888/callback",
                                 "chal", "st")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual(query["redirect_uri"], "http://127.0.0.1:8888/callback")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(set(query["scope"].split()),
                         {"playlist-read-private", "playlist-modify-private", "playlist-modify-public"})


class CallbackTest(unittest.TestCase):
    def wait_in_background(self, redirect, state):
        outcome = {}

        def run():
            try:
                outcome["code"] = auth.wait_for_code("127.0.0.1", redirect, state, timeout=10)
            except NotAuthorised as error:
                outcome["error"] = str(error)

        thread = threading.Thread(target=run)
        thread.start()
        return thread, outcome

    def test_the_callback_returns_the_code_and_ignores_other_paths(self):
        redirect = f"http://127.0.0.1:{free_port()}/callback"
        thread, outcome = self.wait_in_background(redirect, "st")
        self.assertEqual(get(redirect.replace("/callback", "/favicon.ico")), 404)
        self.assertEqual(get(f"{redirect}?code=abc&state=st"), 200)
        thread.join(5)
        self.assertEqual(outcome, {"code": "abc"})

    def test_a_callback_with_another_state_is_refused(self):
        redirect = f"http://127.0.0.1:{free_port()}/callback"
        thread, outcome = self.wait_in_background(redirect, "st")
        self.assertEqual(get(f"{redirect}?code=abc&state=forged"), 400)
        thread.join(5)
        self.assertIn("different state", outcome["error"])

    def test_a_denied_login_is_reported(self):
        redirect = f"http://127.0.0.1:{free_port()}/callback"
        thread, outcome = self.wait_in_background(redirect, "st")
        get(f"{redirect}?error=access_denied&state=st")
        thread.join(5)
        self.assertIn("access_denied", outcome["error"])


if __name__ == "__main__":
    unittest.main()
