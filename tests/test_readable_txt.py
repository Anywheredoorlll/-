from types import SimpleNamespace

from aviation_ft import readable_txt as txt


def test_normalize_width_and_pdf_glyphs_preserves_scientific_symbols():
    assert txt.normalize("３􀆰３􀆰２　面积１２ｍ²ꎬ温度９３℃ꎮ") == "3.3.2 面积12m²，温度93℃。"
    assert txt.normalize("«规范»ꎻ") == "《规范》；"


def test_geometry_fills_merged_cells_but_keeps_unchecked_columns_empty():
    rows = [["分类", "必选", "可选"], ["舱内", "√", ""], [None, "", "√"]]
    table = SimpleNamespace(
        extract=lambda: rows, col_count=3, row_count=3,
        rows=[SimpleNamespace(cells=[(0, 0, 10, 10), (10, 0, 20, 10), (20, 0, 30, 10)]),
              SimpleNamespace(cells=[(0, 10, 10, 30), (10, 10, 20, 20), (20, 10, 30, 20)]),
              SimpleNamespace(cells=[None, (10, 20, 20, 30), (20, 20, 30, 30)])])
    matrix, depth = txt.table_matrix(table)
    assert depth == 1
    assert matrix[1:] == [["舱内", "√", ""], ["舱内", "", "√"]]


def test_two_tier_headers_preserve_parent_column_names():
    rows = [["类型", "长度", None], [None, "最小值", "最大值"], ["A", "9", "14"]]
    table = SimpleNamespace(
        extract=lambda: rows, col_count=3, row_count=3,
        rows=[SimpleNamespace(cells=[(0, 0, 10, 20), (10, 0, 30, 10), None]),
              SimpleNamespace(cells=[None, (10, 10, 20, 20), (20, 10, 30, 20)]),
              SimpleNamespace(cells=[(0, 20, 10, 30), (10, 20, 20, 30), (20, 20, 30, 30)])])
    matrix, depth = txt.table_matrix(table)
    assert depth == 2
    assert matrix == [["类型", "长度 / 最小值", "长度 / 最大值"], ["A", "9", "14"]]


def test_latin_spaces_come_from_character_gaps():
    chars = [{"c": c, "bbox": (x, 0, x + 5, 10)}
             for c, x in zip("firetest", [0, 5, 10, 15, 24, 29, 34, 39])]
    page = SimpleNamespace(get_text=lambda _: {"blocks": [{"lines": [{"spans": [{"chars": chars, "size": 10}]}]}]})
    assert txt.geometry_text_repairs(page) == {"firetest": "fire test"}


def unit(text, page=10, kind="prose", number=1):
    return {"text": text, "raw_text": text, "kind": kind, "doc_id": "d",
            "pdf_page": page, "end_pdf_page": page, "semantic_id": str(number),
            "source_locations": [{"pdf_page": page, "block_id": str(number), "bbox": [0, 0, 1, 1]}]}


def test_clause_keeps_list_explanation_and_page_locators():
    units = [unit("封面", 1), unit("3.1 一般规定", kind="heading"),
             unit("3.1.1 应满足以下要求:", number=2),
             unit("1 第一项；", kind="list_item", number=3),
             unit("2 第二项。", 11, "list_item", 4),
             unit("【条文说明】说明内容。", 11, number=5),
             unit("3.1.2 下一条。", 11, number=6)]
    events, excluded = txt.document_blocks(units, txt.PdfLayout(None))
    blocks = [e for e in events if "parts" in e]
    assert len(excluded) == 1
    assert len(blocks) == 2
    assert blocks[0]["parts"] == ["3.1.1 应满足以下要求:", "1. 第一项；", "2. 第二项。", "【条文说明】说明内容。"]
    assert [loc["block_id"] for loc in blocks[0]["source_locations"]] == ["2", "3", "4", "5"]
    assert blocks[0]["end_pdf_page"] == 11


def test_term_and_reference_boundaries_are_readable():
    layout = txt.PdfLayout(None)
    term = unit("1.3.1 跳火bounce fire火焰现象。")
    term["raw_text"] = "1.3.1 跳火bounce fire\n火焰现象。"
    assert layout.text(term) == "1.3.1 跳火\n英文术语：bounce fire\n火焰现象。"
    assert layout.text(unit("引用[1] 标准甲[2] 标准乙")) == "引用\n[1] 标准甲\n[2] 标准乙"
