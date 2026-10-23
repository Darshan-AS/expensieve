"""Pure-Python PDF text extractor supporting RC4 and AES-128 (/AESV2) Standard Security decryption, FlateDecode streams, ToUnicode CMaps, and 2D layout sorting."""

import hashlib
import re
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass

PDF_PADDING: bytes = bytes.fromhex("28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a")


def _gf_mul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _init_aes_tables() -> tuple[
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
]:
    sbox = [0] * 256
    inv_sbox = [0] * 256
    p, q = 1, 1
    while True:
        p = (p ^ (p << 1) ^ (0x11B if p & 0x80 else 0)) & 0xFF
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q = (q ^ (0x09 if q & 0x80 else 0)) & 0xFF
        xformed = (
            q
            ^ ((q << 1) | (q >> 7))
            ^ ((q << 2) | (q >> 6))
            ^ ((q << 3) | (q >> 5))
            ^ ((q << 4) | (q >> 4))
            ^ 0x63
        )
        sbox[p] = xformed & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    for idx, val in enumerate(sbox):
        inv_sbox[val] = idx
    return (
        tuple(sbox),
        tuple(inv_sbox),
        tuple(_gf_mul(x, 9) for x in range(256)),
        tuple(_gf_mul(x, 11) for x in range(256)),
        tuple(_gf_mul(x, 13) for x in range(256)),
        tuple(_gf_mul(x, 14) for x in range(256)),
    )


_AES_SBOX, _AES_INV_SBOX, _AES_MUL9, _AES_MUL11, _AES_MUL13, _AES_MUL14 = _init_aes_tables()
_AES_RCON: tuple[int, ...] = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


@dataclass(frozen=True, slots=True)
class PdfEncryptInfo:
    """Parsed PDF Standard Security Handler (`/Encrypt`) dictionary parameters."""

    encrypt_obj_id: int
    owner_hash: bytes
    user_hash: bytes
    permissions: int
    version: int
    revision: int
    key_length_bits: int
    file_id_0: bytes
    is_aes: bool = False


def rc4_crypt(key: bytes, data: bytes) -> bytes:
    """Pure-Python RC4 stream cipher for PDF Standard Security Handler decryption."""
    if not key or not data:
        return data
    s = list(range(256))
    j = 0
    klen = len(key)
    for i in range(256):
        j = (j + s[i] + key[i % klen]) & 0xFF
        s[i], s[j] = s[j], s[i]
    i = 0
    j = 0
    out = bytearray(len(data))
    for idx, byte_val in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[idx] = byte_val ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def aes128_cbc_decrypt(key: bytes, data: bytes) -> bytes:
    """Pure-Python AES-128-CBC decryptor (first 16 bytes IV + PKCS#7 padding) for PDF `/AESV2` streams."""
    if len(key) != 16 or len(data) < 32 or len(data) % 16 != 0:
        return data
    rk = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(rk[i - 1])
        if i % 4 == 0:
            temp = [
                _AES_SBOX[temp[1]] ^ _AES_RCON[i // 4 - 1],
                _AES_SBOX[temp[2]],
                _AES_SBOX[temp[3]],
                _AES_SBOX[temp[0]],
            ]
        rk.append([rk[i - 4][j] ^ temp[j] for j in range(4)])
    round_keys = [[rk[r * 4 + c][r_idx] for c in range(4) for r_idx in range(4)] for r in range(11)]

    out = bytearray()
    prev = data[:16]
    for blk_off in range(16, len(data), 16):
        cur = data[blk_off : blk_off + 16]
        s = [cur[i] ^ round_keys[10][i] for i in range(16)]
        for rnd in range(9, 0, -1):
            s = [
                _AES_INV_SBOX[s[0]],
                _AES_INV_SBOX[s[13]],
                _AES_INV_SBOX[s[10]],
                _AES_INV_SBOX[s[7]],
                _AES_INV_SBOX[s[4]],
                _AES_INV_SBOX[s[1]],
                _AES_INV_SBOX[s[14]],
                _AES_INV_SBOX[s[11]],
                _AES_INV_SBOX[s[8]],
                _AES_INV_SBOX[s[5]],
                _AES_INV_SBOX[s[2]],
                _AES_INV_SBOX[s[15]],
                _AES_INV_SBOX[s[12]],
                _AES_INV_SBOX[s[9]],
                _AES_INV_SBOX[s[6]],
                _AES_INV_SBOX[s[3]],
            ]
            rk_r = round_keys[rnd]
            s = [s[i] ^ rk_r[i] for i in range(16)]
            ns = [0] * 16
            for c in range(4):
                b0, b1, b2, b3 = s[c * 4], s[c * 4 + 1], s[c * 4 + 2], s[c * 4 + 3]
                ns[c * 4] = _AES_MUL14[b0] ^ _AES_MUL11[b1] ^ _AES_MUL13[b2] ^ _AES_MUL9[b3]
                ns[c * 4 + 1] = _AES_MUL9[b0] ^ _AES_MUL14[b1] ^ _AES_MUL11[b2] ^ _AES_MUL13[b3]
                ns[c * 4 + 2] = _AES_MUL13[b0] ^ _AES_MUL9[b1] ^ _AES_MUL14[b2] ^ _AES_MUL11[b3]
                ns[c * 4 + 3] = _AES_MUL11[b0] ^ _AES_MUL13[b1] ^ _AES_MUL9[b2] ^ _AES_MUL14[b3]
            s = ns
        s = [
            _AES_INV_SBOX[s[0]],
            _AES_INV_SBOX[s[13]],
            _AES_INV_SBOX[s[10]],
            _AES_INV_SBOX[s[7]],
            _AES_INV_SBOX[s[4]],
            _AES_INV_SBOX[s[1]],
            _AES_INV_SBOX[s[14]],
            _AES_INV_SBOX[s[11]],
            _AES_INV_SBOX[s[8]],
            _AES_INV_SBOX[s[5]],
            _AES_INV_SBOX[s[2]],
            _AES_INV_SBOX[s[15]],
            _AES_INV_SBOX[s[12]],
            _AES_INV_SBOX[s[9]],
            _AES_INV_SBOX[s[6]],
            _AES_INV_SBOX[s[3]],
        ]
        rk0 = round_keys[0]
        for i in range(16):
            out.append(s[i] ^ rk0[i] ^ prev[i])
        prev = cur
    if out:
        pad = out[-1]
        if 1 <= pad <= 16 and all(b == pad for b in out[-pad:]):
            del out[-pad:]
    return bytes(out)


def _unescape_pdf_literal_bytes(raw: bytes) -> bytes:
    """Unescape a PDF literal string `(...)` byte sequence including octal and backslash escapes."""
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == 0x5C:  # backslash
            i += 1
            if i >= n:
                break
            esc = raw[i]
            if esc == ord("n"):
                out.append(10)
            elif esc == ord("r"):
                out.append(13)
            elif esc == ord("t"):
                out.append(9)
            elif esc == ord("b"):
                out.append(8)
            elif esc == ord("f"):
                out.append(12)
            elif esc in (ord("("), ord(")"), ord("\\")):
                out.append(esc)
            elif 48 <= esc <= 55:
                oct_s = chr(esc)
                for _ in range(2):
                    if i + 1 < n and 48 <= raw[i + 1] <= 55:
                        i += 1
                        oct_s += chr(raw[i])
                    else:
                        break
                out.append(int(oct_s, 8) & 0xFF)
            else:
                out.append(esc)
        else:
            out.append(c)
        i += 1
    return bytes(out)


def parse_pdf_encrypt_dict(data: bytes) -> PdfEncryptInfo | None:
    """Parse `/Encrypt` object and trailer `/ID` from a PDF byte payload if encrypted."""
    m_enc = re.search(rb"/Encrypt\s+(\d+)\s+0\s+R", data)
    if not m_enc:
        return None
    enc_oid = int(m_enc.group(1))
    obj_pat = rb"(?:^|[\r\n])" + str(enc_oid).encode("ascii") + rb"\s+0\s+obj\b(.*?)endobj"
    m_obj = re.search(obj_pat, data, re.DOTALL)
    if not m_obj:
        return None
    ebody = m_obj.group(1)

    def _get_bytes_field(tag: bytes) -> bytes:
        m_hex = re.search(tag + rb"\s*<([0-9A-Fa-f]+)>", ebody)
        if m_hex:
            return bytes.fromhex(m_hex.group(1).decode("ascii"))
        idx = ebody.find(tag)
        if idx == -1:
            return b""
        p_start = ebody.find(b"(", idx + len(tag))
        if p_start == -1:
            return b""
        depth = 1
        pos = p_start + 1
        while pos < len(ebody) and depth > 0:
            if ebody[pos] == 0x5C:
                pos += 2
                continue
            if ebody[pos] == ord("("):
                depth += 1
            elif ebody[pos] == ord(")"):
                depth -= 1
            pos += 1
        return _unescape_pdf_literal_bytes(ebody[p_start + 1 : pos - 1])

    m_p = re.search(rb"/P\s+(-?\d+)", ebody)
    m_v = re.search(rb"/V\s+(\d+)", ebody)
    m_r = re.search(rb"/R\s+(\d+)", ebody)
    m_len = re.search(rb"/Length\s+(\d+)", ebody)
    m_id = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>", data)
    if not (m_p and m_id):
        return None
    version = int(m_v.group(1)) if m_v else 2
    revision = int(m_r.group(1)) if m_r else 3
    default_bits = 40 if version == 1 else 128
    raw_len = int(m_len.group(1)) if m_len else default_bits
    key_length_bits = raw_len * 8 if raw_len <= 32 else raw_len
    return PdfEncryptInfo(
        encrypt_obj_id=enc_oid,
        owner_hash=_get_bytes_field(b"/O"),
        user_hash=_get_bytes_field(b"/U"),
        permissions=int(m_p.group(1)),
        version=version,
        revision=revision,
        key_length_bits=key_length_bits,
        file_id_0=bytes.fromhex(m_id.group(1).decode("ascii")),
        is_aes=b"/AESV2" in ebody,
    )


def derive_pdf_encryption_key(password: str, enc_info: PdfEncryptInfo) -> bytes | None:
    """Compute and verify a PDF Standard Security Handler user encryption key (Algorithm 2 & 5)."""
    pwd_bytes = (password.encode("latin1", errors="replace") + PDF_PADDING)[:32]
    md5 = hashlib.md5()
    md5.update(pwd_bytes)
    md5.update(enc_info.owner_hash)
    md5.update(struct.pack("<I", enc_info.permissions & 0xFFFFFFFF))
    md5.update(enc_info.file_id_0)
    digest = md5.digest()
    key_len = max(5, enc_info.key_length_bits // 8)
    if enc_info.revision >= 3:
        for _ in range(50):
            digest = hashlib.md5(digest[:key_len]).digest()
    key = digest[:key_len]

    if enc_info.revision == 2:
        check = rc4_crypt(key, PDF_PADDING)
        if check == enc_info.user_hash[:32]:
            return key
        return None

    md5_u = hashlib.md5()
    md5_u.update(PDF_PADDING)
    md5_u.update(enc_info.file_id_0)
    u_digest = md5_u.digest()
    for i in range(20):
        iter_key = bytes(b ^ i for b in key)
        u_digest = rc4_crypt(iter_key, u_digest)
    if u_digest == enc_info.user_hash[:16]:
        return key
    return None


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


def _extract_pdf_blocks(
    data: bytes,
    passwords: Sequence[str] = (),
) -> list[tuple[int, float, float, str]]:
    """Extract `(page_idx, y_coord, x_coord, text)` tuples from a PDF (decrypting if needed)."""
    enc_info = parse_pdf_encrypt_dict(data)
    decryption_key: bytes | None = None
    if enc_info is not None:
        candidate_passwords = ("", *passwords)
        for pwd in candidate_passwords:
            decryption_key = derive_pdf_encryption_key(pwd, enc_info)
            if decryption_key is not None:
                break
        if decryption_key is None:
            raise ValueError("Encrypted PDF could not be decrypted with the provided passwords.")

    obj_streams: dict[int, str] = {}
    obj_dicts: dict[int, str] = {}

    for match in re.finditer(rb"(?:^|[\r\n])(\d+)\s+(\d+)\s+obj\b", data):
        oid = int(match.group(1))
        gen = int(match.group(2))
        if enc_info is not None and oid == enc_info.encrypt_obj_id:
            continue
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
            if raw_s.endswith(b"\r\n"):
                raw_s = raw_s[:-2]
            elif raw_s.endswith((b"\n", b"\r")):
                raw_s = raw_s[:-1]

            if decryption_key is not None:
                is_aes = enc_info is not None and enc_info.is_aes
                salt = b"sAlT" if is_aes else b""
                obj_key = hashlib.md5(
                    decryption_key + struct.pack("<I", oid)[:3] + struct.pack("<I", gen)[:2] + salt
                ).digest()[: min(len(decryption_key) + 5, 16)]
                raw_s = aes128_cbc_decrypt(obj_key, raw_s) if is_aes else rc4_crypt(obj_key, raw_s)

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
        for bt_block in re.findall(r"\bBT\b(.*?)\bET\b", dec, re.DOTALL):
            x_coord, y_coord = 0.0, 0.0
            tm = re.search(
                r"1\s+0\s+0\s+-?1\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+Tm",
                bt_block,
            )
            if tm:
                x_coord, y_coord = float(tm.group(1)), abs(float(tm.group(2)))
            chars: list[str] = []
            first_td = True
            token_pattern = (
                r"/(F\d+)\s+[\d.]+\s+Tf|"
                r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+Td|"
                r"<([0-9A-Fa-f]+)>\s*Tj|"
                r"\(([^()\\]*(?:\\.[^()\\]*)*)\)\s*Tj"
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
                    unescaped = tok.group(5).replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")
                    if (
                        chars
                        and not chars[-1].endswith(" ")
                        and not unescaped.startswith(" ")
                        and bt_block.count("Tm") > 1
                    ):
                        chars.append(" ")
                    chars.append(unescaped)
            txt = "".join(chars).replace("\u200b", "").strip()
            if txt:
                blocks.append((page_idx, round(y_coord, 1), round(x_coord, 1), txt))

    blocks.sort(key=lambda b: (b[0], b[1], b[2]))
    return blocks


def extract_pdf_lines(data: bytes, passwords: Sequence[str] = ()) -> list[str]:
    """Extract text lines from a PDF byte payload sorted by page and top-to-bottom Y coordinate."""
    return [b[3] for b in _extract_pdf_blocks(data, passwords=passwords)]


def extract_pdf_rows_by_y(
    data: bytes,
    passwords: Sequence[str] = (),
) -> list[tuple[int, float, tuple[str, ...]]]:
    """Group PDF text blocks sharing the same `(page, y_coord)` into left-to-right column tuples."""
    blocks = _extract_pdf_blocks(data, passwords=passwords)
    grouped: dict[tuple[int, float], list[tuple[float, str]]] = {}
    for page_idx, y_coord, x_coord, txt in blocks:
        grouped.setdefault((page_idx, y_coord), []).append((x_coord, txt))

    rows: list[tuple[int, float, tuple[str, ...]]] = []
    for (page_idx, y_coord), cols in sorted(grouped.items()):
        cols.sort(key=lambda c: c[0])
        rows.append((page_idx, y_coord, tuple(c[1] for c in cols)))
    return rows


def extract_pdf_text(data: bytes, passwords: Sequence[str] = ()) -> str:
    """Extract newline-delimited sorted text from a PDF byte payload."""
    return "\n".join(extract_pdf_lines(data, passwords=passwords))


def parse_biff8_xls_rows(data: bytes) -> list[list[str | float]]:
    """Extract 2D cell grid from an OLE2 BIFF8 (.xls) Workbook stream in pure Python."""
    sst: list[str] = []
    cells: dict[tuple[int, int], str | float] = {}
    in_sheet = False

    def _decode_rk(rk: int) -> float:
        if rk & 0x02:
            val = float(rk >> 2)
        else:
            raw_ieee = struct.pack("<Q", (rk & ~3) << 34)
            val = struct.unpack("<d", raw_ieee)[0]
        if rk & 0x01:
            val /= 100.0
        return round(val, 6)

    bof_idx = data.find(b"\x09\x08\x10\x00\x00\x06")
    if bof_idx == -1:
        return []

    pos = bof_idx
    n = len(data)
    while pos + 4 <= n:
        rec_id, rec_len = struct.unpack_from("<HH", data, pos)
        pos += 4
        if pos + rec_len > n:
            break
        rec = data[pos : pos + rec_len]
        pos += rec_len

        if rec_id == 0x0809 and rec_len >= 6:
            _, dt = struct.unpack_from("<HH", rec, 0)
            in_sheet = dt == 0x0010
        elif rec_id == 0x00FC and rec_len >= 8:  # SST (Shared String Table)
            _, unique_strings = struct.unpack_from("<II", rec, 0)
            s_pos = 8
            for _ in range(unique_strings):
                if s_pos + 3 > len(rec):
                    break
                char_count, flags = struct.unpack_from("<HB", rec, s_pos)
                s_pos += 3
                rt_runs = 0
                ext_len = 0
                if flags & 0x08 and s_pos + 2 <= len(rec):
                    rt_runs = struct.unpack_from("<H", rec, s_pos)[0]
                    s_pos += 2
                if flags & 0x04 and s_pos + 4 <= len(rec):
                    ext_len = struct.unpack_from("<I", rec, s_pos)[0]
                    s_pos += 4
                if flags & 0x01:
                    byte_len = char_count * 2
                    s = rec[s_pos : s_pos + byte_len].decode("utf-16le", errors="replace")
                    s_pos += byte_len
                else:
                    s = rec[s_pos : s_pos + char_count].decode("latin1", errors="replace")
                    s_pos += char_count
                s_pos += rt_runs * 4 + ext_len
                sst.append(s.strip())
        elif in_sheet:
            if rec_id == 0x00FD and rec_len >= 10:  # LABELSST
                r, c, _, sst_idx = struct.unpack_from("<HHHI", rec, 0)
                if 0 <= sst_idx < len(sst):
                    cells[(r, c)] = sst[sst_idx]
            elif rec_id == 0x0204 and rec_len >= 8:  # LABEL
                r, c, _, slen = struct.unpack_from("<HHHH", rec, 0)
                cells[(r, c)] = rec[8 : 8 + slen].decode("latin1", errors="replace").strip()
            elif rec_id == 0x0203 and rec_len >= 14:  # NUMBER
                r, c, _, val = struct.unpack_from("<HHHd", rec, 0)
                cells[(r, c)] = round(val, 6)
            elif rec_id == 0x027E and rec_len >= 10:  # RK
                r, c, _, rk = struct.unpack_from("<HHHi", rec, 0)
                cells[(r, c)] = _decode_rk(rk)
            elif rec_id == 0x00BD and rec_len >= 6:  # MULRK
                r, first_c = struct.unpack_from("<HH", rec, 0)
                last_c = struct.unpack_from("<H", rec, rec_len - 2)[0]
                offset = 4
                for c in range(first_c, last_c + 1):
                    if offset + 6 <= rec_len - 2:
                        _, rk = struct.unpack_from("<Hi", rec, offset)
                        cells[(r, c)] = _decode_rk(rk)
                        offset += 6

    if not cells:
        return []
    max_r = max(r for r, _ in cells)
    max_c = max(c for _, c in cells)
    rows: list[list[str | float]] = []
    for r in range(max_r + 1):
        row_vals = [cells.get((r, c), "") for c in range(max_c + 1)]
        if any(v != "" for v in row_vals):
            rows.append(row_vals)
    return rows
