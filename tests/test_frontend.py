#!/usr/bin/env python3
"""前端静态检查：ES5 语法、IE11 缺少的接口、颜色回退样式、页面资源引用。"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
SCRIPTS = sorted((WEB / "js").glob("*.js")) + [WEB / "easter-egg.js"]
PAGES = sorted(WEB.glob("*.html"))

# IE11 与旧版 Chrome 缺少的接口；Promise、Object.assign、find、closest 等由 common.js 补齐。
MODERN = re.compile(
    r"\.(append|prepend|replaceChildren|replaceWith|includes|startsWith|endsWith|padStart|padEnd|flat|flatMap)\("
    r"|(?<!classList)\.remove\(|\.finally\(|\bArray\.(from|of)\b|\bObject\.(entries|values)\b|\bNumber\.(isInteger|isNaN)\b"
    r"|\bURLSearchParams\b|\bfetch\(|\bnew (Map|Set|WeakMap|WeakSet|Proxy)\b|\bSymbol\b|\brequestIdleCallback\b"
)
STRINGS = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"")
COMMENTS = re.compile(r"//[^\n]*")


def code_only(text: str) -> str:
    return COMMENTS.sub("", STRINGS.sub("''", text))


class ScriptTests(unittest.TestCase):
    def test_scripts_parse_as_es5(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node 未安装")
        result = subprocess.run([node, "--expose-internals", str(ROOT / "tools" / "check_es5.js")] + [str(path) for path in SCRIPTS],
                                capture_output=True, text=True, timeout=60)
        if result.stdout.startswith("SKIP"):
            self.skipTest("当前 Node 版本不带 acorn")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_scripts_use_interfaces_available_in_ie11(self) -> None:
        for path in SCRIPTS:
            for number, line in enumerate(code_only(path.read_text(encoding="utf-8")).splitlines(), 1):
                match = MODERN.search(line)
                self.assertIsNone(match, "{}:{} {}".format(path.name, number, line.strip()))


class StyleTests(unittest.TestCase):
    def test_fallback_colors_match_the_stylesheet(self) -> None:
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "css_fallbacks.py"), "--check"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("var(", (WEB / "app-fallback.css").read_text(encoding="utf-8"))


class PageTests(unittest.TestCase):
    def test_pages_load_fallbacks_first_and_reference_existing_versioned_assets(self) -> None:
        self.assertEqual({"admin_dashboard.html", "login.html", "student_dashboard.html"}, {page.name for page in PAGES})
        for page in PAGES:
            html = page.read_text(encoding="utf-8")
            styles = re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)
            scripts = re.findall(r'<script src="([^"]+)"', html)
            self.assertEqual(["/app-fallback.css", "/app.css"], [href.split("?")[0] for href in styles], page.name)
            self.assertEqual("/js/legacy.js", scripts[0].split("?")[0], page.name)
            self.assertEqual("/easter-egg.js", scripts[-1].split("?")[0], page.name)
            for url in styles + scripts:
                path, _, query = url.partition("?")
                self.assertTrue((WEB / path.lstrip("/")).is_file(), page.name + " " + url)
                self.assertRegex(query, r"^v=\d+$", page.name + " " + url)
            self.assertIn('http-equiv="X-UA-Compatible" content="IE=edge"', html, page.name)
            self.assertIn('id="legacy"', html, page.name)


if __name__ == "__main__":
    unittest.main()
