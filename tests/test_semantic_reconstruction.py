import pytest
from aviation_ft import core


def page(number, rows, doc_id="DOC-fixture"):
    return {
        "doc_id": doc_id, "filename": "rules.pdf", "pdf_page": number,
        "printed_page": None, "width": 595, "height": 842,
        "ocr_used": False, "possible_scanned": False,
        "blocks": [
            {"block_id": f"{doc_id}-p{number:04d}-b{i:04d}",
             "bbox": bbox, "text": text, "ocr_used": False}
            for i, (bbox, text) in enumerate(rows, 1)
        ],
    }


def test_merged_table_cell_emitted_before_rows_stays_in_one_table():
    p = page(13, [
        ([236, 534, 359, 548], "表2.2.1 航空器典型火灾场景"),
        ([126, 562, 489, 573], "分类\n火灾位置\n必选火灾场景"),
        ([117, 636, 153, 647], "舱内火灾"),
        ([223, 578, 358, 600], "驾驶舱火灾\n√"),
        ([227, 596, 358, 618], "货舱火灾\n√"),
        ([223, 686, 466, 708], "行李架火灾\n√"),
        ([78, 720, 500, 740], "2.2.2 下一条规定。"),
    ])
    units = core.semantic_blocks_for_page(p)
    assert [u["kind"] for u in units] == ["table", "prose"]
    assert len(units[0]["block_ids"]) == 6
    assert "舱内火灾" in units[0]["text"]
    assert "2.2.2" not in units[0]["text"]


def test_cross_page_list_item_keeps_all_page_locators_and_stops_at_next_clause(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    job = core.create_job("cross-page")
    first = page(1, [
        ([98, 710, 517, 735], "1）设备必须安装安全保护装置，避免燃料"),
    ])
    second = page(2, [
        ([78, 84, 517, 108], "流入密闭空间造成事故。"),
        ([78, 112, 517, 136], "3.9.4 下一条规定应独立保留。"),
    ])
    core.write_jsonl(core.job_dir(job) / "parsed/pages.jsonl", [first, second])
    units = core.semantic_blocks_for_document([first, second])
    assert len(units) == 2
    joined = units[0]
    assert joined["kind"] == "list_item"
    assert joined["text"].endswith("避免燃料流入密闭空间造成事故。")
    assert not joined["quality_issues"]
    assert [x["pdf_page"] for x in joined["source_locations"]] == [1, 2]
    item = {**joined, "quote": joined["text"], "topic": "设备", "clause_group": "3.9", "split": "train"}
    saved = core.add_evidence_batch(job, [item], "reviewer")[0]
    assert saved["end_pdf_page"] == 2
    assert saved["source_locations"] == joined["source_locations"]
    assert saved["quote"] == joined["text"]
    with pytest.raises(ValueError, match="跨页范围"):
        core.add_evidence_batch(job, [{**item, "end_block_id": units[1]["end_block_id"]}], "reviewer")


def test_cross_page_does_not_join_across_missing_pages_or_documents():
    a = page(1, [([78, 710, 500, 735], "1.1.1 本条没有结束")])
    b = page(3, [([78, 84, 500, 108], "另一个页面内容。")])
    c = page(2, [([78, 84, 500, 108], "另一个文档内容。")], "DOC-other")
    assert len(core.semantic_blocks_for_document([a, b, c])) == 3


def test_body_in_margin_is_not_discarded():
    p = page(1, [
        ([78, 60, 500, 80], "1.1.1 本条从靠近页眉处开始。"),
        ([78, 735, 500, 755], "1.1.2 本条在页末继续"),
        ([78, 758, 500, 778], "并在此结束。"),
        ([280, 795, 315, 815], "—\n１\n—"),
    ])
    units = core.semantic_blocks_for_page(p)
    assert len(units) == 2
    assert units[1]["text"] == "1.1.2 本条在页末继续并在此结束。"
    assert len(units[1]["block_ids"]) == 2


def test_suggestion_and_saved_evidence_exclude_interleaved_header(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    job = core.create_job("canonical-source")
    p = page(1, [
        ([78, 100, 500, 120], "1.1.1 为保证设备安全运行，本条规定必须进行检查，并且需要"),
        ([78, 56, 500, 76], "重复页眉内容"),
        ([78, 122, 500, 142], "记录每次检查结果以备核查，发现异常时应立即停止设备运行。"),
    ])
    core.write_jsonl(core.job_dir(job) / "parsed/pages.jsonl", [p])
    units = core.semantic_blocks_for_document([p])
    suggestions = core.evidence_suggestions(job, p["doc_id"])
    assert len(suggestions) == 1
    assert suggestions[0]["quote"] == units[0]["text"]
    saved = core.add_evidence_batch(job, suggestions, "reviewer")[0]
    assert saved["quote"] == units[0]["text"]
    assert saved["block_ids"] == units[0]["block_ids"]
    assert "页眉" not in saved["raw_source_text"]
    assert saved["source_locations"] == units[0]["source_locations"]


def test_txt_uses_canonical_units_and_never_bridges_missing_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    job = core.create_job("canonical-txt")
    pages = [page(1, [([78, 710, 500, 735], "1.1.1 未完的条文")]),
             page(3, [([78, 84, 500, 108], "这是不相邻页面的另一段文字。")])]
    meta = core.load_job(job)
    meta["documents"] = [{"doc_id": "DOC-fixture", "filename": "rules.pdf"}]
    core.write_json(core.job_dir(job) / "job.json", meta)
    core.write_jsonl(core.job_dir(job) / "parsed/pages.jsonl", pages)
    report = core.build_semantic_txt(job)
    text = (core.job_dir(job) / "parsed/semantic_corpus.txt").read_text()
    assert report["semantic_units"] == len(core.semantic_blocks_for_document(pages)) == 2
    assert report["merged_cross_page"] == 0
    assert "PDF 页 1-3" not in text
