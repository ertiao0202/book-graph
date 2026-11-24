# -*- coding: utf-8 -*-
"""纯函数单测：不调用 API、不读真实书稿，直接导入 scripts/book_graph.py 的核心函数。

运行：
    python3 -m unittest discover -s tests -v
或：
    python3 tests/test_core.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import book_graph as bg  # noqa: E402


class TestNormName(unittest.TestCase):
    def test_strips_punct_and_case(self):
        self.assertEqual(bg.norm_name("贾宝玉"), "贾宝玉")
        self.assertEqual(bg.norm_name(" 宝 玉 "), "宝玉")
        self.assertEqual(bg.norm_name("怡红·公子"), "怡红公子")
        self.assertEqual(bg.norm_name("Cao Cao"), "caocao")
        self.assertEqual(bg.norm_name("《魏王》"), "魏王")


class TestIsGenericTitle(unittest.TestCase):
    def test_stop_keys(self):
        for k in ("孤", "魏王", "大将军", "陛下", "主公", "逆贼", "吴王"):
            self.assertTrue(bg._is_generic_title(k), k)

    def test_suffix_rules(self):
        self.assertTrue(bg._is_generic_title("某军军师"))
        self.assertTrue(bg._is_generic_title("陈留王"))
        self.assertTrue(bg._is_generic_title("太子太傅"))

    def test_real_names_pass(self):
        for k in ("曹操", "林黛玉", "刘备", "晁盖", "西门庆"):
            self.assertFalse(bg._is_generic_title(k), k)


class TestUsableKeys(unittest.TestCase):
    def test_generic_titles_dropped(self):
        c = {"name": "曹操", "aliases": ["孟德", "魏王", "孤"]}
        keys = bg.usable_keys(c)
        self.assertIn(bg.norm_name("曹操"), keys)
        self.assertIn(bg.norm_name("孟德"), keys)
        self.assertNotIn(bg.norm_name("魏王"), keys)
        self.assertNotIn(bg.norm_name("孤"), keys)

    def test_single_char_alias_dropped_but_name_kept(self):
        c = {"name": "曹操", "aliases": ["操"]}  # 单字键会把曹操/曹丕混并，必须剔除
        keys = bg.usable_keys(c)
        self.assertIn(bg.norm_name("曹操"), keys)
        self.assertNotIn(bg.norm_name("操"), keys)


class TestCleanBio(unittest.TestCase):
    def test_scrap_perspective_anywhere(self):
        self.assertEqual(bg.clean_bio("与黛玉情投意合。本片段中过生日"), "与黛玉情投意合。过生日")
        self.assertEqual(bg.clean_bio("本片段中担任押司"), "担任押司")

    def test_punct_runs_collapsed(self):
        self.assertEqual(bg.clean_bio("出场。，、登场"), "出场。登场")

    def test_empty_and_plain(self):
        self.assertEqual(bg.clean_bio(""), "")
        self.assertEqual(bg.clean_bio("梁山泊头领"), "梁山泊头领")


class TestSplitChapters(unittest.TestCase):
    def test_cn_numbered_chapters(self):
        text = "序章\n" + "正文内容" * 300 + "\n第一章 风雪夜归\n" + "第一段" * 300 + "\n第二章 落草\n" + "第二段" * 300 + "\n第三章 聚义\n" + "第三段" * 300
        chs = bg.split_chapters(text)
        titles = [t for t, _ in chs]
        self.assertEqual(len(chs), 3)
        self.assertTrue(any("第一章" in t for t in titles))

    def test_too_few_marks_falls_back_to_full_text(self):
        text = "只有一章\n" + "内容" * 300
        chs = bg.split_chapters(text)
        self.assertEqual(len(chs), 1)
        self.assertEqual(chs[0][0], "全文")

    def test_tiny_chapters_filtered(self):
        text = "第一章 真章\n" + "正文内容" * 300 + "\n第二章 短\n几个字\n第三章 尾章\n" + "结尾内容" * 300
        chs = bg.split_chapters(text)
        self.assertEqual(len(chs), 2)


class TestPackChunks(unittest.TestCase):
    def _chapters(self, n, chars_each=500):
        return [(f"第{i}章", "字" * chars_each) for i in range(1, n + 1)]

    def test_size_limit_respected(self):
        chunks = bg.pack_chunks(self._chapters(50, 2000), max_chars=10000)
        for c in chunks:
            self.assertLessEqual(len(c["text"]), 10000 - 1200 + 2000)  # 单章可超一点，块缓冲留出余量
        # 每块 ≤ max_chars（split_oversize 先硬切，pack 再按 max_chars-1200 打包）
        for c in chunks:
            self.assertLessEqual(len(c["text"]), 10000)

    def test_range_label_single_and_span(self):
        one = bg.pack_chunks([("第三章", "字" * 500)], max_chars=40000)
        self.assertEqual(one[0]["range"], "第三章")
        many = bg.pack_chunks([("第三章", "字" * 10), ("第四章", "字" * 10)], max_chars=40000)
        self.assertEqual(many[0]["range"], "第三章 ~ 第四章")

    def test_oversize_chapter_hard_split(self):
        chs = [("第一章", "字" * 90000)]  # 单章 9 万字 > max_chars
        chunks = bg.pack_chunks(chs, max_chars=40000)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c["text"]), 40000)


class TestEstimateCost(unittest.TestCase):
    def test_formula(self):
        prov = {"price_in": 2.0, "price_out": 8.0}
        text = "字" * 62172
        est_in, est_out, cost = bg.estimate_cost(text, 2, prov)
        self.assertEqual(est_in, 62172 + 2 * 1500 + 21000)
        self.assertEqual(est_out, 2 * 3000 + 31000)
        self.assertAlmostEqual(cost, est_in / 1e6 * 2.0 + est_out / 1e6 * 8.0)


class TestNormalizeTaxonomy(unittest.TestCase):
    def test_dedup_validate_truncate(self):
        tax = bg.normalize_taxonomy([
            {"rel": "family", "label": "亲属", "style": "solid"},
            {"rel": "FAMILY", "label": "亲属2"},          # 重复代码，丢弃
            {"rel": "1bad", "label": "非法代码"},        # 非法代码，丢弃
            {"rel": "", "label": "空代码"},              # 空代码，丢弃
            {"rel": "x" * 25, "label": "超长代码"},       # 超长，丢弃
            {"rel": "rival", "label": "竞争", "style": "DASHED"},
            "garbage",                                    # 非 dict，丢弃
        ])
        self.assertEqual([t["rel"] for t in tax], ["family", "rival"])
        self.assertEqual(tax[1]["style"], "dashed")

    def test_empty_falls_back(self):
        self.assertEqual(bg.normalize_taxonomy(None), [])
        self.assertEqual(bg.normalize_taxonomy([]), [])


class TestParseJsonLoose(unittest.TestCase):
    def test_plain_and_fenced(self):
        self.assertEqual(bg.parse_json_loose('{"a": 1}'), {"a": 1})
        self.assertEqual(bg.parse_json_loose('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(bg.parse_json_loose('前言 {"a": 1} 后记'), {"a": 1})

    def test_garbage_returns_none(self):
        self.assertIsNone(bg.parse_json_loose("不是 JSON"))
        self.assertIsNone(bg.parse_json_loose(""))


class TestStripHtml(unittest.TestCase):
    def test_script_style_removed(self):
        raw = "<p>正文</p><script>var x=1;</script><style>.a{}</style><p>第二段</p>"
        out = bg.strip_html(raw)
        self.assertIn("正文", out)
        self.assertIn("第二段", out)
        self.assertNotIn("var x", out)
        self.assertNotIn(".a{}", out)


class TestClusterCharacters(unittest.TestCase):
    def _c(self, name, aliases=(), rels=()):
        return {"name": name, "aliases": list(aliases), "relations": list(rels),
                "en": "", "importance": 3, "group_hint": "", "bio": ""}

    def test_alias_exact_match_merges(self):
        chars = [self._c("宋江", aliases=["及时雨"]), self._c("及时雨", aliases=[])]
        cl = bg.cluster_characters(chars)
        self.assertEqual(len(cl), 1)
        self.assertEqual(len(cl[0]["chars"]), 2)

    def test_alias_bridge_merges_clusters(self):
        # 张三--别名-->李四，李四--别名-->王五：三个簇应桥接成一个
        chars = [self._c("张三", aliases=["李四"]), self._c("李四", aliases=["王五"]), self._c("王五")]
        cl = bg.cluster_characters(chars)
        self.assertEqual(len(cl), 1)
        self.assertEqual(len(cl[0]["chars"]), 3)

    def test_generic_title_does_not_merge(self):
        # "孤" 是 STOP_KEY：两个都以"孤"为键的人不得并团
        chars = [self._c("曹操", aliases=["孤"]), self._c("刘备", aliases=["孤"])]
        cl = bg.cluster_characters(chars)
        self.assertEqual(len(cl), 2)

    def test_single_char_key_does_not_merge(self):
        # "操" 单字键被剔除：曹操与曹植不得因"操"并团
        chars = [self._c("曹操"), self._c("操之", aliases=["操"])]
        cl = bg.cluster_characters(chars)
        names = [sorted(c["name"] for c in cluster["chars"]) for cluster in cl]
        self.assertEqual(len(cl), 2)


class TestDeriveTimeline(unittest.TestCase):
    def test_first_appearance(self):
        merged = [
            {"name": "宋江", "aliases": ["及时雨"], "en": "", "importance": 5,
             "group_hint": "", "bio": "", "mentions": 10},
            {"name": "李逵", "aliases": [], "en": "", "importance": 3,
             "group_hint": "", "bio": "", "mentions": 5},
        ]
        chunk_chars = [
            [{"name": "宋江", "relations": [{"target": "李逵", "rel": "family", "note": "主仆"}]}],
            [{"name": "李逵", "aliases": ["黑旋风"], "relations": []}],
        ]
        node_first, edge_first = bg.derive_timeline(chunk_chars, merged)
        self.assertEqual(node_first["宋江"], 0)
        self.assertEqual(node_first["李逵"], 1)  # 块0仅作为关系目标出现，块1才有独立条目
        keys = list(edge_first)
        self.assertEqual(len(keys), 1)

    def test_status_changes(self):
        merged = [{"name": "林冲", "aliases": [], "en": "", "importance": 5,
                   "group_hint": "", "bio": "", "mentions": 10}]
        mapping = {"官军": "朝廷", "土匪": "梁山"}
        groups = ["朝廷", "梁山"]
        chunk_chars = [
            [{"name": "林冲", "relations": [], "group_hint": "官军"}],
            [{"name": "林冲", "relations": [], "group_hint": "官军"}],   # 连续不变，不记变化点
            [{"name": "林冲", "relations": [], "group_hint": "土匪"}],
        ]
        seq = bg.derive_status_timeline(chunk_chars, merged, mapping, groups)
        self.assertEqual(seq["林冲"], [[0, "朝廷"], [2, "梁山"]])


class TestBuildGraph(unittest.TestCase):
    def _finals(self):
        return [
            {"name": "宋江", "aliases": [], "en": "", "group": "梁山", "importance": 5,
             "bio": "首领", "mentions": 10},
            {"name": "李逵", "aliases": [], "en": "", "group": "梁山", "importance": 3,
             "bio": "步军头领", "mentions": 5},
            {"name": "王伦", "aliases": [], "en": "", "group": "梁山", "importance": 1,
             "bio": "前任寨主", "mentions": 1},
        ]

    def test_isolated_removed_and_lead_kept(self):
        edges = [{"from": "宋江", "target": "李逵", "rel": "family", "note": "主仆"}]
        graph, report = bg.build_graph(self._finals(), edges)
        names = [n["name"] for n in graph["nodes"]]
        self.assertIn("宋江", names)
        self.assertIn("李逵", names)
        self.assertNotIn("王伦", names)             # 无关系被剔除
        self.assertEqual(report["剔除无关系人物"], 1)

    def test_lead_kept_even_if_isolated(self):
        # 主角即使无任何边也强制保留（防主角缺失）
        edges = [{"from": "李逵", "target": "王伦", "rel": "rival", "note": "火并"}]
        graph, _ = bg.build_graph(self._finals(), edges)
        names = [n["name"] for n in graph["nodes"]]
        self.assertIn("宋江", names)  # 排序后第一名

    def test_undirected_dedup_keeps_longest_note(self):
        edges = [
            {"from": "宋江", "target": "李逵", "rel": "family", "note": "短"},
            {"from": "李逵", "target": "宋江", "rel": "family", "note": "更长一些的描述"},
        ]
        graph, _ = bg.build_graph(self._finals(), edges)
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(graph["edges"][0]["label"], "更长一些的描述")

    def test_unknown_target_counted_dangling(self):
        edges = [{"from": "宋江", "target": "查无此人", "rel": "family", "note": "x" * 70}]
        graph, report = bg.build_graph(self._finals(), edges)
        self.assertEqual(report["无法归边的关系提及"], 1)

    def test_edge_first_attached(self):
        edges = [{"from": "宋江", "target": "李逵", "rel": "family", "note": "主仆"}]
        node_first = {"宋江": 0, "李逵": 1}
        edge_first = {"\u0001".join(sorted(("宋江", "李逵"))) + "\u0001family": 0}
        graph, _ = bg.build_graph(self._finals(), edges,
                                  node_first=node_first, edge_first=edge_first)
        self.assertEqual(graph["edges"][0]["first"], 0)
        firsts = {n["name"]: n["first"] for n in graph["nodes"]}
        self.assertEqual(firsts["宋江"], 0)


class TestAutoMaxNodes(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(bg.auto_max_nodes(299_999), 80)
        self.assertEqual(bg.auto_max_nodes(300_000), 120)
        self.assertEqual(bg.auto_max_nodes(799_999), 120)
        self.assertEqual(bg.auto_max_nodes(800_000), 150)


if __name__ == "__main__":
    unittest.main()
