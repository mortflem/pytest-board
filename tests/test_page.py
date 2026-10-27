"""The page the board serves."""
import re
import shutil
import subprocess

import pytest

import server as pb
from conftest import REPO


def test_page_and_server_versions_match():
    """PAGE_VERSION in static/index.html and BOARD_VERSION in the server are bumped together (the page asks for a
    restart when the running server is older than it)."""
    page = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    assert re.search(r"const PAGE_VERSION='([^']+)'", page).group(1) == pb.BOARD_VERSION


def test_page_script_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed (it only checks the page's JavaScript for syntax errors)")
    page = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert scripts
    js = tmp_path / "page.js"
    js.write_text("\n".join(scripts), encoding="utf-8")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_served(board):
    html = board.get_page("/")
    assert "<title>" in html and "PAGE_VERSION" in html
