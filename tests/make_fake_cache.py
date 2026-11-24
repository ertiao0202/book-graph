# -*- coding: utf-8 -*-
"""构造一本假书的 v2 缓存，用于零 token 端到端测试 render / export-image / export-top。"""
import json
import sys
from pathlib import Path


def build():
    merged = [
        {"name": "宋江", "aliases": ["及时雨", "呼保义"], "en": "Song Jiang", "importance": 5,
         "group_hint": "梁山", "bio": "梁山泊首领，郓城押司出身。", "mentions": 42},
        {"name": "李逵", "aliases": ["黑旋风"], "en": "Li Kui", "importance": 4,
         "group_hint": "梁山", "bio": "江州小牢子，性如烈火。", "mentions": 30},
        {"name": "林冲", "aliases": ["豹子头"], "en": "Lin Chong", "importance": 4,
         "group_hint": "梁山", "bio": "八十万禁军教头，被逼上梁山。", "mentions": 28},
        {"name": "高俅", "aliases": [], "en": "Gao Qiu", "importance": 3,
         "group_hint": "朝廷", "bio": "殿帅府太尉，陷害林冲。", "mentions": 15},
    ]
    raw_edges = [
        {"from": "宋江", "target": "李逵", "rel": "family", "note": "肝胆相照的兄弟"},
        {"from": "李逵", "target": "宋江", "rel": "family", "note": "对宋江死心塌地"},
        {"from": "林冲", "target": "高俅", "rel": "rival", "note": "陷害之仇，火烧草料场后决裂"},
    ]
    sep = "\u0001"
    edge_first = {sep.join(sorted(("宋江", "李逵"))) + sep + "family": 0,
                  sep.join(sorted(("林冲", "高俅"))) + sep + "rival": 1}
    return {
        "version": 2,
        "title": "测试水浒传",
        "groups": ["梁山", "朝廷"],
        "mapping": {"梁山": "梁山", "朝廷": "朝廷"},
        "merged": merged,
        "raw_edges": raw_edges,
        "skipped": [],
        "rel_taxonomy": [{"rel": "family", "label": "结义兄弟", "style": "solid", "def": "x"},
                         {"rel": "rival", "label": "仇怨", "style": "dashed", "def": "y"}],
        "chunk_ranges": ["第一回 王教头私走延安府", "第二回 九纹龙大闹史家村"],
        "node_first": {"宋江": 0, "李逵": 0, "林冲": 1, "高俅": 1},
        "edge_first": edge_first,
        "node_groups": {"林冲": [[1, "朝廷"], [1, "梁山"]]},
    }


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fake_cache.json")
    out.write_text(json.dumps(build(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"written: {out}")
