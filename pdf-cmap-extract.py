#!/usr/bin/env python3
"""PDFに埋め込まれたフォントのcmapテーブルを抽出するCLIツール"""

import sys
import os
import argparse
import struct
import tempfile
import re

# PyMuPDF
try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz as fitz  # type: ignore
    except ImportError:
        print("WARNING: pymupdf not installed", file=sys.stderr)
        fitz = None

from fontTools.ttLib import TTFont


# ─── OTF wrapper for CFF fonts ──────────────────────────────────

def build_otf_from_cff(cff_data):
    """CFFデータをOTFコンテナにラップする (sfntVersion='OTTO')"""
    sfnt_version = b'OTTO'
    num_tables = 1
    header = sfnt_version + struct.pack('>4H', num_tables, 16, 0, 0)
    padded = cff_data + b'\x00' * ((4 - len(cff_data) % 4) % 4)
    words = len(padded) // 4
    words_le = struct.unpack(f'<{words}I', padded)
    checksum = sum(words_le) & 0xFFFFFFFF
    data_offset = 12 + 16
    dir_entry = b'CFF ' + struct.pack('>I', checksum) + \
        struct.pack('>II', data_offset, len(cff_data))
    return header + dir_entry + cff_data


def open_font(font_bytes):
    """フォントバイト列をTTFontで開く。CFFの場合はOTFラッパーを適用。"""
    tmp = tempfile.NamedTemporaryFile(suffix='.otf', delete=False)
    tmp.write(font_bytes)
    path = tmp.name
    tmp.close()
    try:
        return TTFont(path)
    except Exception:
        os.unlink(path)
        # CFF → OTF wrapper
        otf_data = build_otf_from_cff(font_bytes)
        tmp2 = tempfile.NamedTemporaryFile(suffix='.otf', delete=False)
        tmp2.write(otf_data)
        p2 = tmp2.name
        tmp2.close()
        try:
            return TTFont(p2)
        except Exception as e:
            os.unlink(p2)
            raise Exception(f"Not TrueType/OpenType/CFF: {e}")


# ─── PyMuPDF helpers ────────────────────────────────────────────

def _get_font_stream_xref(doc, font_xref):
    """フォントxref→FontDescriptor→FontFile3 のチェーンを辿る。
    Type0(CIDFont)の場合はDescendantFonts→CIDFont→FontDescriptorも辿る。"""
    keys = doc.xref_get_keys(font_xref)

    # Type0: DescendantFonts 経由でCIDFontを探す
    if 'DescendantFonts' in keys:
        df_ref = doc.xref_get_key(font_xref, 'DescendantFonts')
        if df_ref and df_ref[0] == 'xref':
            # DescendantFonts配列のxref → その近くにCIDFontがある
            for candidate in range(font_xref - 5, font_xref + 5):
                if candidate <= 0:
                    continue
                ck = doc.xref_get_keys(candidate)
                if 'FontDescriptor' in ck and 'Subtype' in ck:
                    st = doc.xref_get_key(candidate, 'Subtype')
                    if st and 'CIDFont' in str(st[1]):
                        fd_ref = doc.xref_get_key(candidate, 'FontDescriptor')
                        if fd_ref and fd_ref[0] == 'xref':
                            fd_xref = int(fd_ref[1].split()[0])
                            for ff_key in ('FontFile', 'FontFile2', 'FontFile3'):
                                if ff_key in doc.xref_get_keys(fd_xref):
                                    ff_ref = doc.xref_get_key(fd_xref, ff_key)
                                    if ff_ref and ff_ref[0] == 'xref':
                                        ff_xref = int(ff_ref[1].split()[0])
                                        if doc.is_stream(ff_xref):
                                            return ff_xref
        return None

    # FontDescriptor 経由
    if 'FontDescriptor' in keys:
        fd_ref = doc.xref_get_key(font_xref, 'FontDescriptor')
        if fd_ref and fd_ref[0] == 'xref':
            fd_xref = int(fd_ref[1].split()[0])
            for ff_key in ('FontFile', 'FontFile2', 'FontFile3'):
                if ff_key in doc.xref_get_keys(fd_xref):
                    ff_ref = doc.xref_get_key(fd_xref, ff_key)
                    if ff_ref and ff_ref[0] == 'xref':
                        ff_xref = int(ff_ref[1].split()[0])
                        if doc.is_stream(ff_xref):
                            return ff_xref

    if doc.is_stream(font_xref):
        return font_xref
    return None


def get_font_data(doc, font_xref):
    """フォントのデコード済みストリームデータを取得。"""
    sx = _get_font_stream_xref(doc, font_xref)
    if sx is None:
        return None
    return doc.xref_stream(sx)


def get_tounicode_cmap(doc, font_xref):
    """PDFのToUnicode CMapを抽出する。
    戻り値: [(pdf_char_code, unicode), ...] のリスト
    pdf_char_code は16進数文字列（例: '20', '4A'）
    """
    keys = doc.xref_get_keys(font_xref)
    if 'ToUnicode' not in keys:
        return []
    tu_ref = doc.xref_get_key(font_xref, 'ToUnicode')
    if tu_ref is None or tu_ref[0] != 'xref':
        return []
    tu_xref = int(tu_ref[1].split()[0])
    raw = doc.xref_stream(tu_xref)
    if raw is None:
        return []
    text = raw.decode('latin-1', errors='replace')

    results = []
    # Parse beginbfchar ... endbfchar sections
    # Format: <hex> <hex>  (PDF char code → Unicode)
    in_bfchar = False
    for line in text.split('\n'):
        line = line.strip()
        if 'beginbfchar' in line:
            in_bfchar = True
            continue
        if 'endbfchar' in line:
            in_bfchar = False
            continue
        if in_bfchar and line.startswith('<'):
            # Parse: <code_hex> <unicode_hex>
            # Example: '<20> <0020>' → code='20', unicode='0020'
            m = re.match(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', line)
            if m:
                results.append((m.group(1), m.group(2)))
    return results


# ─── Font list ──────────────────────────────────────────────────

def list_embedded_fonts(pdf_path):
    """PDFに使われているフォントの一覧を表示する。"""
    doc = fitz.open(pdf_path)
    all_fonts = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        for f in page.get_fonts():
            all_fonts.append(f)

    seen = set()
    for i, f in enumerate(all_fonts):
        key = (f[0], f[3])
        if key not in seen:
            seen.add(key)
            data = get_font_data(doc, f[0])
            tu = get_tounicode_cmap(doc, f[0])
            embedded = '✅' if data else '❌'
            tu_flag = ' [ToUnicode]' if tu else ''
            sz = f' ({len(data)} bytes)' if data else ''
            print(f"  [{i}] xref={f[0]}  "
                  f"name='{f[3]}'  "
                  f"type={f[2]}  {embedded}{sz}{tu_flag}")

    doc.close()
    if not seen:
        print("  (No fonts found)")
    return


# ─── CMap extraction ────────────────────────────────────────────

def extract_cmap_from_font_bytes(font_bytes, max_entries=200):
    """TTFontでフォントを開きcmapテーブルを抽出。
    戻り値: [(unicode, glyph_name), ...]"""
    font = open_font(font_bytes)
    cmap = font['cmap']
    all_codes = {}
    for tbl in cmap.tables:
        all_codes.update(tbl.cmap)
    sorted_codes = sorted(all_codes.items(), key=lambda x: x[0])
    results = sorted_codes[:max_entries]
    font.close()
    return results


def extract_cmap_from_pdf(pdf_path, font_index=0, max_entries=200):
    """PDFから指定インデックスのフォントのcmapを抽出する。"""
    doc = fitz.open(pdf_path)

    # 全ページスキャン
    candidates = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        for f in page.get_fonts():
            candidates.append(f)

    # 重複除去
    seen_xref = set()
    unique = []
    for f in candidates:
        x = f[0]
        if x and x not in seen_xref:
            seen_xref.add(x)
            unique.append(f)

    if font_index >= len(unique):
        doc.close()
        print(f"ERROR: Font index {font_index} out of range (0-{len(unique)-1})")
        sys.exit(1)

    target = unique[font_index]
    xref = target[0]
    fontname = target[3]
    font_type = target[2]

    print(f"  Font: {fontname}  type={font_type}  xref={xref}")

    # 1) ToUnicode CMap
    tu_cmap = get_tounicode_cmap(doc, xref)
    print(f"  ToUnicode CMap: {len(tu_cmap)} entries")

    # 2) Font program cmap (TrueType/OTF only)
    font_bytes = get_font_data(doc, xref)
    font_cmap = []
    if font_bytes:
        print(f"  Font data: {len(font_bytes)} bytes")
        try:
            font_cmap = extract_cmap_from_font_bytes(font_bytes, max_entries)
            print(f"  Font cmap: {len(font_cmap)} entries")
        except Exception as e:
            print(f"  Font cmap: not available ({e})")
    else:
        print(f"  Font data: not embedded")

    doc.close()

    # 結合
    combined = {}
    # ToUnicode を優先
    for pdf_code, unicode_hex in tu_cmap:
        cp = int(unicode_hex, 16)
        combined[cp] = f"pdf:0x{pdf_code}"
    # Font cmap を追加
    for cp, gn in font_cmap:
        if cp not in combined:
            combined[cp] = gn

    sorted_combined = sorted(combined.items(), key=lambda x: x[0])
    return fontname, sorted_combined[:max_entries]


# ─── CLI ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Extract cmap table from embedded fonts in a PDF')
    parser.add_argument('pdf', help='Input PDF file path')
    parser.add_argument('--csv', '-o', type=str, default=None,
                        help='Output CSV file path')
    parser.add_argument('--font-index', '-f', type=int, default=0,
                        help='Font index to analyze (default: 0)')
    parser.add_argument('--list-fonts', '-l', action='store_true',
                        help='List embedded fonts and exit')
    parser.add_argument('--max-entries', '-n', type=int, default=200,
                        help='Max cmap entries to show (default: 200)')
    args = parser.parse_args()

    if not fitz:
        print("ERROR: pymupdf is required. pip install pymupdf", file=sys.stderr)
        sys.exit(1)

    print(f"PDF: {args.pdf}")

    if args.list_fonts:
        list_embedded_fonts(args.pdf)
        return

    fontname, cmap_entries = extract_cmap_from_pdf(
        args.pdf, args.font_index, args.max_entries)

    print(f"\n{'='*60}")
    print(f"Font: {fontname}")
    print(f"cmap entries: {len(cmap_entries)} (showing up to {args.max_entries})")
    print(f"{'='*60}")

    if args.csv:
        import csv
        with open(args.csv, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['code_point', 'char', 'glyph_info'])
            for cp, info in cmap_entries:
                ch = chr(cp) if cp < 0x110000 else '?'
                w.writerow([f"U+{cp:04X}", ch, info])
        print(f"  CSV saved: {args.csv}")
    else:
        for cp, info in cmap_entries:
            ch = chr(cp) if cp < 0x110000 else '?'
            print(f"  U+{cp:04X}  {ch}  ->  {info}")


if __name__ == '__main__':
    main()
