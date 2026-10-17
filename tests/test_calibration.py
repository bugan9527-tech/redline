"""标定层的纯单测：抽样、桶表、AI 契约和幂等写回。"""

from __future__ import annotations

import copy
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from contextlib import redirect_stderr

from helpers import ROOT  # noqa: F401  （helpers 会把 app/ 放进 sys.path）

import ai_layer as AI


def make_doc(chapters: int = 6, per: int = 10, kind: str = "body") -> dict:
    out = {"chapters": []}
    for ci in range(chapters):
        paras = []
        for pi in range(per):
            paras.append({
                "pid": f"c{ci + 1}p{pi + 1}",
                "kind": kind,
                "text": f"第{ci + 1}章第{pi + 1}段：他走进屋里，看见桌上有一封信。",
                "score": 0.55,
                "flags": [],
                "anchors": [],
                "evidence": ["规则证据"],
            })
        out["chapters"].append({"title": f"第{ci + 1}章", "paragraphs": paras})
    return out


class TestSamplePlan(unittest.TestCase):
    def test_signature_is_stable_and_sorted(self):
        self.assertEqual(AI.signature(0.3, ["scenery", "short_burst"]),
                         "body|0.30|scenery+short_burst")
        self.assertEqual(AI.signature(0.55, []), "body|0.55|")

    def test_deterministic_and_chapter_cap(self):
        doc = make_doc(chapters=6, per=10)
        first = AI.plan_sample(doc, budget=800, seed=7)
        second = AI.plan_sample(copy.deepcopy(doc), budget=800, seed=7)
        self.assertEqual(first["sample_pids"], second["sample_pids"])
        self.assertEqual(first["n_sample"], 60)  # 短书自适应上限，抽满 6×10
        self.assertEqual(first["chapter_limit"], 10)
        self.assertTrue(all(b["n_sample"] > 0 for b in first["blocks"]))
        for ci in range(6):
            self.assertLessEqual(sum(1 for ref in first["items"] if ref["ci"] == ci), 10)

    def test_twenty_blocks_and_rare_merge(self):
        doc = make_doc(chapters=25, per=10)
        # 造两个稀有签名，样本池必须把它们并入 other。
        doc["chapters"][0]["paragraphs"][0]["flags"] = ["scenery"]
        doc["chapters"][0]["paragraphs"][0]["score"] = 0.30
        doc["chapters"][1]["paragraphs"][0]["flags"] = ["recap"]
        doc["chapters"][1]["paragraphs"][0]["score"] = 0.20
        plan = AI.plan_sample(doc, budget=800, seed=1)
        self.assertEqual(len(plan["blocks"]), 20)
        self.assertTrue(any(row["key"] == "other" and row["n_book"] >= 2
                            for row in plan["buckets"]))
        self.assertTrue(all(b["n_sample"] > 0 for b in plan["blocks"]))

    def test_ten_chapter_short_book_also_uses_adaptive_cap(self):
        plan = AI.plan_sample(make_doc(chapters=10, per=8), budget=800, seed=1)
        self.assertEqual(plan["chapter_limit"], 8)
        self.assertEqual(plan["n_sample"], 80)

    def test_dry_does_not_write_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analyzed = root / "out" / "analyzed.json"
            analyzed.parent.mkdir()
            analyzed.write_text(json.dumps(make_doc()), encoding="utf-8")
            stats = AI.run(analyzed, dry=True, budget=800, seed=0)
            self.assertTrue(stats["dry"])
            self.assertFalse((root / "calibration.json").exists())
            self.assertFalse((root / "calibration_report.txt").exists())

    def test_run_reserves_half_budget_for_escalation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analyzed = root / "out" / "analyzed.json"
            analyzed.parent.mkdir()
            analyzed.write_text(json.dumps(make_doc(chapters=2, per=100)),
                                encoding="utf-8")
            stats = AI.run(analyzed, dry=True, budget=400, seed=0)
            self.assertEqual(stats["target"], 200)
            self.assertLessEqual(stats["n_sample"], 200)

    def test_full_run_with_stub_writes_calibration(self):
        from test_api import AiStub

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analyzed = root / "out" / "analyzed.json"
            analyzed.parent.mkdir()
            analyzed.write_text(json.dumps(make_doc(chapters=6, per=10)),
                                encoding="utf-8")
            stub = AiStub()
            try:
                stats = AI.run(analyzed, stub.config["endpoint"],
                               stub.config["model"], stub.config["key"],
                               budget=800, seed=0)
            finally:
                stub.close()
            self.assertEqual(stats["failed"], 0)
            self.assertGreater(stats["n_sample"], 0)
            cal = json.loads((root / "calibration.json").read_text(encoding="utf-8"))
            self.assertIn("prompt_hash", cal)
            self.assertIn("sample_pids", cal)
            self.assertIn("buckets", cal)
            self.assertIn("first_pass_rate", cal["metrics"])
            self.assertIn("quote_invalid_items", cal["metrics"])
            self.assertIn("label_guard", cal["metrics"])
            self.assertNotIn(stub.config["key"],
                             (root / "calibration.json").read_text(encoding="utf-8"))
            self.assertIn("escalation_pending", cal["metrics"])
            self.assertGreater(cal["metrics"]["escalation_pending"], 0)
            self.assertTrue((root / "calibration_report.txt").is_file())
            saved = json.loads(analyzed.read_text(encoding="utf-8"))
            self.assertTrue(all("score_rule" in p for ch in saved["chapters"]
                                for p in ch["paragraphs"]))

    def test_full_run_is_idempotent_for_analyzed_json(self):
        from test_api import AiStub

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analyzed = root / "out" / "analyzed.json"
            analyzed.parent.mkdir()
            analyzed.write_text(json.dumps(make_doc(chapters=6, per=10)),
                                encoding="utf-8")
            outputs = []
            for _ in range(2):
                stub = AiStub()
                try:
                    AI.run(analyzed, stub.config["endpoint"],
                           stub.config["model"], stub.config["key"],
                           budget=800, seed=0)
                finally:
                    stub.close()
                outputs.append(analyzed.read_text(encoding="utf-8"))
            self.assertEqual(outputs[0], outputs[1])


class TestFitAndApply(unittest.TestCase):
    def test_posterior_mean_is_monotonic(self):
        doc = make_doc(chapters=1, per=20)
        plan = AI.plan_sample(doc, budget=8, seed=0)
        refs = plan["items"]
        self.assertTrue(refs)
        low = [{"pid": ref["pid"], "ref": ref, "label": "neutral"}
               for ref in refs]
        high = [{"pid": ref["pid"], "ref": ref, "label": "plot"}
                for ref in refs]
        low_bucket, _ = AI.fit_buckets(plan, low)
        high_bucket, _ = AI.fit_buckets(plan, high)
        key = next(iter(low_bucket))
        self.assertGreater(high_bucket[key]["E"], low_bucket[key]["E"])

    def test_stable_bucket_writes_back_without_pending(self):
        doc = make_doc(chapters=1, per=40)
        plan = AI.plan_sample(doc, budget=8, seed=0)
        labels = [{"pid": ref["pid"], "ref": ref, "label": "plot"}
                  for ref in plan["items"]]
        buckets, _ = AI.fit_buckets(plan, labels)
        key = next(iter(buckets))
        self.assertTrue(buckets[key]["stable"])
        AI.apply_calibration(doc, plan, buckets, [])
        para = doc["chapters"][0]["paragraphs"][0]
        self.assertEqual(para["score"], 0.85)
        self.assertEqual(para["cal"]["source"], "bucket")
        self.assertFalse(para["cal"]["pending"])
        self.assertEqual(sum(1 for line in para["evidence"]
                             if line.startswith("标定:")), 1)

    def test_high_rule_plot_survives_unstable_bucket_mean(self):
        doc = make_doc(chapters=1, per=3)
        para = doc["chapters"][0]["paragraphs"][0]
        para["score"] = 0.85
        para["score_rule"] = 0.85
        ref = {
            "pid": para["pid"], "sig": "body|0.85|", "score": 0.85,
            "kind": "body", "text": para["text"],
        }
        plan = {"refs": [ref], "items": [ref], "sample_pids": [para["pid"]]}
        buckets = {
            "body|0.85|": {
                "n_sample": 8, "E": 0.62, "max_label": "plot",
                "stable": False, "rare": False,
                "p": {"plot": 0.42},
            },
        }
        audit = AI.apply_calibration(doc, plan, buckets, [])
        self.assertEqual(para["score"], 0.85)
        self.assertEqual(para["cal"]["source"], "rule_guard")
        self.assertEqual(audit["tier_after"].get("plot"), 1)

    def test_request_item_has_context_and_rule_evidence(self):
        doc = make_doc(chapters=2, per=10)
        doc["chapters"][0]["paragraphs"][1]["evidence"] = ["规则证据 A"]
        plan = AI.plan_sample(doc, budget=800, seed=0)
        ref = next(item for item in plan["refs"] if item["pid"] == "c1p2")
        request = AI._request_item(ref, 0)
        self.assertEqual(request["i"], 0)
        self.assertIn("text", request)
        self.assertIn("prev", request)
        self.assertIn("next", request)
        self.assertIn("chapter", request)
        self.assertEqual(request["rule"]["evidence"], ["规则证据 A"])

    def test_human_report_exposes_m1_pending_and_no_signal(self):
        text = AI._human_report({
            "version": 1, "n_sample": 8, "n_paras": 20, "n_escalated": 2,
            "seed": 0, "model": "stub", "endpoint_host": "local",
            "metrics": {"escalation_pending": 11, "no_signal": 7,
                        "quote_failure_rate": 0.0, "sample_shortfall": 0,
                        "block_coverage": [], "rule_errors": {}},
        }, {})
        self.assertIn("待升级：11 段", text)
        self.assertIn("规则无信号：7 段", text)

    def test_apply_is_idempotent_and_preserves_shape(self):
        doc = make_doc(chapters=3, per=9)
        doc["chapters"][0]["paragraphs"][0]["kind"] = "sys_panel"
        original_count = sum(len(ch["paragraphs"]) for ch in doc["chapters"])
        plan = AI.plan_sample(doc, budget=800, seed=2)
        labels = []
        for i, ref in enumerate(plan["items"]):
            labels.append({"pid": ref["pid"], "ref": ref,
                           "label": ["neutral", "plot", "ornament"][i % 3]})
        buckets, _ = AI.fit_buckets(plan, labels)
        one = copy.deepcopy(doc)
        two = copy.deepcopy(doc)
        audit_one = AI.apply_calibration(one, plan, buckets, [])
        AI.apply_calibration(two, plan, buckets, [])
        self.assertEqual(json.dumps(one, ensure_ascii=False),
                         json.dumps(two, ensure_ascii=False))
        self.assertEqual(sum(len(ch["paragraphs"]) for ch in one["chapters"]),
                         original_count)
        self.assertEqual(sum(audit_one["tier_before"].values()), original_count)
        self.assertEqual(sum(audit_one["tier_after"].values()), original_count)
        self.assertEqual(one["chapters"][0]["paragraphs"][0]["kind"], "sys_panel")
        self.assertIn("score_rule", one["chapters"][0]["paragraphs"][0])
        evidence = one["chapters"][1]["paragraphs"][0]["evidence"]
        self.assertEqual(sum(1 for line in evidence if line.startswith("标定:")), 1)

    def test_reader_kept_paragraph_is_not_sampled_or_rewritten(self):
        doc = make_doc(chapters=2, per=6)
        kept = doc["chapters"][0]["paragraphs"][0]
        kept["keep"] = True
        kept["score"] = 0.37
        plan = AI.plan_sample(doc, budget=800, seed=0)
        self.assertNotIn(kept["pid"], plan["sample_pids"])
        labels = [{"pid": ref["pid"], "ref": ref, "label": "neutral"}
                  for ref in plan["items"]]
        buckets, _ = AI.fit_buckets(plan, labels)
        AI.apply_calibration(doc, plan, buckets, [])
        self.assertEqual(kept["score"], 0.37)
        self.assertNotIn("cal", kept)

    def test_calibration_evidence_is_not_sent_as_rule_evidence(self):
        doc = make_doc(chapters=1, per=10)
        doc["chapters"][0]["paragraphs"][0]["evidence"] = [
            "原始规则证据",
            "标定: body|0.55| → mid (样本 8，AI 普通 50%)",
            "AI 复核 0.55 → 0.6: 旧路径",
        ]
        plan = AI.plan_sample(doc, budget=800, seed=0)
        ref = next(item for item in plan["refs"]
                   if item["pid"] == "c1p1")
        self.assertEqual(ref["evidence"], ["原始规则证据"])


class TestReplyContract(unittest.TestCase):
    def item(self) -> dict:
        return {"i": 0, "text": "他推开门，决定离开。", "prev": "前一段",
                "next": "后一段", "chapter": "第1章",
                "rule": {"score": 0.55}}

    def test_quote_must_be_original(self):
        valid = [{"i": 0, "label": "plot", "quote": "决定离开",
                  "why": "决定", "rule_error": "under", "rule_note": "none"}]
        out, err, _ = AI.validate_reply([self.item()], valid)
        self.assertIsNotNone(out, err)
        bad = [dict(valid[0], quote="这句话不在原文里")]
        out, err, quote_bad = AI.validate_reply([self.item()], bad)
        self.assertIsNotNone(out, err)
        self.assertEqual(quote_bad, 1)
        self.assertFalse(out[0]["quote_ok"])

    def test_quote_may_come_from_context(self):
        valid = [{"i": 0, "label": "neutral", "quote": "前一段",
                  "why": "上下文", "rule_error": "none", "rule_note": "none"}]
        out, err, _ = AI.validate_reply([self.item()], valid)
        self.assertIsNotNone(out, err)

    def test_ask_batch_keeps_labels_when_quote_retry_still_fails(self):
        raw = [{"i": 0, "label": "plot", "quote": "不在原文",
                "why": "", "rule_error": "none", "rule_note": "none"}]
        with mock.patch.object(
            AI, "_call_with_network_retries",
            side_effect=[(raw, ""), (raw, "")],
        ):
            out, err, stats = AI.ask_batch(
                [self.item()], "http://stub", "m", "k")
        self.assertIsNotNone(out, err)
        self.assertEqual(out[0]["label"], "plot")
        self.assertFalse(out[0]["quote_ok"])
        self.assertEqual(stats["quote_bad"], 1)
        self.assertEqual(stats["contract_retries"], 1)

    def test_i_must_be_in_order(self):
        items = [
            {"i": 0, "text": "甲", "prev": "", "next": "", "chapter": "1",
             "rule": {"score": 0.55}},
            {"i": 1, "text": "乙", "prev": "", "next": "", "chapter": "1",
             "rule": {"score": 0.55}},
        ]
        raw = [
            {"i": 1, "label": "neutral", "quote": "乙", "why": "",
             "rule_error": "none", "rule_note": "none"},
            {"i": 0, "label": "neutral", "quote": "甲", "why": "",
             "rule_error": "none", "rule_note": "none"},
        ]
        out, err, _ = AI.validate_reply(items, raw)
        self.assertIsNone(out)
        self.assertIn("i", err)

    def test_missing_item_is_rejected(self):
        items = [
            {"i": 0, "text": "甲", "prev": "", "next": "", "chapter": "1",
             "rule": {"score": 0.55}},
            {"i": 1, "text": "乙", "prev": "", "next": "", "chapter": "1",
             "rule": {"score": 0.55}},
        ]
        raw = [{"i": 0, "label": "neutral", "quote": "甲", "why": "",
                "rule_error": "none", "rule_note": "none"}]
        out, err, _ = AI.validate_reply(items, raw)
        self.assertIsNone(out)
        self.assertIn("请求", err)

    def test_all_bad_quotes_keep_labels_and_count_evidence_failures(self):
        items = [
            {"i": i, "text": f"原文{i}", "prev": "", "next": "",
             "chapter": "1", "rule": {"score": 0.55}}
            for i in range(8)
        ]
        raw = [
            {"i": i, "label": "neutral", "quote": "不存在的引用",
             "why": "", "rule_error": "none", "rule_note": "none"}
            for i in range(8)
        ]
        out, err, quote_bad = AI.validate_reply(items, raw)
        self.assertIsNotNone(out, err)
        self.assertEqual(len(out), 8)
        self.assertTrue(all(not item["quote_ok"] for item in out))
        self.assertEqual(quote_bad, 8)

    def test_rule_error_is_computed_not_taken_from_model_claim(self):
        item = {"i": 0, "text": "他决定离开。", "prev": "", "next": "",
                "chapter": "1", "rule": {"score": 0.55}}
        raw = [{"i": 0, "label": "plot", "quote": "决定离开",
                "why": "", "rule_error": "none", "rule_note": "模型自称没错"}]
        out, err, _ = AI.validate_reply([item], raw)
        self.assertIsNotNone(out, err)
        self.assertEqual(out[0]["rule_error"], "under")
        self.assertEqual(out[0]["model_claim"], "none")
        self.assertFalse(out[0]["rule_error_match"])


class TestLabelGuard(unittest.TestCase):
    def make_buckets(self, labels, bucket_labels):
        labels_list = [{"label": label} for label in labels]
        buckets = {
            f"b{i}": {"n_sample": 8, "max_label": label}
            for i, label in enumerate(bucket_labels)
        }
        return labels_list, buckets

    def test_high_share_without_bucket_agreement_does_not_fail(self):
        labels, buckets = self.make_buckets(
            ["neutral"] * 96 + ["plot"] * 4,
            ["neutral", "plot", "plot"],
        )
        guard = AI._label_guard(labels, buckets)
        self.assertFalse(guard["degenerate"])
        self.assertTrue(guard["warning"])

    def test_high_share_with_consistent_buckets_fails(self):
        labels, buckets = self.make_buckets(
            ["neutral"] * 96 + ["plot"] * 4,
            ["neutral", "neutral", "neutral"],
        )
        guard = AI._label_guard(labels, buckets)
        self.assertTrue(guard["degenerate"])
        self.assertFalse(guard["warning"])


class TestAskAggregation(unittest.TestCase):
    def test_quote_bad_items_and_first_pass_are_aggregated(self):
        refs = [{"pid": f"p{i}", "ci": 0, "text": "正文", "prev": "", "next": "",
                 "chapter": "1", "score": 0.55, "flags": [], "anchors": [],
                 "evidence": [], "sig": "body|0.55|"} for i in range(8)]
        with redirect_stderr(io.StringIO()), mock.patch.object(AI, "ask_batch", return_value=(
                None, "bad", {"quote_bad": 8, "first_pass_items": 0,
                              "first_pass_batches": 0, "contract_retries": 1})):
            labels, stats = AI._ask_refs(refs, "http://stub", "m", "k", 8, "ask")
        self.assertEqual(labels, [])
        self.assertEqual(stats["quote_failures"], 8)
        self.assertEqual(stats["first_pass_items"], 0)
        self.assertEqual(stats["contract_retries"], 1)


class TestConcurrentBatches(unittest.TestCase):
    def test_batches_overlap_and_merge_in_source_order(self):
        refs = [
            {
                "pid": f"p{i}", "ci": i // 10, "text": f"正文{i}", "prev": "",
                "next": "", "chapter": str(i // 10 + 1), "score": 0.55,
                "flags": [], "anchors": [], "evidence": [], "sig": "body|0.55|",
            }
            for i in range(32)
        ]
        lock = threading.Lock()
        active = 0
        peak = 0

        def fake_ask_batch(items, endpoint, model, key):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return (
                [{"i": item["i"], "label": "neutral", "quote": item["text"]}
                 for item in items],
                "",
                {"quote_bad": 0, "first_pass_items": len(items),
                 "first_pass_batches": 1, "contract_retries": 0},
            )

        with mock.patch.object(AI, "ask_batch", side_effect=fake_ask_batch):
            labels, stats = AI._ask_refs(
                refs, "http://stub", "m", "k", 8, "ask", concurrency=3)

        self.assertGreaterEqual(peak, 2)
        self.assertEqual([item["pid"] for item in labels],
                         [ref["pid"] for ref in refs])
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["batches"], 4)


class TestEscalation(unittest.TestCase):
    def test_split_bucket_is_selected_by_gain(self):
        refs = []
        for i in range(40):
            sig = "body|0.55|" if i < 30 else "body|0.30|scenery"
            refs.append({"pid": f"p{i}", "ci": i % 2, "pi": i,
                         "sig": sig, "text": "正文", "score": 0.55})
        plan = {"refs": refs, "sample_pids": []}
        buckets = {
            "body|0.55|": {"n_book": 30, "entropy": 0.2, "rare": False,
                           "stable": False, "E": 0.5},
            "body|0.30|scenery": {"n_book": 10, "entropy": 0.9, "rare": False,
                                  "stable": False, "E": 0.4},
        }
        selected, by_bucket = AI.plan_escalation(plan, buckets, 5, 1)
        self.assertEqual(len(selected), 5)
        self.assertTrue(all(item["sig"] == "body|0.30|scenery" for item in selected))
        self.assertEqual(by_bucket.get("body|0.30|scenery"), 5)

    def test_rare_bucket_wins_before_split(self):
        refs = []
        for i in range(5):
            refs.append({"pid": f"r{i}", "ci": i, "pi": 0,
                         "sig": "body|0.30|scenery", "text": "正文", "score": 0.3})
        for i in range(5, 40):
            refs.append({"pid": f"s{i}", "ci": i, "pi": 0,
                         "sig": "body|0.55|", "text": "正文", "score": 0.55})
        plan = {"refs": refs, "sample_pids": []}
        buckets = {
            "body|0.30|scenery": {"n_book": 5, "entropy": 0.1, "rare": True,
                                  "stable": False, "E": 0.3},
            "body|0.55|": {"n_book": 35, "entropy": 1.0, "rare": False,
                           "stable": False, "E": 0.55},
        }
        selected, _ = AI.plan_escalation(plan, buckets, 5, 1)
        self.assertEqual(len(selected), 5)
        self.assertTrue(all(item["sig"] == "body|0.30|scenery" for item in selected))

    def test_budget_shortfall_is_pending_not_silent(self):
        doc = make_doc(chapters=1, per=20)
        plan = AI.plan_sample(doc, budget=800, seed=0, sample_cap=5)
        labels = [{"pid": ref["pid"], "ref": ref, "label": "neutral"}
                  for ref in plan["items"]]
        buckets, _ = AI.fit_buckets(plan, labels)
        key = next(iter(buckets))
        self.assertFalse(buckets[key]["stable"])
        selected, _ = AI.plan_escalation(plan, buckets, 2, 0)
        self.assertEqual(len(selected), 2)
        escalation = [{"pid": ref["pid"], "ref": ref, "label": "plot"}
                      for ref in selected]
        audit = AI.apply_calibration(doc, plan, buckets, escalation)
        self.assertGreater(audit["pending_by_bucket"].get(key, 0), 0)
        pending = [p for p in doc["chapters"][0]["paragraphs"]
                   if p.get("cal", {}).get("pending")]
        self.assertGreater(len(pending), 0)


if __name__ == "__main__":
    unittest.main()
