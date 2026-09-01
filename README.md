# PDF cmap Extractor

PDF に埋め込まれたフォントの **cmap テーブル**（コードポイント ⇔ グリフインデックス）を抽出する Streamlit アプリ。

## 使い方

### Docker Compose（推奨）

```bash
docker compose up -d
# → http://localhost:8501
```

PDFをアップロードするだけで解析できます。ホストの `./pdfs/` ディレクトリもコンテナ内の `/pdfs` にマウントされます。

### 直接実行

```bash
pip install -r requirements.txt
streamlit run app.py
```

### CLI版（Docker不要、単発解析）

同リポジトリの `pdf-cmap-extract.py` はCLIツールです：

```bash
python pdf-cmap-extract.py sample.pdf --list-fonts
python pdf-cmap-extract.py sample.pdf --font-index 0 --csv cmap.csv
```

## 出力内容

| 項目 | 説明 |
|------|------|
| cmap サブテーブル一覧 | format, platformID, platEncID, エントリ数 |
| コードポイント ⇔ グリフ | U+XXXX → グリフ名（ページネーション表示） |
| 異体字セレクタ (UVS) | format=14 のテーブルがあれば表示 |
| CSVダウンロード | 全エントリのCSV |
| ZIPダウンロード | サブテーブル個別のCSVをZIPで一括取得 |

## 技術概要

1. **PyMuPDF** でPDFを開き、各ページのフォントリソースをスキャン
2. xref（内部参照ID）からフォントの生バイナリデータを抽出
3. **fonttools** の `TTFont` で開き cmap テーブルを解析
4. 最適なサブテーブル（`getBestCmap()`）および UVS (format=14) を表示
