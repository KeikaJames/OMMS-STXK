#!/usr/bin/env python3
"""由 web/app.css 生成 web/app-fallback.css：用到颜色变量的声明换成浅色主题的实际值。

页面先加载 app-fallback.css，再加载 app.css。支持 CSS 变量的浏览器取 app.css 的变量值；
IE11 忽略含 var() 的声明，保留回退值。修改 app.css 后执行：python3 tools/css_fallbacks.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web" / "app.css"
TARGET = ROOT / "web" / "app-fallback.css"
HEADER = "/* 由 tools/css_fallbacks.py 根据 app.css 生成，供不支持 CSS 变量的浏览器使用。 */\n"


def strip_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def variables(text):
    block = re.search(r":root\s*\{(.*?)\}", text, re.S).group(1)
    return {name: value.strip() for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


def resolve(value, table):
    for _ in range(5):
        updated = re.sub(r"var\((--[\w-]+)\)", lambda match: table[match.group(1)], value)
        if updated == value:
            break
        value = updated
    return value


def rules(text):
    """返回 (外层 at 规则, 选择器, 声明体) 序列。"""
    found, stack, position = [], [], 0
    while position < len(text):
        brace = text.find("{", position)
        close = text.find("}", position)
        if close != -1 and (brace == -1 or close < brace):
            if stack:
                stack.pop()
            position = close + 1
            continue
        if brace == -1:
            break
        prelude = " ".join(text[position:brace].split())
        if prelude.startswith("@"):
            stack.append(prelude)
            position = brace + 1
            continue
        end = text.find("}", brace)
        found.append((tuple(stack), prelude, text[brace + 1:end]))
        position = end + 1
    return found


def build(source_text):
    text = strip_comments(source_text)
    table = variables(text)
    groups = []
    for stack, selector, body in rules(text):
        if selector.startswith(":root") or any("prefers-color-scheme" in rule or rule.startswith("@keyframes")
                                               for rule in stack):
            continue
        declarations = []
        for item in body.split(";"):
            if "var(--" not in item or ":" not in item:
                continue
            prop, value = item.split(":", 1)
            declarations.append("{}: {};".format(prop.strip(), resolve(" ".join(value.split()), table)))
        if not declarations:
            continue
        line = "{} {{ {} }}".format(selector, " ".join(declarations))
        if groups and groups[-1][0] == stack:
            groups[-1][1].append(line)
        else:
            groups.append((stack, [line]))
    out = [HEADER]
    for stack, lines in groups:
        indent = ""
        for rule in stack:
            out.append(indent + rule + " {\n")
            indent += "  "
        for line in lines:
            out.append(indent + line + "\n")
        for depth in range(len(stack) - 1, -1, -1):
            out.append("  " * depth + "}\n")
    return "".join(out)


def main():
    generated = build(SOURCE.read_text(encoding="utf-8"))
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != generated:
            print("app-fallback.css 需要重新生成：python3 tools/css_fallbacks.py")
            return 1
        return 0
    TARGET.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
