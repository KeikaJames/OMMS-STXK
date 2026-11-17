#!/usr/bin/env python3
"""名单表格读取与账号表生成：xlsx、xls（BIFF8）、csv、网页表格、XML 表格 2003。"""

from __future__ import annotations

import io
from pathlib import Path
import struct
import sys
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sheets  # noqa: E402

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None


# ---------------------------------------------------------------------------
# 测试用的 BIFF8 写入器：共享字符串表按很小的记录长度拆分，字符数组跨记录。
# ---------------------------------------------------------------------------
def record(kind: int, body: bytes) -> bytes:
    return struct.pack("<HH", kind, len(body)) + body


def wide(text: str) -> bool:
    return any(ord(char) > 255 for char in text)


def unicode_body(text: str) -> bytes:
    flag = 1 if wide(text) else 0
    return struct.pack("<HB", len(text), flag) + text.encode("utf-16-le" if flag else "latin-1")


def sst_records(strings: list[str], chunk: int = 48) -> bytes:
    records = []
    current = bytearray(struct.pack("<II", len(strings), len(strings)))
    kind = 0x00FC

    def flush():
        nonlocal current, kind
        records.append(record(kind, bytes(current)))
        current = bytearray()
        kind = 0x003C

    for text in strings:
        is_wide = wide(text)
        if len(current) + 3 + (2 if is_wide else 1) > chunk:
            flush()
        current += struct.pack("<HB", len(text), 1 if is_wide else 0)
        remaining = text
        while remaining:
            width = 2 if is_wide else 1
            room = (chunk - len(current)) // width
            if room <= 0:
                flush()
                is_wide = wide(remaining)
                current += bytes([1 if is_wide else 0])
                continue
            part, remaining = remaining[:room], remaining[room:]
            current += part.encode("utf-16-le" if is_wide else "latin-1")
    flush()
    return b"".join(records)


def build_workbook_stream(book: list[tuple[str, dict, list]]) -> bytes:
    strings: list[str] = []
    index: dict[str, int] = {}

    def shared(text):
        if text not in index:
            index[text] = len(strings)
            strings.append(text)
        return index[text]

    sheet_streams = []
    for _name, cells, merges in book:
        body = bytearray(record(0x0809, struct.pack("<HHHHII", 0x0600, 0x0010, 0x0DBB, 0x07CC, 0, 6)))
        for (row, col), value in sorted(cells.items()):
            if isinstance(value, str) and value.startswith("label:"):
                body += record(0x0204, struct.pack("<HHH", row, col, 15) + unicode_body(value[6:]))
            elif isinstance(value, str) and value.startswith("formula:"):
                result = bytes([0, 0, 0, 0, 0, 0, 0xFF, 0xFF])
                body += record(0x0006, struct.pack("<HHH", row, col, 15) + result + struct.pack("<HIH", 0, 0, 0))
                body += record(0x0207, unicode_body(value[8:]))
            elif isinstance(value, str):
                body += record(0x00FD, struct.pack("<HHHI", row, col, 15, shared(value)))
            elif isinstance(value, int):
                body += record(0x027E, struct.pack("<HHHi", row, col, 15, (value << 2) | 0x02))
            else:
                body += record(0x0203, struct.pack("<HHHd", row, col, 15, float(value)))
        if merges:
            body += record(0x00E5, struct.pack("<H", len(merges)) + b"".join(
                struct.pack("<HHHH", r1, r2, c1, c2) for r1, c1, r2, c2 in merges))
        body += record(0x000A, b"")
        sheet_streams.append(bytes(body))
    bof = record(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0x0DBB, 0x07CC, 0, 6))
    sst = sst_records(strings)
    eof = record(0x000A, b"")

    def boundsheet(name, position):
        flag = 1 if wide(name) else 0
        encoded = name.encode("utf-16-le" if flag else "latin-1")
        return record(0x0085, struct.pack("<IBB", position, 0, 0) + bytes([len(name), flag]) + encoded)

    placeholder = b"".join(boundsheet(name, 0) for name, _cells, _merges in book)
    position = len(bof) + len(placeholder) + len(sst) + len(eof)
    sheets_index = []
    for stream in sheet_streams:
        sheets_index.append(position)
        position += len(stream)
    boundsheets = b"".join(boundsheet(name, at) for (name, _cells, _merges), at in zip(book, sheets_index))
    return bof + boundsheets + sst + eof + b"".join(sheet_streams)


def build_cfb(stream: bytes) -> bytes:
    stream = stream + b"\x00" * max(0, 4096 - len(stream))
    sector = 512
    data_sectors = (len(stream) + sector - 1) // sector
    padded = stream + b"\x00" * (data_sectors * sector - len(stream))
    directory_sector = 1 + data_sectors
    assert directory_sector < 128
    fat = [0xFFFFFFFF] * 128
    fat[0] = 0xFFFFFFFD
    for i in range(data_sectors):
        fat[1 + i] = 2 + i if i < data_sectors - 1 else 0xFFFFFFFE
    fat[directory_sector] = 0xFFFFFFFE
    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HHHHH", header, 24, 0x003E, 0x0003, 0xFFFE, 9, 6)
    struct.pack_into("<I", header, 44, 1)
    struct.pack_into("<I", header, 48, directory_sector)
    struct.pack_into("<I", header, 56, 4096)
    struct.pack_into("<IIII", header, 60, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
    struct.pack_into("<109I", header, 76, *([0] + [0xFFFFFFFF] * 108))

    def entry(name, kind, start, size, child=0xFFFFFFFF):
        raw = bytearray(128)
        encoded = (name + "\x00").encode("utf-16-le")
        raw[0:len(encoded)] = encoded
        struct.pack_into("<H", raw, 64, len(encoded))
        raw[66], raw[67] = kind, 1
        struct.pack_into("<III", raw, 68, 0xFFFFFFFF, 0xFFFFFFFF, child)
        struct.pack_into("<II", raw, 116, start, size)
        return bytes(raw)

    directory = entry("Root Entry", 5, 0xFFFFFFFE, 0, child=1) + entry("Workbook", 2, 1, len(stream)) + bytes(256)
    return bytes(header) + struct.pack("<128I", *fat) + padded + directory


def build_xls(book) -> bytes:
    return build_cfb(build_workbook_stream(book))


ROSTER_XLS = [
    ("高一年级", {
        (0, 0): "高一年级学生名单",
        (1, 0): "序号", (1, 1): "姓 名", (1, 2): "性别", (1, 3): "班级",
        (2, 0): 1, (2, 1): "张三", (2, 2): "男", (2, 3): "高一(1)班",
        (3, 0): 2.0, (3, 1): "label:李 四", (3, 2): "女",
        (4, 0): 3, (4, 1): "formula:王五", (4, 2): "男",
        (5, 0): 4, (5, 1): "巴特尔•乌兰", (5, 3): "高一（2）班",
        (7, 1): "合计",
    }, [(0, 0, 0, 3), (2, 3, 4, 3)]),
    ("高二3班", {(0, 0): "学生姓名", (0, 1): "学号", (1, 0): "赵六", (1, 1): 20260001, (2, 0): "Anna  Lee"}, []),
    ("说明", {(0, 0): "这一页没有名单"}, []),
]


def rows_of(result):
    return [(row["name"], row["class"], tuple(row["notes"])) for row in result["rows"]]


class XlsTests(unittest.TestCase):
    def test_xls_roster_with_merges_labels_formulas_and_split_shared_strings(self) -> None:
        data = build_xls(ROSTER_XLS)
        result = sheets.parse_roster("名单.xls", data)
        self.assertEqual([
            ("张三", "高一1班", ()),
            ("李四", "高一1班", ()),
            ("王五", "高一1班", ()),
            ("巴特尔·乌兰", "高一2班", ()),
            ("赵六", "高二3班", ("班级取自工作表名",)),
            ("Anna Lee", "高二3班", ("班级取自工作表名",)),
        ], rows_of(result))
        self.assertEqual([("高一年级", 2), ("高二3班", 1), ("说明", None)],
                         [(info["name"], info["header"]) for info in result["sheets"]])
        self.assertEqual(1, result["skipped"])

    def test_long_shared_strings_survive_record_splits(self) -> None:
        names = ["学生{:03d}号名字比较长一些".format(i) for i in range(60)] + ["Latin name {}".format(i) for i in range(20)]
        cells = {(0, 0): "姓名", (0, 1): "班级"}
        for i, name in enumerate(names, start=1):
            cells[(i, 0)] = name
            cells[(i, 1)] = "高一{}班".format(i % 7 + 1)
        result = sheets.parse_roster("x.xls", build_xls([("名单", cells, [])]))
        self.assertEqual(names, [row["name"] for row in result["rows"]])

    def test_corrupt_chains_are_reported(self) -> None:
        data = bytearray(build_xls(ROSTER_XLS))
        struct.pack_into("<I", data, 512 + 4, 1)
        with self.assertRaises(sheets.TableError):
            sheets.read_table("x.xls", bytes(data))
        with self.assertRaises(sheets.TableError):
            sheets.read_table("x.xls", bytes(data[:700]))


@unittest.skipIf(openpyxl is None, "openpyxl unavailable")
class XlsxTests(unittest.TestCase):
    def workbook(self) -> bytes:
        book = openpyxl.Workbook()
        first = book.active
        first.title = "名单"
        first["A1"] = "2026 年秋季社团选课名单"
        first.merge_cells("A1:E1")
        first.append([])
        first.append(["序号", "学生姓名", "年级", "班", "备注"])
        first.append([1, "张伟", "高一", 1, ""])
        first.append([2, "王芳", "高一", "2班", ""])
        first.append([])
        first.append(["序号", "学生姓名", "年级", "班", ""])
        first.append([3, "刘洋", "初二", "(3)", ""])
        first.append([4, "张伟", "高一", 1, "同名"])
        first.append(["", "合计", "", "", ""])
        second = book.create_sheet("高一1班")
        second.append(["姓名", "性别"])
        second.append(["陈静", "女"])
        third = book.create_sheet("Sheet3")
        third.append(["姓名", "性别"])
        third.append(["高一5班"])
        third.append(["甲", "男"])
        third.append(["乙", "女"])
        third.append(["高一6班"])
        third.append(["丙", "男"])
        hidden = book.create_sheet("隐藏")
        hidden.append(["姓名", "班级"])
        hidden.append(["不应出现", "高一1班"])
        hidden.sheet_state = "hidden"
        out = io.BytesIO()
        book.save(out)
        return out.getvalue()

    def test_xlsx_roster_with_grade_columns_titles_and_context_rows(self) -> None:
        result = sheets.parse_roster("名单.xlsx", self.workbook())
        self.assertEqual([
            ("张伟", "高一1班", ("同班同名",)),
            ("王芳", "高一2班", ()),
            ("刘洋", "初二3班", ()),
            ("张伟", "高一1班", ("同班同名",)),
            ("陈静", "高一1班", ("班级取自工作表名",)),
            ("甲", "高一5班", ()),
            ("乙", "高一5班", ()),
            ("丙", "高一6班", ()),
        ], rows_of(result))
        self.assertEqual(["名单", "高一1班", "Sheet3"], [info["name"] for info in result["sheets"]])

    def test_entities_in_xml_parts_are_refused(self) -> None:
        data = self.workbook()
        source = zipfile.ZipFile(io.BytesIO(data))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as target:
            for item in source.infolist():
                content = source.read(item)
                if item.filename == "xl/workbook.xml":
                    content = b'<!DOCTYPE x [<!ENTITY a "aaaa">]>' + content
                target.writestr(item, content)
        with self.assertRaises(sheets.TableError):
            sheets.read_table("x.xlsx", out.getvalue())

    def test_accounts_workbook_is_sorted_by_class_and_ready_to_print(self) -> None:
        accounts = [
            {"name": "乙", "class": "高一10班", "username": "yi", "password": "p2"},
            {"name": "甲", "class": "高一2班", "username": "jia", "password": "p1"},
            {"name": "丙", "class": "高一2班", "username": "bing", "password": "p3"},
        ]
        book = openpyxl.load_workbook(io.BytesIO(sheets.accounts_xlsx(accounts)))
        sheet = book["账号"]
        values = [[cell.value for cell in row] for row in sheet.iter_rows()]
        self.assertEqual(["姓名", "班级", "账号", "密码"], values[0])
        self.assertEqual([["甲", "高一2班", "jia", "p1"], ["丙", "高一2班", "bing", "p3"],
                          ["乙", "高一10班", "yi", "p2"]], values[1:])
        self.assertEqual([3], [brk.id for brk in sheet.row_breaks.brk])
        self.assertEqual("$1:$1", sheet.print_title_rows)
        self.assertEqual(1, sheet.page_setup.fitToWidth)


class TextTableTests(unittest.TestCase):
    def test_csv_in_gbk_and_tab_separated_utf8(self) -> None:
        gbk = "姓名,班级\n张三,高一1班\n李四,高一2班\n".encode("gb18030")
        self.assertEqual([("张三", "高一1班", ()), ("李四", "高一2班", ())],
                         rows_of(sheets.parse_roster("a.csv", gbk)))
        tabbed = "﻿序号\t姓名\t班级\n1\t王五\t初一(4)班\n".encode("utf-8")
        self.assertEqual([("王五", "初一4班", ())], rows_of(sheets.parse_roster("a.txt", tabbed)))

    def test_web_page_tables_and_spreadsheet_2003(self) -> None:
        page = ("<html><body><table><tr><th>姓名</th><th colspan=\"2\">班级</th></tr>"
                "<tr><td>张三</td><td>高一1班</td><td></td></tr></table></body></html>").encode("utf-8")
        self.assertEqual([("张三", "高一1班", ())], rows_of(sheets.parse_roster("导出.xls", page)))
        xml = ('<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
               'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="名单"><Table>'
               '<Row><Cell><Data ss:Type="String">姓名</Data></Cell><Cell><Data ss:Type="String">班级</Data></Cell></Row>'
               '<Row><Cell><Data ss:Type="String">李四</Data></Cell>'
               '<Cell ss:Index="2"><Data ss:Type="String">高二2班</Data></Cell></Row>'
               '</Table></Worksheet></Workbook>').encode("utf-8")
        self.assertEqual([("李四", "高二2班", ())], rows_of(sheets.parse_roster("导出.xls", xml)))

    def test_files_without_the_two_headers_are_reported(self) -> None:
        with self.assertRaises(sheets.TableError) as caught:
            sheets.parse_roster("a.csv", "学号,性别\n1,男\n".encode("utf-8"))
        self.assertIn("姓名", str(caught.exception))
        with self.assertRaises(sheets.TableError):
            sheets.parse_roster("a.xlsx", b"\x00\x01\x02 not a workbook")

    def test_summary_rows_and_names_with_digits(self) -> None:
        text = "序号,姓名,班级\n1,张伟123,高一1班\n2,O'Neil,高一1班\n3,Батбаяр,高一1班\n合计,3 人,\n".encode("utf-8")
        parsed = sheets.parse_roster("a.csv", text)
        self.assertEqual([("张伟123", "高一1班", ("姓名有误",)), ("O'Neil", "高一1班", ()), ("Батбаяр", "高一1班", ())],
                         rows_of(parsed))
        self.assertEqual(1, parsed["skipped"])

    def test_names_and_classes_are_normalized(self) -> None:
        self.assertEqual("张三", sheets.clean_name(" 张　三​ "))
        self.assertEqual("巴特尔·乌兰", sheets.clean_name("巴特尔・乌兰"))
        self.assertEqual("高一1班", sheets.clean_class("高一（1）班"))
        self.assertEqual("高二3班", sheets.clean_class("高二年级3班"))
        self.assertEqual("5班", sheets.clean_class("５"))


if __name__ == "__main__":
    unittest.main()
