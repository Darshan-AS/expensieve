"""Pure-Python PDF text extractor supporting FlateDecode streams, ToUnicode CMaps, and 2D layout sorting."""

import re
import zlib


def _parse_tounicode_cmap(dec: str) -> dict[int, str]:
    """Parse a PDF ToUnicode CMap stream into a glyph-ID -> Unicode string dictionary."""
    cmap: dict[int, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", dec, re.DOTALL):
        for cm in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            src = int(cm.group(1), 16)
            cmap[src] = bytes.fromhex(cm.group(2)).decode("utf-16-be", errors="replace")

    for block in re.findall(r"beginbfrange(.*?)endbfrange", dec, re.DOTALL):
        for line in block.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            rm_arr = re.match(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", line)
            if rm_arr:
                start = int(rm_arr.group(1), 16)
                vals = re.findall(r"<([0-9A-Fa-f]+)>", rm_arr.group(3))
                for idx, val in enumerate(vals):
                    cmap[start + idx] = bytes.fromhex(val).decode("utf-16-be", errors="replace")
                continue
            rm = re.match(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", line)
            if rm:
                start = int(rm.group(1), 16)
                end = int(rm.group(2), 16)
                base = int(rm.group(3), 16)
                for cid in range(start, end + 1):
                    cmap[cid] = chr(base + (cid - start))
    return cmap


def extract_pdf_lines(data: bytes) -> list[str]:
    """Extract text lines from a PDF byte payload sorted by page and top-to-bottom Y coordinate."""
    obj_streams: dict[int, str] = {}
    obj_dicts: dict[int, str] = {}

    for match in re.finditer(rb"(?:^|[\r\n])(\d+)\s+0\s+obj\b", data):
        oid = int(match.group(1))
        end_idx = data.find(b"endobj", match.end())
        if end_idx == -1:
            continue
        body = data[match.end() : end_idx]
        s_pos = body.find(b"stream")
        e_pos = body.rfind(b"endstream")
        if s_pos != -1 and e_pos != -1 and e_pos > s_pos:
            raw_s = body[s_pos + 6 : e_pos]
            if raw_s.startswith(b"\r\n"):
                raw_s = raw_s[2:]
            elif raw_s.startswith((b"\n", b"\r")):
                raw_s = raw_s[1:]
            try:
                obj_streams[oid] = zlib.decompress(raw_s.rstrip(b"\r\n")).decode("latin1", errors="replace")
            except zlib.error:
                try:
                    obj_streams[oid] = zlib.decompress(raw_s).decode("latin1", errors="replace")
                except zlib.error:
                    obj_streams[oid] = raw_s.decode("latin1", errors="replace")
            obj_dicts[oid] = body[:s_pos].decode("latin1", errors="replace")
        else:
            obj_dicts[oid] = body.decode("latin1", errors="replace")

    font_obj_cmaps: dict[int, dict[int, str]] = {}
    for oid, obj_dict in obj_dicts.items():
        tu = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj_dict)
        if tu:
            tu_id = int(tu.group(1))
            if tu_id in obj_streams:
                font_obj_cmaps[oid] = _parse_tounicode_cmap(obj_streams[tu_id])

    font_name_cmaps: dict[str, dict[int, str]] = {}
    for text_blob in (*obj_dicts.values(), *obj_streams.values()):
        for fm in re.finditer(r"/(F\d+)\s+(\d+)\s+0\s+R", text_blob):
            fname, foid = fm.group(1), int(fm.group(2))
            if foid in font_obj_cmaps:
                font_name_cmaps[fname] = font_obj_cmaps[foid]

    fallback_cmap: dict[int, str] = {}
    for cm in font_obj_cmaps.values():
        fallback_cmap.update(cm)
    for stream_text in obj_streams.values():
        if "begincmap" in stream_text and not fallback_cmap:
            fallback_cmap.update(_parse_tounicode_cmap(stream_text))

    blocks: list[tuple[int, float, float, str]] = []
    page_idx = 0
    for _oid, dec in sorted(obj_streams.items()):
        if "begincmap" in dec or "BT" not in dec:
            continue
        page_idx += 1
        cur_cmap = fallback_cmap
        for bt_block in re.findall(r"BT(.*?)ET", dec, re.DOTALL):
            x_coord, y_coord = 0.0, 0.0
            tm = re.search(
                r"1\s+0\s+0\s+-1\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+Tm",
                bt_block,
            )
            if tm:
                x_coord, y_coord = float(tm.group(1)), float(tm.group(2))
            chars: list[str] = []
            first_td = True
            token_pattern = (
                r"/(F\d+)\s+[\d.]+\s+Tf|"
                r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+Td|"
                r"<([0-9A-Fa-f]+)>\s*Tj|"
                r"\(([^()]*)\)\s*Tj"
            )
            for tok in re.finditer(token_pattern, bt_block):
                if tok.group(1):
                    cur_cmap = font_name_cmaps.get(tok.group(1), fallback_cmap)
                elif tok.group(2) is not None:
                    dx, dy = float(tok.group(2)), float(tok.group(3))
                    if first_td and y_coord == 0.0:
                        x_coord, y_coord = dx, abs(dy)
                        first_td = False
                elif tok.group(4):
                    hex_str = tok.group(4)
                    for i in range(0, len(hex_str), 4):
                        cid = int(hex_str[i : i + 4], 16)
                        chars.append(cur_cmap.get(cid, ""))
                elif tok.group(5) is not None:
                    chars.append(tok.group(5))
            txt = "".join(chars).replace("\u200b", "").strip()
            if txt:
                blocks.append((page_idx, round(y_coord, 1), round(x_coord, 1), txt))

    blocks.sort(key=lambda b: (b[0], b[1], b[2]))
    return [b[3] for b in blocks]


def extract_pdf_text(data: bytes) -> str:
    """Extract newline-delimited sorted text from a PDF byte payload."""
    return "\n".join(extract_pdf_lines(data))
