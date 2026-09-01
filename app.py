import streamlit as st
import io
import os
import tempfile
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# バックエンドロジック
# ---------------------------------------------------------------------------

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

from fontTools.ttLib import TTFont


def list_embedded_fonts(pdf_bytes):
    """PDFバイト列から埋め込みフォントの一覧を返す。"""
    if fitz is None:
        return [], "PyMuPDF (pymupdf) が必要です。"

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    all_fonts = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        for f in page.get_fonts():
            all_fonts.append(f)

    # 重複除去
    seen = set()
    unique = []
    for f in all_fonts:
        key = (f.get('xref'), f.get('fontname', ''))
        if key not in seen:
            seen.add(key)
            unique.append(f)

    doc.close()

    info_lines = []
    for i, f in enumerate(unique):
        info_lines.append({
            'index': i,
            'xref': f.get('xref', 0),
            'name': f.get('fontname', f.get('name', '?')),
            'type': f.get('type', '?'),
            'font_enc': f.get('font_enc', '?'),
        })

    return info_lines, None


def extract_font_data(pdf_bytes, font_index=0):
    """PDFから指定インデックスのフォント生データを抽出する。"""
    if fitz is None:
        return None, "PyMuPDF (pymupdf) が必要です。"

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')

    candidates = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        for f in page.get_fonts():
            candidates.append(f)

    seen_xref = set()
    unique = []
    for f in candidates:
        x = f.get('xref', 0)
        if x and x not in seen_xref:
            seen_xref.add(x)
            unique.append(f)

    if font_index >= len(unique):
        doc.close()
        return None, f"フォントインデックス {font_index} が範囲外 (0-{len(unique)-1})"

    target = unique[font_index]
    xref = target.get('xref', 0)
    fontname = target.get('fontname', target.get('name', '?'))

    try:
        raw = doc.get_data(xref)
        doc.close()
        return (fontname, raw), None
    except Exception as e:
        doc.close()
        return None, str(e)


def analyze_font_data(fontname, font_bytes, max_preview=50):
    """フォントデータを解析し、cmap情報を返す。"""
    results = []

    # 一時ファイルに書き出し
    ext = '.ttf'
    tmp_path = f'/tmp/_st_font_{fontname.replace("/", "_").replace(" ", "_")}{ext}'
    with open(tmp_path, 'wb') as f:
        f.write(font_bytes)

    font = None
    try:
        font = TTFont(tmp_path)
    except Exception:
        tmp_path2 = tmp_path.replace('.ttf', '.otf')
        with open(tmp_path2, 'wb') as f:
            f.write(font_bytes)
        try:
            font = TTFont(tmp_path2)
        except Exception as e:
            return None, f"TTFont open 失敗: {e}"

    if 'cmap' not in font:
        font.close()
        return None, "cmap テーブルが存在しません"

    # --- サブテーブル一覧 ---
    sub_tables = []
    for tbl in font['cmap'].tables:
        sub_tables.append({
            'format': tbl.format,
            'platformID': tbl.platformID,
            'platEncID': tbl.platEncID,
            'entries': len(tbl.cmap),
        })

    # --- Best cmap ---
    best = font['cmap'].getBestCmap()
    cmap_entries = []
    if best:
        for code, glyph in sorted(best.items()):
            ch = chr(code) if code >= 0x20 and code != 0x7f else f'<U+{code:X}>'
            cmap_entries.append({
                'code_point': f'U+{code:04X}',
                'char': ch,
                'glyph': glyph,
            })

    # --- UVS (format 14) ---
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
    os.remove(tmp_path)

    return {
        'fontname': fontname,
        'sub_tables': sub_tables,
        'cmap_entries': cmap_entries,
        'uvs_entries': uvs_entries,
    }, None


def cmap_to_csv_bytes(cmap_result):
    """解析結果をCSVバイト列に変換する。"""
    buf = io.StringIO()
    buf.write("code_point,char,glyph\n")
    for e in cmap_result['cmap_entries']:
        buf.write(f"{e['code_point']},{e['char']},{e['glyph']}\n")
    # UVS
    if cmap_result['uvs_entries']:
        buf.write("\n# Variation Selectors (UVS)\n")
        buf.write("base_code_point,uvs_code_point,glyph\n")
        for e in cmap_result['uvs_entries']:
            buf.write(f"{e['base_cp']},{e['uvs_cp']},{e['glyph']}\n")
    return buf.getvalue().encode('utf-8')


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="PDF cmap Extractor", layout="wide")
st.title("📄 PDF → cmap 抽出")
st.markdown(
    "PDFに埋め込まれたフォントの cmap テーブル（コードポイント⇔グリフ）を抽出します。"
)

# ---------- ファイルアップロード ----------
uploaded = st.file_uploader(
    "PDFファイルをアップロード", type=['pdf'],
    help="フォントが埋め込まれたPDFファイルを選択"
)

if uploaded is None:
    st.info("PDFをアップロードすると、埋め込みフォントの一覧が表示されます。")
    st.stop()

# PDF読み込み
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

# フォント選択
font_opts = [f"  [{f['index']}] {f['name']}  (xref={f['xref']})" for f in fonts]
font_idx = st.selectbox(
    "解析するフォントを選択", index=0,
    options=range(len(font_opts)),
    format_func=lambda i: font_opts[i],
)

# ---------- 抽出 & 解析 ----------
with st.spinner("フォントデータを抽出・解析中…"):
    font_data, err = extract_font_data(pdf_bytes, font_index=font_idx)

if err:
    st.error(f"フォント抽出失敗: {err}")
    st.stop()

fontname, raw_bytes = font_data

st.subheader(f"フォント: {fontname}")
st.caption(f"サイズ: {len(raw_bytes):,} バイト")

result, err = analyze_font_data(fontname, raw_bytes, max_preview=100)

if err:
    st.error(err)
    st.stop()

# ---------- サブテーブル情報 ----------
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

# ---------- cmap 一覧 (ページネーション) ----------
entries = result['cmap_entries']
uvs = result['uvs_entries']

st.subheader(f"コードポイント ⇔ グリフ ({len(entries):,} 件)")

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
buf_zip = io.BytesIO()
with zipfile.ZipFile(buf_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
    for tbl_idx, tbl_info in enumerate(result['sub_tables']):
        # 個別サブテーブルのcmapを取得
        # fontTools から直接取得するため、再解析
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix='.ttf', delete=False)
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
