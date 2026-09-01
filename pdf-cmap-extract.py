#!/usr/bin/env python3
"""
PDF内に埋め込まれたフォントの cmap テーブルを抽出して表示/CSV出力する。

Usage:
    python pdf-cmap-extract.py <input.pdf> [--csv <output.csv>]
    python pdf-cmap-extract.py <input.pdf> [--list-fonts]
    python pdf-cmap-extract.py <input.pdf> [--font-index N]
"""

import sys
import os
import argparse
import struct

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._c_m_a_p import CmapSubTable


def extract_font_programs(pdf_path):
    """PDFから埋め込みフォントのプログラムデータを抽出する。
    戻り値: [(font_name, font_data_bytes), ...]
    """
    if fitz is None:
        print("ERROR: PyMuPDF (pymupdf) が必要です。 pip install pymupdf", file=sys.stderr)
        sys.exit(1)

    doc = fitz.open(pdf_path)
    fonts_found = []

    # 全ページのフォントリソースをスキャン
    for page_num in range(len(doc)):
        page = doc[page_num]
        # ページのフォントディクショナリ
        fonts = page.get_fonts()
        for f in fonts:
            # f: dict with keys like 'name', 'type', 'xref', 'fontname', etc.
            fonts_found.append(f)

    # 重複除去（同じフォントが複数ページで参照される）
    seen = set()
    unique_fonts = []
    for f in fonts_found:
        key = (f.get('xref'), f.get('fontname', ''))
        if key not in seen:
            seen.add(key)
            unique_fonts.append(f)

    if not unique_fonts:
        print("No embedded fonts found on page resources.")
        # ドキュメント全体のフォントリストも試す
        doc_fonts = doc.get_fontlist()
        if doc_fonts:
            unique_fonts = doc_fonts

    results = []
    for f in unique_fonts:
        fontname = f.get('fontname', f.get('name', 'unknown'))
        xref = f.get('xref', 0)
        if not xref:
            continue
        try:
            # xref からフォントオブジェクトを読み取る
            font_obj = doc.get_tounicode(xref)
            # フォントプログラムのストリームデータを取得
            stream = doc.get_data(xref)
            if stream and len(stream) > 0:
                results.append((fontname, stream, xref))
        except Exception as e:
            print(f"  [!] xref {xref} ({fontname}): {e}", file=sys.stderr)

    doc.close()
    return results


def try_extract_font_from_pdf(pdf_path, font_index=0):
    """PDFから埋め込みフォントを抽出し、メモリ上でTTFontとして開けるか試す。
    複数の方法で試行する。
    """
    doc = fitz.open(pdf_path)

    # --- 方法1: ページのフォントリソースから ---
    candidates = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        fonts = page.get_fonts()
        for f in fonts:
            candidates.append(f)

    if not candidates:
        print("No fonts found on pages.")
        doc.close()
        return []

    # ユニークなxrefごとにまとめる
    seen_xref = set()
    unique_candidates = []
    for f in candidates:
        x = f.get('xref', 0)
        if x and x not in seen_xref:
            seen_xref.add(x)
            unique_candidates.append(f)

    if font_index >= len(unique_candidates):
        print(f"Font index {font_index} out of range (0-{len(unique_candidates)-1})")
        doc.close()
        return []

    target = unique_candidates[font_index]
    xref = target.get('xref', 0)
    fontname = target.get('fontname', target.get('name', '?'))
    print(f"  Font: {fontname}  xref={xref}")

    results = []

    # xref から生ストリームデータ取得
    try:
        raw = doc.get_data(xref)
        if raw and len(raw) > 4:
            # 先頭4バイトでフォント形式を判定
            sig = raw[:4]
            if sig in (b'\x00\x01\x00\x00', b'\x01\x00\x00\x00',  # TrueType
                       b'OTTO', b'ttcf', b'wOFF', b'wOFF'):
                print(f"  -> Detected font signature: {sig[:4]}")
                results.append((fontname, raw))
            elif sig == b'\x00\x01\x00\x00' or sig[:2] in (b'\x00\x01', b'\x01\x00'):
                # TrueType の可能性
                results.append((fontname, raw))
            else:
                print(f"  -> Unrecognized signature: {sig[:4].hex()} '{sig[:4]}'")
                # それでもTTFontに渡してみる
                results.append((fontname, raw))
    except Exception as e:
        print(f"  [!] Failed to get font data: {e}")

    doc.close()
    return results


def analyze_font_data(font_data_list, output_csv=None):
    """抽出したフォントデータをfonttoolsで解析し、cmapを表示する。"""
    for fontname, data in font_data_list:
        print(f"\n{'='*60}")
        print(f"Font: {fontname}  ({len(data)} bytes)")
        print(f"{'='*60}")

        # 一時ファイルに書き出してTTFontで開く
        ext = '.ttf'  # 仮。実際はシグネチャで判定
        tmp_path = f'/tmp/_pdf_font_{fontname.replace("/", "_")}{ext}'
        with open(tmp_path, 'wb') as f:
            f.write(data)

        try:
            font = TTFont(tmp_path)
        except Exception as e:
            print(f"  [!] TTFont open failed: {e}")
            # .otf として試す
            tmp_path2 = tmp_path.replace('.ttf', '.otf')
            with open(tmp_path2, 'wb') as f:
                f.write(data)
            try:
                font = TTFont(tmp_path2)
            except Exception as e2:
                print(f"  [!] Also failed as .otf: {e2}")
                continue

        # cmapテーブル存在確認
        if 'cmap' not in font:
            print("  [!] No cmap table in this font")
            continue

        print("\n  cmap sub-tables:")
        for tbl in font['cmap'].tables:
            print(f"    format={tbl.format}, platformID={tbl.platformID}, "
                  f"platEncID={tbl.platEncID}, entries={len(tbl.cmap)}")

        # getBestCmap() で最適なcmapを取得
        best = font['cmap'].getBestCmap()
        if best:
            print(f"\n  Best cmap ({len(best)} entries):")

            if output_csv:
                with open(output_csv, 'w', encoding='utf-8') as fp:
                    fp.write("code,uvs,glyph\n")
                    for code, glyph in best.items():
                        fp.write(f"U+{code:X},,{glyph}\n")
                    # UVS (format 14) があれば追加
                    for tbl in font['cmap'].tables:
                        if tbl.format == 14:
                            for uvs, entries in tbl.uvsDict.items():
                                for code, glyph in entries:
                                    fp.write(f"U+{code:X},U+{uvs:X},{glyph}\n")
                print(f"  -> CSV saved: {output_csv}")
            else:
                # 表示: 最初の20件 + 制御文字はスキップ表示
                shown = 0
                for code, glyph in sorted(best.items()):
                    if shown >= 20:
                        print(f"    ... and {len(best) - 20} more entries")
                        break
                    # 制御文字はラベル表示
                    ch = chr(code) if code >= 0x20 and code != 0x7f else f'<U+{code:X}>'
                    print(f"    U+{code:04X} ({ch}) -> {glyph}")
                    shown += 1

        # UVS (format 14) 個別表示
        for tbl in font['cmap'].tables:
            if tbl.format == 14 and tbl.uvsDict:
                print(f"\n  UVS (variation selector) table:")
                for uvs, entries in tbl.uvsDict.items():
                    print(f"    UVS U+{uvs:X}:")
                    for code, glyph in entries[:10]:
                        print(f"      U+{code:X} -> {glyph}")
                    if len(entries) > 10:
                        print(f"      ... and {len(entries)-10} more")

        # 全サブテーブルのcmapを個別に表示（オプション）
        print(f"\n  All sub-table details:")
        for tbl in font['cmap'].tables:
            print(f"    format={tbl.format} platformID={tbl.platformID} "
                  f"platEncID={tbl.platEncID}: {len(tbl.cmap)} entries")
            if len(tbl.cmap) <= 5:
                for code, glyph in tbl.cmap.items():
                    print(f"      U+{code:X} -> {glyph}")

        font.close()
        # 一時ファイル削除
        try:
            os.remove(tmp_path)
        except:
            pass

    return


def list_embedded_fonts(pdf_path):
    """PDFに埋め込まれたフォントの一覧を表示する。"""
    doc = fitz.open(pdf_path)
    all_fonts = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        for f in page.get_fonts():
            all_fonts.append(f)

    seen = set()
    for i, f in enumerate(all_fonts):
        key = (f.get('xref'), f.get('fontname', ''))
        if key not in seen:
            seen.add(key)
            print(f"  [{i}] xref={f.get('xref')}  "
                  f"name='{f.get('fontname', f.get('name', '?'))}'  "
                  f"type={f.get('type', '?')}")

    doc.close()
    if not seen:
        print("  (No embedded fonts found)")
    return


def main():
    parser = argparse.ArgumentParser(
        description='Extract cmap table from embedded fonts in a PDF')
    parser.add_argument('pdf', help='Input PDF file path')
    parser.add_argument('--csv', '-o', type=str, default=None,
                        help='Output CSV file path')
    parser.add_argument('--list-fonts', '-l', action='store_true',
                        help='List embedded fonts and exit')
    parser.add_argument('--font-index', '-i', type=int, default=0,
                        help='Font index to extract (default: 0)')

    args = parser.parse_args()

    if not os.path.exists(args.pdf):
        print(f"Error: PDF not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    print(f"PDF: {args.pdf}")

    if args.list_fonts:
        list_embedded_fonts(args.pdf)
        return

    fonts_data = try_extract_font_from_pdf(args.pdf, font_index=args.font_index)
    if not fonts_data:
        print("Failed to extract any font data. Try --list-fonts first.")
        sys.exit(1)

    analyze_font_data(fonts_data, output_csv=args.csv)


if __name__ == '__main__':
    main()
