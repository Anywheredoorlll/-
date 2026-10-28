"""Conservative exact-decimal checks of simple numeric relations."""
from __future__ import annotations

import re
from decimal import Decimal

NUMERIC_VERSION = "numeric-v2"
NUMBER = re.compile(
    r"(?P<operator>不低于|不少于|至少|不高于|不大于|至多|高于|低于|等于|为|是|>=|<=|>|<|=)?"
    r"\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>毫米|厘米|千米|公里|米|吨|千克|公斤|百分点|%|％|℃|°C|mm|km|kg|m)?"
)
OPERATORS = {"不低于": ">=", "不少于": ">=", "至少": ">=", ">=": ">=",
             "不高于": "<=", "不大于": "<=", "至多": "<=", "<=": "<=",
             "高于": ">", ">": ">", "低于": "<", "<": "<",
             "等于": "=", "为": "=", "是": "=", "=": "="}
PROPERTIES = ("长度", "宽度", "高度", "厚度", "距离", "速度", "重量", "质量", "面积", "数量", "比例", "占比", "温度", "净空", "坡度")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def extract_numeric_facts(text: str) -> list[dict]:
    facts = []
    citation_spans = [m.span() for m in re.finditer(r"\[E\d+\]", text)]
    for match in NUMBER.finditer(text):
        if any(start <= match.start() < end for start, end in citation_spans):
            continue
        prefix = re.split(r"[。；;.!?！？\n]", text[:match.start()])[-1].strip()
        context = _compact(prefix[-80:])
        property_name = next((p for p in PROPERTIES if context.endswith(p)), "")
        subject = context[:-len(property_name)] if property_name else context
        kind = "percentage_point" if match.group("unit") == "百分点" else (
            "percentage" if match.group("unit") in {"%", "％"} else "measurement")
        if re.search(r"(?:第|编号|序号|GB|MH/T)\s*$", context, re.I):
            kind = "identifier"
        if (match.start() > 0 and text[match.start() - 1:match.start()] == ":"
                or text[match.end():match.end() + 1] == ":"):
            kind = "ratio"
        if re.search(r"\d{4}[-年]\d{1,2}(?:[-月]\d{1,2})?", text[max(0, match.start() - 4):match.end() + 8]):
            kind = "date"
        if (match.start() > 0 and text[match.start() - 1:match.start()] == "."
                or text[match.end():match.end() + 1] == "."):
            kind = "identifier"
        facts.append({"subject": subject, "property": property_name,
                      "operator": OPERATORS.get(match.group("operator"), "="),
                      "value": str(Decimal(match.group("value"))),
                      "lower": None, "upper": None, "lower_inclusive": None,
                      "upper_inclusive": None, "unit": match.group("unit") or "",
                      "conditions": re.findall(r"(?:当|若|如果|在)([^，,；;。]+)", context),
                      "exceptions": re.findall(r"(?:除|但|例外)([^，,；;。]+)", context),
                      "kind": kind, "source_span": {"start": match.start(), "end": match.end()},
                      "context": context})
    return facts


def normalize_numeric_fact(fact: dict) -> dict:
    return {**fact, "value": str(Decimal(str(fact["value"]))),
            "unit": fact.get("unit", "").replace("％", "%"),
            "subject": _compact(fact.get("subject", "")),
            "property": _compact(fact.get("property", "")),
            "conditions": [_compact(x) for x in fact.get("conditions", [])],
            "exceptions": [_compact(x) for x in fact.get("exceptions", [])]}


def align_numeric_facts(answer_facts: list[dict], source_facts: list[dict]) -> list[dict]:
    results = []
    for fact in answer_facts:
        same_value = [source for source in source_facts
                      if source["value"] == fact["value"] and source.get("unit") == fact.get("unit")]
        same_relation = [source for source in same_value if source["operator"] == fact["operator"]]
        same_context = [source for source in same_relation
                        if fact.get("subject") and fact.get("property")
                        and source.get("subject") == fact["subject"]
                        and source.get("property") == fact["property"]
                        and source.get("conditions") == fact.get("conditions")
                        and source.get("exceptions") == fact.get("exceptions")]
        results.append({"answer_fact": fact, "value_matches": same_value,
                        "relation_matches": same_relation, "context_matches": same_context})
    return results


def compare_numeric_facts(answer: str, sources: str) -> dict:
    answer_facts = [normalize_numeric_fact(x) for x in extract_numeric_facts(answer)]
    source_facts = [normalize_numeric_fact(x) for x in extract_numeric_facts(sources)]
    aligned = align_numeric_facts(answer_facts, source_facts)
    issues = []
    for item in aligned:
        if not item["value_matches"]:
            issues.append("数字或单位在证据中缺失")
        elif not item["relation_matches"]:
            issues.append("数值比较关系冲突")
        elif not item["context_matches"]:
            issues.append("相同数字的主体、属性、条件或例外无法可靠对齐")
    if any(f["kind"] in {"identifier", "date", "ratio"} for f in answer_facts):
        issues.append("编号、日期或比例需要单独核实")
    if re.search(r"\d", re.sub(r"\[E\d+\]", "", answer)) and not answer_facts:
        issues.append("答案中数字无法解析")
    if any(not f["property"] for f in answer_facts):
        issues.append("数值属性无法可靠解析")
    if any("|" in line for line in sources.splitlines()):
        issues.append("表格数值需核对行列标题和脚注")
    status = "not_applicable" if not answer_facts and not issues else (
        "needs_human" if issues else "structure_checked")
    return {"status": status, "issues": list(dict.fromkeys(issues)),
            "answer_facts": answer_facts, "source_facts": source_facts,
            "alignments": aligned, "rule_version": NUMERIC_VERSION,
            "note": "数值结构检查通过不等于语义支持通过"}
