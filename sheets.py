"""表格读写：读取学生名单（xlsx、xls、csv 及常见的网页表格导出），生成 Excel。只用标准库。"""

import csv
import html.parser
import io
import math
import re
import struct
import unicodedata
import zipfile
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape

MAX_SHEETS = 50
MAX_ROWS = 20000
MAX_COLUMNS = 256
MAX_CELL = 200
MAX_ZIP_ENTRIES = 2000
MAX_PART_BYTES = 64 * 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_SHARED_STRINGS = 500000
HEADER_SCAN_ROWS = 40


class TableError(ValueError):
    """文件无法读取；消息直接显示给管理员。"""


class Sheet:
    def __init__(self, name, rows, merges=None):
        self.name = name
        self.rows = rows
        self.merges = merges or []


def _number_text(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    if not math.isfinite(number):
        return ""
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return format(number, "g")


def _dense(cells):
    """{(行, 列): 文本} → 行列表；行号与列号从 0 开始。"""
    if not cells:
        return []
    height = min(MAX_ROWS, max(row for row, _col in cells) + 1)
    width = min(MAX_COLUMNS, max(col for _row, col in cells) + 1)
    rows = [[""] * width for _ in range(height)]
    for (row, col), value in cells.items():
        if row < height and col < width:
            rows[row][col] = str(value)[:MAX_CELL]
    return rows


# ---------------------------------------------------------------------------
# xlsx
# ---------------------------------------------------------------------------
_DTD = re.compile(rb"<!\s*(doctype|entity)", re.I)
_CELL_REF = re.compile(r"([A-Z]{1,3})(\d+)$")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _column_index(letters):
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _read_part(archive, name):
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART_BYTES:
        raise TableError("表格过大")
    with archive.open(info) as part:
        data = part.read(MAX_PART_BYTES + 1)
    if len(data) > MAX_PART_BYTES:
        raise TableError("表格过大")
    if _DTD.search(data):
        raise TableError("表格格式无法识别")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise TableError("表格内容已损坏") from exc


def _relationship_id(element):
    for key, value in element.attrib.items():
        if key.endswith("}id"):
            return value
    return None


def _texts(element):
    """共享字符串或行内字符串：拼接各段 <t>，跳过注音 <rPh>。"""
    parts = []
    for child in element:
        name = _local(child.tag)
        if name == "t":
            parts.append(child.text or "")
        elif name == "r":
            for grandchild in child:
                if _local(grandchild.tag) == "t":
                    parts.append(grandchild.text or "")
    return "".join(parts)


def read_xlsx(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise TableError("表格内容已损坏") from exc
    with archive:
        if len(archive.infolist()) > MAX_ZIP_ENTRIES:
            raise TableError("表格格式无法识别")
        workbook = _read_part(archive, "xl/workbook.xml")
        relations = _read_part(archive, "xl/_rels/workbook.xml.rels")
        if workbook is None or relations is None:
            raise TableError("表格格式无法识别")
        targets = {}
        for item in relations:
            target = item.get("Target") or ""
            target = target[1:] if target.startswith("/") else "xl/" + target
            targets[item.get("Id")] = target.replace("/./", "/")
        shared = []
        strings = _read_part(archive, "xl/sharedStrings.xml")
        if strings is not None:
            for item in strings:
                if _local(item.tag) == "si":
                    shared.append(_texts(item))
                    if len(shared) > MAX_SHARED_STRINGS:
                        raise TableError("表格过大")
        sheets = []
        for entry in workbook.iter():
            if _local(entry.tag) != "sheet" or entry.get("state") in ("hidden", "veryHidden"):
                continue
            target = targets.get(_relationship_id(entry))
            root = _read_part(archive, target) if target else None
            if root is None:
                continue
            sheets.append(_xlsx_sheet(entry.get("name") or "", root, shared))
            if len(sheets) >= MAX_SHEETS:
                break
    return sheets


def _xlsx_sheet(name, root, shared):
    cells, merges = {}, []
    for element in root.iter():
        tag = _local(element.tag)
        if tag == "row":
            next_col = 0
            row_number = element.get("r")
            row_index = int(row_number) - 1 if row_number and row_number.isdigit() else None
            for cell in element:
                if _local(cell.tag) != "c":
                    continue
                match = _CELL_REF.match(cell.get("r") or "")
                if match:
                    col, row = _column_index(match.group(1)), int(match.group(2)) - 1
                else:
                    col, row = next_col, row_index if row_index is not None else 0
                next_col = col + 1
                if row >= MAX_ROWS or col >= MAX_COLUMNS:
                    continue
                kind = cell.get("t") or "n"
                value_node = next((child for child in cell if _local(child.tag) == "v"), None)
                raw = value_node.text if value_node is not None else None
                if kind == "s":
                    try:
                        value = shared[int(raw)]
                    except (TypeError, ValueError, IndexError):
                        value = ""
                elif kind == "inlineStr":
                    holder = next((child for child in cell if _local(child.tag) == "is"), None)
                    value = _texts(holder) if holder is not None else ""
                elif kind == "str":
                    value = raw or ""
                elif kind == "b":
                    value = "TRUE" if raw == "1" else "FALSE"
                elif kind == "e":
                    value = ""
                else:
                    value = _number_text(raw) if raw is not None else ""
                if value != "":
                    cells[(row, col)] = value
        elif tag == "mergeCell":
            parts = (element.get("ref") or "").split(":")
            first, last = _CELL_REF.match(parts[0]), _CELL_REF.match(parts[-1])
            if first and last:
                merges.append((int(first.group(2)) - 1, _column_index(first.group(1)),
                               int(last.group(2)) - 1, _column_index(last.group(1))))
    return Sheet(name, _dense(cells), merges)


# ---------------------------------------------------------------------------
# xls（BIFF8，复合文档）
# ---------------------------------------------------------------------------
_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_END_OF_CHAIN = 0xFFFFFFFE
_FREE_SECTOR = 0xFFFFFFFF


def _cfb_stream(data):
    """从复合文档中取出 Workbook 流。"""
    if len(data) < 512 or data[:8] != _CFB_MAGIC:
        raise TableError("表格格式无法识别")
    sector_shift, mini_shift = struct.unpack_from("<HH", data, 30)
    if sector_shift not in (9, 12) or mini_shift != 6:
        raise TableError("表格格式无法识别")
    size = 1 << sector_shift
    mini_size = 1 << mini_shift
    fat_count, first_directory = struct.unpack_from("<I", data, 44)[0], struct.unpack_from("<I", data, 48)[0]
    mini_cutoff = struct.unpack_from("<I", data, 56)[0]
    first_mini_fat, _mini_fat_count, first_difat, difat_count = struct.unpack_from("<IIII", data, 60)
    sector_total = (len(data) - size) // size
    per_sector = size // 4

    def sector(sid):
        if sid >= sector_total:
            raise TableError("表格内容已损坏")
        offset = (sid + 1) * size
        return data[offset:offset + size]

    difat = list(struct.unpack_from("<109I", data, 76))
    sid, seen = first_difat, set()
    for _ in range(difat_count):
        if sid in (_END_OF_CHAIN, _FREE_SECTOR):
            break
        if sid in seen:
            raise TableError("表格内容已损坏")
        seen.add(sid)
        entries = struct.unpack("<%dI" % per_sector, sector(sid))
        difat.extend(entries[:-1])
        sid = entries[-1]
    fat = []
    for fat_sector in [value for value in difat if value not in (_END_OF_CHAIN, _FREE_SECTOR)][:fat_count]:
        fat.extend(struct.unpack("<%dI" % per_sector, sector(fat_sector)))

    def chain(start, table):
        result, current, visited = [], start, set()
        while current not in (_END_OF_CHAIN, _FREE_SECTOR):
            if current >= len(table) or current in visited:
                raise TableError("表格内容已损坏")
            visited.add(current)
            result.append(current)
            current = table[current]
        return result

    def read(start):
        return b"".join(sector(value) for value in chain(start, fat))

    directory = read(first_directory)
    entries = []
    for offset in range(0, len(directory) - 127, 128):
        length = struct.unpack_from("<H", directory, offset + 64)[0]
        kind = directory[offset + 66]
        start, stream_size = struct.unpack_from("<II", directory, offset + 116)
        name = directory[offset:offset + max(0, min(length, 64) - 2)].decode("utf-16-le", "replace")
        entries.append((name, kind, start, stream_size))
    if not entries or entries[0][1] != 5:
        raise TableError("表格内容已损坏")
    wanted = {"workbook": "biff8", "book": "biff5"}
    target = next((entry for entry in entries if entry[1] == 2 and entry[0].lower() in wanted), None)
    if target is None:
        raise TableError("表格格式无法识别")
    if wanted[target[0].lower()] != "biff8":
        raise TableError("请把表格另存为 xlsx 后上传")
    _name, _kind, start, stream_size = target
    if stream_size > MAX_STREAM_BYTES:
        raise TableError("表格过大")
    if stream_size >= mini_cutoff:
        stream = read(start)
    else:
        mini_stream = read(entries[0][2])
        mini_fat = []
        for value in chain(first_mini_fat, fat):
            mini_fat.extend(struct.unpack("<%dI" % per_sector, sector(value)))
        stream = b"".join(mini_stream[value * mini_size:(value + 1) * mini_size] for value in chain(start, mini_fat))
    if len(stream) < stream_size:
        raise TableError("表格内容已损坏")
    return stream[:stream_size]


def _records(stream, position=0):
    while position + 4 <= len(stream):
        kind, length = struct.unpack_from("<HH", stream, position)
        yield kind, stream[position + 4:position + 4 + length]
        position += 4 + length


class _Segments:
    """SST 记录及其 CONTINUE 续记录；字符数组跨记录时，续记录开头一个字节给出新的编码宽度。"""

    def __init__(self, parts):
        self.parts = parts
        self.index = 0
        self.position = 0

    def take(self, count):
        out = bytearray()
        while count > 0:
            while self.index < len(self.parts) and self.position >= len(self.parts[self.index]):
                self.index += 1
                self.position = 0
            if self.index >= len(self.parts):
                raise TableError("表格内容已损坏")
            chunk = self.parts[self.index][self.position:self.position + count]
            out += chunk
            self.position += len(chunk)
            count -= len(chunk)
        return bytes(out)

    def number(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def characters(self, count, wide):
        out = []
        while count > 0:
            if self.index >= len(self.parts):
                raise TableError("表格内容已损坏")
            part = self.parts[self.index]
            width = 2 if wide else 1
            available = (len(part) - self.position) // width
            if available <= 0:
                self.index += 1
                self.position = 0
                if self.index >= len(self.parts) or not self.parts[self.index]:
                    raise TableError("表格内容已损坏")
                wide = self.parts[self.index][0] & 1
                self.position = 1
                continue
            take = min(available, count)
            raw = part[self.position:self.position + take * width]
            out.append(raw.decode("utf-16-le" if wide else "latin-1"))
            self.position += take * width
            count -= take
        return "".join(out)

    def rich_string(self):
        length = self.number("<H")
        flags = self.number("<B")
        runs = self.number("<H") if flags & 0x08 else 0
        extended = self.number("<I") if flags & 0x04 else 0
        if extended > MAX_STREAM_BYTES:
            raise TableError("表格内容已损坏")
        text = self.characters(length, flags & 0x01)
        if runs:
            self.take(4 * runs)
        if extended:
            self.take(extended)
        return text


def _unicode_string(body, offset, short=False):
    if short:
        length, flags = body[offset], body[offset + 1]
        offset += 2
    else:
        length, flags = struct.unpack_from("<HB", body, offset)
        offset += 3
    width = 2 if flags & 0x01 else 1
    raw = body[offset:offset + length * width]
    return raw.decode("utf-16-le" if width == 2 else "latin-1", "replace")


def _rk_number(value):
    if value & 0x02:
        number = value >> 2
    else:
        number = struct.unpack("<d", struct.pack("<Q", ((value & 0xFFFFFFFC) & 0xFFFFFFFF) << 32))[0]
    if value & 0x01:
        number /= 100
    return number


def read_xls(data):
    stream = _cfb_stream(data)
    shared, boundsheets, sst_parts, collecting = [], [], None, False
    for kind, body in _records(stream):
        if collecting and kind == 0x003C:
            sst_parts.append(body)
            continue
        if collecting:
            collecting = False
        if kind == 0x0809 and len(body) >= 2 and struct.unpack_from("<H", body)[0] != 0x0600:
            raise TableError("请把表格另存为 xlsx 后上传")
        elif kind == 0x0085 and len(body) >= 8:
            position, state, sheet_type = struct.unpack_from("<IBB", body)
            if state == 0 and sheet_type == 0:
                boundsheets.append((_unicode_string(body, 6, short=True), position))
        elif kind == 0x00FC:
            sst_parts, collecting = [body], True
        elif kind == 0x000A:
            break
    if sst_parts:
        segments = _Segments(sst_parts)
        segments.take(4)
        unique = segments.number("<I")
        for _ in range(min(unique, MAX_SHARED_STRINGS)):
            shared.append(segments.rich_string())
    sheets = []
    for name, position in boundsheets[:MAX_SHEETS]:
        if position >= len(stream):
            continue
        sheets.append(_xls_sheet(name, stream, position, shared))
    return sheets


def _xls_sheet(name, stream, position, shared):
    cells, merges, pending = {}, [], None

    def put(row, col, value):
        if row < MAX_ROWS and col < MAX_COLUMNS and value != "":
            cells[(row, col)] = value

    for kind, body in _records(stream, position):
        if kind == 0x000A:
            break
        if kind == 0x00FD and len(body) >= 10:
            row, col, _xf, index = struct.unpack_from("<HHHI", body)
            put(row, col, shared[index] if index < len(shared) else "")
        elif kind == 0x0203 and len(body) >= 14:
            row, col = struct.unpack_from("<HH", body)
            put(row, col, _number_text(struct.unpack_from("<d", body, 6)[0]))
        elif kind == 0x027E and len(body) >= 10:
            row, col, _xf, value = struct.unpack_from("<HHHi", body)
            put(row, col, _number_text(_rk_number(value)))
        elif kind == 0x00BD and len(body) >= 6:
            row, first = struct.unpack_from("<HH", body)
            for index in range((len(body) - 6) // 6):
                _xf, value = struct.unpack_from("<Hi", body, 4 + 6 * index)
                put(row, first + index, _number_text(_rk_number(value)))
        elif kind == 0x0204 and len(body) >= 9:
            row, col = struct.unpack_from("<HH", body)
            put(row, col, _unicode_string(body, 6))
        elif kind == 0x0006 and len(body) >= 20:
            row, col = struct.unpack_from("<HH", body)
            result = body[6:14]
            if result[6:8] == b"\xff\xff":
                if result[0] == 0:
                    pending = (row, col)
                elif result[0] == 1:
                    put(row, col, "TRUE" if result[2] else "FALSE")
            else:
                put(row, col, _number_text(struct.unpack("<d", result)[0]))
        elif kind == 0x0207 and pending and len(body) >= 3:
            put(pending[0], pending[1], _unicode_string(body, 0))
            pending = None
        elif kind == 0x00E5 and len(body) >= 2:
            count = struct.unpack_from("<H", body)[0]
            for index in range(min(count, (len(body) - 2) // 8)):
                first_row, last_row, first_col, last_col = struct.unpack_from("<HHHH", body, 2 + 8 * index)
                merges.append((first_row, first_col, last_row, last_col))
    return Sheet(name, _dense(cells), merges)


# ---------------------------------------------------------------------------
# 文本：csv、网页表格、XML 表格 2003
# ---------------------------------------------------------------------------
def decode_text(data):
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", "replace")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", "replace")
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def read_csv(text):
    lines = text.splitlines()[:20]
    delimiter, best = None, 0
    for candidate in (",", "\t", ";", "，", "|"):
        counts = [line.count(candidate) for line in lines if line.strip()]
        score = min(counts) if counts and min(counts) > 0 else (max(counts) if counts else 0) / 10
        if score > best:
            delimiter, best = candidate, score
    rows = []
    if delimiter:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        for row in reader:
            rows.append([cell[:MAX_CELL] for cell in row[:MAX_COLUMNS]])
            if len(rows) >= MAX_ROWS:
                break
    else:
        rows = [[line[:MAX_CELL]] for line in text.splitlines()[:MAX_ROWS]]
    return [Sheet("", rows)]


class _HtmlTables(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sheets, self.rows, self.row, self.cell, self.span = [], None, None, None, 1

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.rows = []
        elif tag == "tr" and self.rows is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
            span = dict(attrs).get("colspan") or "1"
            self.span = int(span) if span.isdigit() and 0 < int(span) <= MAX_COLUMNS else 1
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip()[:MAX_CELL])
            self.row.extend([""] * (self.span - 1))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.rows is not None:
            if len(self.rows) < MAX_ROWS:
                self.rows.append(self.row[:MAX_COLUMNS])
            self.row = None
        elif tag == "table" and self.rows is not None:
            if self.rows and len(self.sheets) < MAX_SHEETS:
                self.sheets.append(Sheet("", self.rows))
            self.rows = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def read_markup(text):
    head = text.lstrip()[:2000].lower()
    if "urn:schemas-microsoft-com:office:spreadsheet" in head or "<workbook" in head:
        return _read_spreadsheet_2003(text)
    parser = _HtmlTables()
    parser.feed(text)
    parser.close()
    if not parser.sheets:
        raise TableError("表格格式无法识别")
    return parser.sheets


def _read_spreadsheet_2003(text):
    data = text.encode("utf-8")
    if _DTD.search(data):
        raise TableError("表格格式无法识别")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise TableError("表格内容已损坏") from exc
    sheets = []
    for worksheet in root.iter():
        if _local(worksheet.tag) != "Worksheet":
            continue
        name = next((value for key, value in worksheet.attrib.items() if key.endswith("Name")), "")
        cells, merges, row_index = {}, [], -1
        for row in worksheet.iter():
            if _local(row.tag) != "Row":
                continue
            row_index = _index_attribute(row, row_index + 1)
            col_index = -1
            for cell in row:
                if _local(cell.tag) != "Cell":
                    continue
                col_index = _index_attribute(cell, col_index + 1)
                value = "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "Data")
                across = _int_attribute(cell, "MergeAcross")
                down = _int_attribute(cell, "MergeDown")
                if value and row_index < MAX_ROWS and col_index < MAX_COLUMNS:
                    cells[(row_index, col_index)] = value[:MAX_CELL]
                if across or down:
                    merges.append((row_index, col_index, row_index + down, col_index + across))
                col_index += across
        sheets.append(Sheet(name, _dense(cells), merges))
        if len(sheets) >= MAX_SHEETS:
            break
    return sheets


def _int_attribute(element, suffix):
    for key, value in element.attrib.items():
        if key.endswith(suffix) and value.isdigit():
            return int(value)
    return 0


def _index_attribute(element, default):
    value = _int_attribute(element, "Index")
    return value - 1 if value > 0 else default


def read_table(filename, data):
    """按文件内容识别格式；扩展名只在内容无法判断时参考。"""
    if data.startswith(b"PK\x03\x04"):
        return read_xlsx(data)
    if data.startswith(_CFB_MAGIC):
        return read_xls(data)
    text = decode_text(data)
    if text.lstrip().startswith("<"):
        return read_markup(text)
    if (filename or "").lower().endswith((".xlsx", ".xls", ".et", ".numbers")):
        raise TableError("表格格式无法识别")
    return read_csv(text)


# ---------------------------------------------------------------------------
# 名单：定位表头、规整姓名与班级、逐行标注
# ---------------------------------------------------------------------------
NAME_HEADERS = {"姓名", "学生姓名", "名字", "学生名字", "学生", "name", "studentname", "fullname"}
CLASS_HEADERS = {"班级", "班别", "行政班", "所在班级", "班级名称", "班级名", "教学班", "class", "classname"}
GRADE_HEADERS = {"年级", "grade"}
NUMBER_HEADERS = {"班号", "班级号", "班序号", "班级序号", "班"}
SUMMARY_NAMES = {"合计", "小计", "总计", "共计", "总人数", "人数"}
_INVISIBLE = re.compile("[\u0000-\u001f\u007f­​-‏ - ⁠-⁯﻿]")
_DOTS = re.compile("[•‧・·ˑ·∙⋅⸱]")
_CJK = "㐀-鿿豈-﫿"
_CLASS_TITLE = re.compile(r"^(?:高|初|小)?[一二三四五六七八九\d]*(?:年级)?\(?\d+\)?班$")


def header_key(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[(\[【].*?[)\]】]", "", text)
    text = re.sub(r"[\s*:#.\-_/]", "", text)
    return text.lower()


def clean_name(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = _INVISIBLE.sub("", text)
    text = _DOTS.sub("·", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<=[{0}·])\s+(?=[{0}·])".format(_CJK), "", text)
    return text[:40]


def clean_class(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = _INVISIBLE.sub("", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"\((\d+)\)", r"\1", text)
    text = re.sub(r"^(高|初)([一二三])年级(\d+)班$", r"\1\2\3班", text)
    if re.fullmatch(r"\d+", text):
        text += "班"
    return text[:40]


def _join_grade(grade, number):
    grade = clean_class(grade).replace("年级", "")
    number = clean_class(number)
    if not number:
        return ""
    if grade and not number.startswith(grade):
        return grade + number
    return number


def _locate_header(rows):
    for index, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        keys = [header_key(cell) for cell in row]
        name_col = next((i for i, key in enumerate(keys) if key in NAME_HEADERS), None)
        if name_col is None:
            continue
        grade_col = next((i for i, key in enumerate(keys) if key in GRADE_HEADERS), None)
        class_col = next((i for i, key in enumerate(keys)
                          if key in CLASS_HEADERS or (key == "班" and grade_col is None)), None)
        number_col = None
        if class_col is None and grade_col is not None:
            number_col = next((i for i, key in enumerate(keys) if key in NUMBER_HEADERS), None)
        elif class_col is not None and grade_col is not None:
            number_col = class_col
            class_col = None
        return index, name_col, class_col, grade_col, number_col
    return None


def _merged_source(merges, row, col):
    for first_row, first_col, last_row, last_col in merges:
        if first_row <= row <= last_row and first_col <= col <= last_col and (row, col) != (first_row, first_col):
            return first_row, first_col
    return None


def _cell(rows, row, col):
    if col is None or row >= len(rows) or col >= len(rows[row]):
        return ""
    return rows[row][col]


def parse_roster(filename, data):
    """返回 {"rows": [...], "sheets": [...], "skipped": n}；每行含 sheet、line、name、class、notes。"""
    sheets = read_table(filename, data)
    result, sheet_infos, skipped = [], [], 0
    for sheet in sheets:
        rows = sheet.rows
        located = _locate_header(rows)
        if located is None:
            sheet_infos.append({"name": sheet.name, "header": None, "rows": 0})
            continue
        header, name_col, class_col, grade_col, number_col = located
        sheet_class = clean_class(sheet.name) if _CLASS_TITLE.match(clean_class(sheet.name)) else ""
        context, previous, count = "", "", 0
        for index in range(header + 1, len(rows)):
            row = rows[index]
            values = [value.strip() for value in row if value and value.strip()]
            if not values:
                continue
            raw_name = _cell(rows, index, name_col)
            merged = _merged_source(sheet.merges, index, name_col)
            if not raw_name and merged:
                continue
            if header_key(raw_name) in NAME_HEADERS:
                continue
            if len(values) == 1 and _CLASS_TITLE.match(clean_class(values[0])):
                context = clean_class(values[0])
                continue
            name = clean_name(raw_name)
            if not name or name in SUMMARY_NAMES or name.startswith(("注", "备注", "说明")):
                skipped += 1
                continue
            notes = []
            if class_col is not None:
                klass = clean_class(_cell(rows, index, class_col))
                if not klass:
                    source = _merged_source(sheet.merges, index, class_col)
                    if source:
                        klass = clean_class(_cell(rows, source[0], source[1]))
            elif number_col is not None:
                klass = _join_grade(_cell(rows, index, grade_col), _cell(rows, index, number_col))
            else:
                klass = ""
            if not klass and context:
                klass = context
            if not klass and sheet_class:
                klass = sheet_class
                notes.append("班级取自工作表名")
            if not klass and previous and class_col is not None:
                klass = previous
                notes.append("班级沿用上一行")
            if not klass:
                notes.append("缺少班级")
            if not re.search("[{}A-Za-z]".format(_CJK), name) or re.fullmatch(r"[\d\s.]+", name):
                notes.append("姓名有误")
            previous = klass or previous
            result.append({"sheet": sheet.name, "line": index + 1, "name": name, "class": klass, "notes": notes})
            count += 1
        sheet_infos.append({"name": sheet.name, "header": header + 1, "rows": count})
    if not any(info["header"] for info in sheet_infos):
        raise TableError("表头需包含“姓名”和“班级”")
    seen = {}
    for item in result:
        key = (item["name"], item["class"])
        seen[key] = seen.get(key, 0) + 1
    for item in result:
        if item["class"] and seen[(item["name"], item["class"])] > 1:
            item["notes"].append("同班同名")
    return {"rows": result, "sheets": sheet_infos, "skipped": skipped}


# ---------------------------------------------------------------------------
# 写 xlsx
# ---------------------------------------------------------------------------
_SHEET_NAME_BAD = re.compile(r"[\\/?*\[\]:]")
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def column_letter(index):
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def sheet_names(titles):
    names, used = [], set()
    for title in titles:
        base = _SHEET_NAME_BAD.sub(" ", str(title)).strip()[:28] or "Sheet"
        name, n = base, 2
        while name in used:
            name, n = "{} ({})".format(base, n), n + 1
        used.add(name)
        names.append(name)
    return names


def _text_cell(ref, value, style):
    text = xml_escape("" if value is None else str(value))
    return '<c r="{}" t="inlineStr"{}><is><t xml:space="preserve">{}</t></is></c>'.format(
        ref, ' s="{}"'.format(style) if style else "", text)


def _package(names, sheet_xmls, styles, defined_names=""):
    overrides = "".join(
        '<Override PartName="/xl/worksheets/sheet{}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(i + 1)
        for i in range(len(sheet_xmls)))
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + overrides + "</Types>")
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="xl/workbook.xml"/></Relationships>')
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="{}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'.format(_MAIN_NS)
        + "".join('<sheet name="{}" sheetId="{}" r:id="rId{}"/>'.format(xml_escape(name, {'"': "&quot;"}), i + 1, i + 1)
                  for i, name in enumerate(names))
        + "</sheets>" + defined_names + "</workbook>")
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join('<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                  'relationships/worksheet" Target="worksheets/sheet{0}.xml"/>'.format(i + 1)
                  for i in range(len(sheet_xmls)))
        + '<Relationship Id="rId{}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
          'styles" Target="styles.xml"/></Relationships>'.format(len(sheet_xmls) + 1))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        for i, sheet_xml in enumerate(sheet_xmls):
            archive.writestr("xl/worksheets/sheet{}.xml".format(i + 1), sheet_xml)
    return out.getvalue()


_PLAIN_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="{}">'
    '<fonts count="2"><font><sz val="11"/><name val="宋体"/></font>'
    '<font><b/><sz val="11"/><name val="宋体"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>').format(_MAIN_NS)


def write_xlsx(sheets):
    """sheets：[(表名, 表头, 行, 列宽)]。单元格一律写成文本，前导零原样保留。"""
    names = sheet_names(title for title, *_rest in sheets)
    xmls = []
    for _title, header, rows, widths in sheets:
        cols = "".join('<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(i + 1, w)
                       for i, w in enumerate(widths))
        lines = ['<row r="1">' + "".join(_text_cell(column_letter(i) + "1", v, 1) for i, v in enumerate(header)) + "</row>"]
        for r, row in enumerate(rows, start=2):
            lines.append('<row r="{}">'.format(r) + "".join(
                _text_cell(column_letter(i) + str(r), v, 0) for i, v in enumerate(row)) + "</row>")
        xmls.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="{}">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
            'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            '<cols>{}</cols><sheetData>{}</sheetData></worksheet>'.format(_MAIN_NS, cols, "".join(lines)))
    return _package(names, xmls, _PLAIN_STYLES)


def natural_key(text):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text or "")]


_ACCOUNT_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="{}">'
    '<fonts count="3"><font><sz val="11"/><name val="宋体"/></font>'
    '<font><b/><sz val="14"/><name val="宋体"/></font>'
    '<font><sz val="14"/><name val="宋体"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
    '<border><left style="thin"><color rgb="FF808080"/></left><right style="thin"><color rgb="FF808080"/></right>'
    '<top style="thin"><color rgb="FF808080"/></top><bottom style="thin"><color rgb="FF808080"/></bottom>'
    '<diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">'
    '<alignment vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">'
    '<alignment vertical="center"/></xf></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>').format(_MAIN_NS)


def accounts_xlsx(accounts):
    """账号表：姓名、班级、账号、密码；按班级排序，每个班级从新的一页开始，每页重复表头。"""
    ordered = sorted(enumerate(accounts), key=lambda item: (natural_key(item[1].get("class")), item[0]))
    header = ["姓名", "班级", "账号", "密码"]
    lines = ['<row r="1" ht="32" customHeight="1">'
             + "".join(_text_cell(column_letter(i) + "1", v, 1) for i, v in enumerate(header)) + "</row>"]
    breaks, previous = [], None
    for number, (_index, account) in enumerate(ordered, start=2):
        klass = account.get("class") or ""
        if previous is not None and klass != previous:
            breaks.append(number - 1)
        previous = klass
        values = [account.get("name"), klass, account.get("username"), account.get("password")]
        lines.append('<row r="{}" ht="32" customHeight="1">'.format(number)
                     + "".join(_text_cell(column_letter(i) + str(number), v, 2) for i, v in enumerate(values))
                     + "</row>")
    last = len(ordered) + 1
    cols = "".join('<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(i + 1, w)
                   for i, w in enumerate([16, 14, 20, 16]))
    row_breaks = ""
    if breaks:
        row_breaks = '<rowBreaks count="{0}" manualBreakCount="{0}">{1}</rowBreaks>'.format(
            len(breaks), "".join('<brk id="{}" max="16383" man="1"/>'.format(row) for row in breaks))
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="{ns}"><sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
        '<dimension ref="A1:D{last}"/>'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="32" customHeight="1"/>'
        '<cols>{cols}</cols><sheetData>{rows}</sheetData>'
        '<printOptions horizontalCentered="1"/>'
        '<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>'
        '<pageSetup paperSize="9" orientation="portrait" fitToWidth="1" fitToHeight="0"/>'
        '{breaks}</worksheet>').format(ns=_MAIN_NS, last=last, cols=cols, rows="".join(lines), breaks=row_breaks)
    defined = ('<definedNames><definedName name="_xlnm.Print_Titles" localSheetId="0">'
               "'账号'!$1:$1</definedName></definedNames>")
    return _package(["账号"], [sheet], _ACCOUNT_STYLES, defined)
