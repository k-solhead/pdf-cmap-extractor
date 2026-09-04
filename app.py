import streamlit as st
import io
import os
import tempfile
import zipfile
import struct
import re

# ---------------------------------------------------------------------------
# バックエンドロジック
# ---------------------------------------------------------------------------

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz as fitz
    except ImportError:
        fitz = None

from fontTools.ttLib import TTFont


# ─── OTF wrapper for CFF fonts ──────────────────────────────────

def build_otf_from_cff(cff_data):
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
    tmp = tempfile.NamedTemporaryFile(suffix='.otf', delete=False)
    tmp.write(font_bytes)
    path = tmp.name
    tmp.close()
    try:
        return TTFont(path)
    except Exception:
        os.unlink(path)
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

    if 'DescendantFonts' in keys:
        df_ref = doc.xref_get_key(font_xref, 'DescendantFonts')
        if df_ref and df_ref[0] == 'xref':
            for candidate in range(font_xref - 5, font_xref + 5):
                if candidate <= 0:
                    continue
                ck = doc.xref_get_keys(candidate)
                if 'FontDescriptor' in ck and 'Subtype' in ck:
                    st_sub = doc.xref_get_key(candidate, 'Subtype')
                    if st_sub and 'CIDFont' in str(st_sub[1]):
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
    sx = _get_font_stream_xref(doc, font_xref)
    if sx is None:
        return None
    return doc.xref_stream(sx)  # decompressed


def get_tounicode_cmap(doc, font_xref):
    """PDFのToUnicode CMapを抽出。戻り値: [(pdf_code_hex, unicode_hex), ...]"""
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
            m = re.match(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', line)
            if m:
                results.append((m.group(1), m.group(2)))
    return results


# ─── Font listing ───────────────────────────────────────────────

def list_embedded_fonts(pdf_bytes):
    if fitz is None:
        return [], "PyMuPDF (pymupdf) が必要です。"

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    # (xref, fontname) → 出現ページ番号リスト のマップを作る
    page_map = {}
    for page_num in range(len(doc)):
        page = doc[page_num]
        for f in page.get_fonts():
            key = (f[0], f[3])
            if key not in page_map:
                page_map[key] = {'pages': set(), 'f': f}
            page_map[key]['pages'].add(page_num + 1)  # 1-indexed ページ番号

    # ユニークフォントを出現順に並べる
    seen = set()
    unique_pairs = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        for f in page.get_fonts():
            key = (f[0], f[3])
            if key not in seen:
                seen.add(key)
                unique_pairs.append(key)

    unique = [page_map[k]['f'] for k in unique_pairs]
    pages_list = [sorted(page_map[k]['pages']) for k in unique_pairs]

    doc.close()

    # 埋め込み状態チェック（2パス目）
    doc2 = fitz.open(stream=pdf_bytes, filetype='pdf')
    info_lines = []
    for i, f in enumerate(unique):
        xref = f[0]
        data = get_font_data(doc2, xref)
        tu = get_tounicode_cmap(doc2, xref)
        info_lines.append({
            'index': i,
            'xref': xref,
            'name': f[3],
            'type': f[2],
            'font_enc': f[1],
            'embedded': data is not None,
            'tounicode': len(tu) > 0,
            'pages': pages_list[i],
        })
    doc2.close()

    return info_lines, None


# ─── Extraction & analysis ──────────────────────────────────────

def extract_font_data(pdf_bytes, font_index=0):
    if fitz is None:
        return None, "PyMuPDF が必要です。"

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    # (xref, fontname) → 出現ページ番号リスト
    page_map = {}
    for page_num in range(len(doc)):
        page = doc[page_num]
        for f in page.get_fonts():
            key = (f[0], f[3])
            if key not in page_map:
                page_map[key] = {'pages': set(), 'f': f}
            page_map[key]['pages'].add(page_num + 1)

    seen = set()
    unique = []
    pages_list = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        for f in page.get_fonts():
            key = (f[0], f[3])
            if key not in seen:
                seen.add(key)
                unique.append(page_map[key]['f'])
                pages_list.append(sorted(page_map[key]['pages']))

    if font_index >= len(unique):
        doc.close()
        return None, f"フォントインデックス {font_index} が範囲外 (0-{len(unique)-1})"

    target = unique[font_index]
    xref = target[0]
    fontname = target[3]
    font_pages = pages_list[font_index]

    font_bytes = get_font_data(doc, xref)
    if font_bytes is None:
        doc.close()
        return None, f"フォント {fontname} (xref={xref}) に埋め込みストリームが見つかりません"

    # ToUnicode CMap も抽出
    tu = get_tounicode_cmap(doc, xref)

    doc.close()
    return {
        'fontname': fontname,
        'font_bytes': font_bytes,
        'tounicode': tu,
        'pages': font_pages,
    }, None


def analyze_font_data(font_data, max_preview=50):
    """フォントデータを解析し、cmap情報を返す。"""
    fontname = font_data['fontname']
    font_bytes = font_data['font_bytes']
    tu_cmap = font_data['tounicode']

    # 1) ToUnicode CMap をコードポイントに変換
    tu_entries = []
    for pdf_code, unicode_hex in tu_cmap:
        cp = int(unicode_hex, 16)
        ch = chr(cp) if cp < 0x110000 else '?'
        tu_entries.append({
            'code_point': f'U+{cp:04X}',
            'char': ch,
            'glyph': f"pdf:0x{pdf_code}",
        })

    # 2) fontTools cmap (TrueType/OTFのみ)
    font_cmap_entries = []
    font = None
    try:
        font = open_font(font_bytes)
        if 'cmap' in font:
            best = font['cmap'].getBestCmap()
            if best:
                for code, glyph in sorted(best.items()):
                    ch = chr(code) if code >= 0x20 and code != 0x7f else f'<U+{code:X}>'
                    font_cmap_entries.append({
                        'code_point': f'U+{code:04X}',
                        'char': ch,
                        'glyph': glyph,
                    })
            # Sub-table info
            sub_tables = []
            for tbl in font['cmap'].tables:
                sub_tables.append({
                    'format': tbl.format,
                    'platformID': tbl.platformID,
                    'platEncID': tbl.platEncID,
                    'entries': len(tbl.cmap),
                })

            # UVS (format 14)
            uvs_entries = []
            for tbl in font['cmap'].tables:
                if tbl.format == 14 and tbl.uvsDict:
                    for uvs, entries in tbl.uvsDict.items():
                        for code, glyph in entries:
                            uvs_entries.append({
                                'base_cp': f'U+{code:X}',
                                'uvs_cp': f'U+{uvs:X}',
                                'glyph': str(glyph),
                            })
            font.close()
        else:
            sub_tables = []
            uvs_entries = []
    except Exception as e:
        sub_tables = []
        uvs_entries = []
        font = None

    # 3) 統合: ToUnicode優先、font cmapで補完
    combined_map = {}
    for e in tu_entries:
        cp = int(e['code_point'].replace('U+', '').replace(':', ''), 16)
        combined_map[cp] = e['glyph']
    for e in font_cmap_entries:
        cp = int(e['code_point'].replace('U+', '').replace(':', ''), 16)
        if cp not in combined_map:
            combined_map[cp] = e['glyph']

    merged = []
    for cp, glyph in sorted(combined_map.items()):
        ch = chr(cp) if cp < 0x110000 else '?'
        merged.append({
            'code_point': f'U+{cp:04X}',
            'char': ch,
            'glyph': glyph,
        })

    return {
        'fontname': fontname,
        'sub_tables': sub_tables,
        'cmap_entries': merged[:max_preview],
        'total_entries': len(combined_map),
        'uvs_entries': uvs_entries,
        'font_cmap_entries': font_cmap_entries[:max_preview],
        'tounicode_entries': tu_entries,
    }, None


def cmap_to_csv_bytes(result):
    buf = io.StringIO()
    buf.write("code_point,char,glyph\n")
    for e in result['cmap_entries']:
        buf.write(f"{e['code_point']},{e['char']},{e['glyph']}\n")
    if result.get('uvs_entries'):
        buf.write("\n# Variation Selectors (UVS)\n")
        buf.write("base_code_point,uvs_code_point,glyph\n")
        for e in result['uvs_entries']:
            buf.write(f"{e['base_cp']},{e['uvs_cp']},{e['glyph']}\n")
    return buf.getvalue().encode('utf-8')


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="PDF cmap Extractor", layout="wide")
st.title("📄 PDF → cmap 抽出")
st.markdown(
    "PDFに埋め込まれたフォントのコードポイント⇔グリフマッピングを抽出します。"
    "TrueType/OTFはfontTools cmap、CFF/Type1はPDFのToUnicode CMapを使用。"
)

# ---------- ファイルアップロード ----------
uploaded = st.file_uploader(
    "PDFファイルをアップロード", type=['pdf'],
    help="フォントが埋め込まれたPDFファイルを選択"
)

if uploaded is None:
    st.info("PDFをアップロードすると、埋め込みフォントの一覧が表示されます。")
    st.stop()

pdf_bytes = uploaded.read()

# ---------- フォント一覧表示 ----------
with st.spinner("PDFを解析中…"):
    fonts, err = list_embedded_fonts(pdf_bytes)

if err:
    st.error(err)
    st.stop()

if not fonts:
    st.warning("埋め込まれたフォントが見つかりませんでした。")
    st.stop()

st.success(f"{len(fonts)} 個の埋め込みフォントを検出")

# ❌ フォントを除外した選択用リストを作る
selectable = [f for f in fonts if f['embedded']]
excluded_count = len(fonts) - len(selectable)

if excluded_count > 0:
    st.caption(f"⚠ {excluded_count} 個のフォントは埋め込まれていないため選択不可（標準フォントなど）")

if not selectable:
    st.warning("解析可能な埋め込みフォントが見つかりませんでした。")
    st.stop()

# フォント選択（ページ番号付き）
font_opts = [
    f"  [{f['index']}] {f['name']}  "
    f"(xref={f['xref']}, type={f['type']}, "
    f"p.{','.join(str(p) for p in f['pages'][:5])}"
    f"{'…' if len(f['pages']) > 5 else ''}, "
    f"{'📋 ToUnicode' if f['tounicode'] else ''})"
    for f in selectable
]
font_idx_real = st.selectbox(
    "解析するフォントを選択", index=0,
    options=range(len(font_opts)),
    format_func=lambda i: font_opts[i],
)

# selectable のインデックス → fonts の実際のインデックス
font_idx = fonts.index(selectable[font_idx_real])

# ---------- 抽出 & 解析 ----------
with st.spinner("フォントデータを抽出・解析中…"):
    font_data, err = extract_font_data(pdf_bytes, font_index=font_idx)

if err:
    st.error(f"フォント抽出失敗: {err}")
    st.stop()

fontname = font_data['fontname']
raw_bytes = font_data['font_bytes']
tu_count = len(font_data['tounicode'])

font_pages = font_data.get('pages', [])
pages_str = f"p.{','.join(str(p) for p in font_pages[:10])}{'…' if len(font_pages) > 10 else ''}" if font_pages else "ページ不明"
st.subheader(f"フォント: {fontname}")
st.caption(f"サイズ: {len(raw_bytes):,} バイト | {pages_str} | ToUnicode: {tu_count} エントリ")

result, err = analyze_font_data(font_data, max_preview=100)

if err:
    st.error(err)
    st.stop()

# ---------- cmap サブテーブル情報 (TrueType/OTFのみ) ----------
if result['sub_tables']:
    with st.expander("cmap サブテーブル一覧", expanded=True):
        cols = st.columns(4)
        cols[0].write("**format**")
        cols[1].write("**platformID**")
        cols[2].write("**platEncID**")
        cols[3].write("**entries**")
        for tbl in result['sub_tables']:
            cols[0].write(tbl['format'])
            cols[1].write(tbl['platformID'])
            cols[2].write(tbl['platEncID'])
            cols[3].write(f"{tbl['entries']:,}")
else:
    st.caption("(CFF/Type1フォントのためcmapサブテーブルはありません)")

# ---------- cmap 一覧 (ページネーション) ----------
entries = result['cmap_entries']
total = result['total_entries']

st.subheader(f"コードポイント ⇔ グリフ ({total:,} 件中 {len(entries):,} 件表示)")

page_size = st.slider("1ページの表示件数", 10, 500, 100, key='page_size')
num_pages = max(1, (len(entries) + page_size - 1) // page_size)
page_no = st.number_input("ページ", min_value=1, max_value=num_pages, value=1)
start = (page_no - 1) * page_size
end = min(start + page_size, len(entries))

rows = [[e['code_point'], e['char'], e['glyph']] for e in entries[start:end]]
st.dataframe(
    rows,
    column_config={
        0: st.column_config.Column("コードポイント"),
        1: st.column_config.Column("文字"),
        2: st.column_config.Column("グリフ"),
    },
    use_container_width=True,
    hide_index=True,
)

# ---------- UVS 表示 ----------
uvs = result.get('uvs_entries', [])
if uvs:
    with st.expander(f"異体字セレクタ (UVS, {len(uvs)} 件)", expanded=False):
        rows2 = [[e['base_cp'], e['uvs_cp'], e['glyph']] for e in uvs]
        st.dataframe(
            rows2,
            column_config={
                0: st.column_config.Column("ベースコード"),
                1: st.column_config.Column("UVSコード"),
                2: st.column_config.Column("グリフ"),
            },
            use_container_width=True,
            hide_index=True,
        )

# ---------- CSVダウンロード ----------
csv_bytes = cmap_to_csv_bytes(result)
st.download_button(
    label="📥 cmap.csv をダウンロード",
    data=csv_bytes,
    file_name="cmap.csv",
    mime="text/csv",
)

# ---------- 全サブテーブル個別ダウンロード (ZIP) ----------
if result['sub_tables']:
    buf_zip = io.BytesIO()
    with zipfile.ZipFile(buf_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for tbl_idx, tbl_info in enumerate(result['sub_tables']):
            tmp = tempfile.NamedTemporaryFile(suffix='.otf', delete=False)
            tmp.write(raw_bytes)
            tmp.close()
            try:
                f2 = TTFont(tmp.name)
                tbl = f2['cmap'].tables[tbl_idx]
                csv_part = io.StringIO()
                csv_part.write("code_point,glyph\n")
                for code, glyph in tbl.cmap.items():
                    csv_part.write(f"U+{code:X},{glyph}\n")
                zf.writestr(
                    f"cmap_subtable_{tbl_idx}_fmt{tbl_info['format']}_"
                    f"pid{tbl_info['platformID']}_"
                    f"eid{tbl_info['platEncID']}.csv",
                    csv_part.getvalue()
                )
                f2.close()
            except Exception:
                pass
            os.unlink(tmp.name)

    buf_zip.seek(0)
    st.download_button(
        label="📦 全サブテーブルCSV (ZIP)",
        data=buf_zip,
        file_name="cmap_tables.zip",
        mime="application/zip",
    )
