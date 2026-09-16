"""
note/ブログ記事 読み上げチェッカー (Flask + ブラウザ版)

テキスト(Markdown可)を貼り付けて再生すると、文章を句点単位で区切って
順番に読み上げる。見出し/太字/リストなどのMarkdown記法は表示ではレンダリング
しつつ、読み上げ時には記号を取り除いたプレーンテキストとして渡す。
URLを貼ると記事本文を取得してMarkdownに変換し、貼り付け欄に流し込む。

音声合成: Microsoft Edge の読み上げ機能 (edge-tts) を使用。無料・APIキー
不要だがインターネット接続が必要。

ローカル実行 (`python app.py` / run.bat) では、Edgeの「アプリモード」で
タブなしのウィンドウとして自動で開き、ウィンドウを閉じるとサーバーも
自動終了する。クラウドにデプロイする場合は gunicorn 等のWSGIサーバーが
`app` オブジェクトを直接importして使うため、この自動起動/自動終了ロジック
(__main__ ブロック内) は実行されない。
"""

import asyncio
import hashlib
import ipaddress
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import webbrowser
from urllib.parse import urlparse

import edge_tts
import requests
from bs4 import BeautifulSoup
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
)

app = Flask(__name__)
TEMP_DIR = tempfile.mkdtemp(prefix="note_reader_")

PORT = int(os.environ.get("PORT", 5177))
ACCESS_CODE = os.environ.get("ACCESS_CODE")  # 設定すると合言葉ゲートが有効になる

_last_ping = time.time()
_ping_lock = threading.Lock()


# ---------- アクセスゲート (ACCESS_CODE が設定されている場合のみ有効) ----------
@app.before_request
def _check_access():
    if not ACCESS_CODE:
        return None
    if request.path in ("/login", "/favicon.ico") or request.path.startswith("/static"):
        return None
    if request.cookies.get("access_code") == ACCESS_CODE:
        return None
    return redirect(f"/login?next={request.path}")


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        code = request.form.get("code", "")
        if code == ACCESS_CODE:
            resp = redirect(request.args.get("next") or "/")
            resp.set_cookie(
                "access_code",
                ACCESS_CODE,
                max_age=60 * 60 * 24 * 30,
                httponly=True,
                samesite="Lax",
                secure=request.is_secure,
            )
            return resp
        error = "合言葉が違います"
    return render_template("login.html", error=error)


@app.route("/")
def index():
    return render_template("index.html")


# ---------- 音声合成 ----------
@app.route("/synthesize", methods=["POST"])
def synthesize():
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    voice = data.get("voice") or "ja-JP-NanamiNeural"
    rate = data.get("rate") or "+0%"

    if not text:
        return jsonify({"error": "empty text"}), 400

    key = hashlib.sha1(f"{text}|{voice}|{rate}".encode("utf-8")).hexdigest()[:16]
    filename = f"{key}.mp3"
    path = os.path.join(TEMP_DIR, filename)

    if not os.path.exists(path):
        async def synth():
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            await communicate.save(path)

        try:
            asyncio.run(synth())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"url": f"/audio/{filename}"})


@app.route("/audio/<path:filename>")
def audio(filename):
    return send_from_directory(TEMP_DIR, filename, mimetype="audio/mpeg")


# ---------- 記事URL取得 ----------
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
MAX_FETCH_BYTES = 3 * 1024 * 1024
BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote"}
CONTAINER_TAGS = {"div", "section", "figure", "main", "article"}


def _is_safe_url(url):
    """外部URLのみ許可し、内部/ローカルアドレスへのアクセス(SSRF)を防ぐ。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _iter_blocks(el):
    for child in el.find_all(recursive=False):
        name = getattr(child, "name", None)
        if name in BLOCK_TAGS:
            yield child
        elif name in ("ul", "ol"):
            for li in child.find_all("li", recursive=False):
                yield li
        elif name in CONTAINER_TAGS:
            yield from _iter_blocks(child)


def _blocks_to_markdown(scope):
    lines = []
    for el in _iter_blocks(scope):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        name = el.name
        if name and name[0] == "h" and name[1:].isdigit():
            level = min(int(name[1:]), 6)
            lines.append("#" * level + " " + text)
        elif name == "li":
            lines.append("- " + text)
        elif name == "blockquote":
            lines.append("> " + text)
        else:
            lines.append(text)
    return "\n\n".join(lines)


def _extract_article(html, url):
    soup = BeautifulSoup(html, "html.parser")

    title = None
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
    if title:
        for sep in ("｜", " | ", " – ", " — "):
            if sep in title:
                title = title.split(sep)[0].strip()
                break

    hostname = urlparse(url).hostname or ""
    markdown = None

    if hostname == "note.com" or hostname.endswith(".note.com"):
        body = soup.select_one(".note-common-styles__textnote-body")
        if body:
            markdown = _blocks_to_markdown(body)

    if not markdown or len(markdown) < 100:
        for tag in soup(
            ["script", "style", "nav", "header", "footer", "aside", "iframe", "form", "button", "noscript"]
        ):
            tag.decompose()
        scope = soup.find("article") or soup.body
        if scope:
            fallback = _blocks_to_markdown(scope)
            if fallback and len(fallback) > len(markdown or ""):
                markdown = fallback

    if not markdown or len(markdown) < 50:
        return None, title

    if title and not markdown.lstrip().startswith("#"):
        markdown = f"# {title}\n\n{markdown}"

    return markdown, title


@app.route("/fetch_article", methods=["POST"])
def fetch_article():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URLを入力してください"}), 400
    if not _is_safe_url(url):
        return jsonify({"error": "このURLは取得できません"}), 400

    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=10, stream=True)
        resp.raise_for_status()
        content = b""
        for chunk in resp.iter_content(8192):
            content += chunk
            if len(content) > MAX_FETCH_BYTES:
                break
        encoding = resp.encoding or resp.apparent_encoding or "utf-8"
        html = content.decode(encoding, errors="replace")
    except requests.RequestException:
        return jsonify({"error": "記事を取得できませんでした。コピペをお試しください。"}), 502

    markdown, title = _extract_article(html, url)
    if not markdown:
        return jsonify({"error": "本文を取り出せませんでした。コピペをお試しください。"}), 422

    return jsonify({"markdown": markdown, "title": title or ""})


@app.route("/ping", methods=["POST"])
def ping():
    global _last_ping
    with _ping_lock:
        _last_ping = time.time()
    return "", 204


# ---------- デスクトップ利用時の自動起動/自動終了 (__main__ 実行時のみ) ----------
def _watchdog():
    while True:
        time.sleep(3)
        with _ping_lock:
            idle = time.time() - _last_ping
        if idle > 15:
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
            os._exit(0)


def _find_edge():
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return shutil.which("msedge")


def _open_window(url):
    edge = _find_edge()
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--window-size=860,780"])
    else:
        webbrowser.open(url)


def main():
    global _last_ping
    is_hosted = bool(os.environ.get("PORT"))  # PaaSが自動設定するのが一般的
    if is_hosted:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
        return

    _last_ping = time.time() + 15  # ブラウザ起動猶予
    threading.Thread(target=_watchdog, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    threading.Timer(1.0, lambda: _open_window(url)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
