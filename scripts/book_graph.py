#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
book_graph.py — 书籍人物关系图谱生成流水线
用法（务必通过解释器调用，勿加执行位）：
  python3 scripts/book_graph.py config --provider deepseek --key sk-xxx
  python3 scripts/book_graph.py estimate 书.txt --title "书名"
  python3 scripts/book_graph.py run 书.txt --title "书名" [--out 图谱.html] [--max-nodes N]
  python3 scripts/book_graph.py render --data 缓存.json --title "书名" [--out 图谱.html]
配置默认存 ~/.config/book-graph/config.json；抽取缓存存 ~/.cache/book-graph/（重复处理同一本书不花 token）。
"""
import argparse, hashlib, json, os, re, shutil, sys, time, zipfile
import urllib.request, urllib.error
from html.parser import HTMLParser
from pathlib import Path

# Windows 兼容兜底：stdout/stderr 被捕获（管道、CI、agent 宿主）时，中文 Windows 的
# Python 会按 GBK 编码输出，¥ / ✅ 等非 GBK 字符直接抛 UnicodeEncodeError——
# 最坏情况是钱已花完、HTML 已写出，却在最后一行打印时崩掉。统一改为 UTF-8 输出。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

CONFIG_PATH = Path(os.environ.get("BOOK_GRAPH_CONFIG", str(Path.home() / ".config/book-graph/config.json")))
CACHE_DIR = Path(os.environ.get("BOOK_GRAPH_CACHE", str(Path.home() / ".cache/book-graph")))

PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        # 元/百万 token，仅为默认值，以官网为准；可在 config 中改
        "price_in": 2.0, "price_out": 8.0,
        "doc": "https://platform.deepseek.com",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2-0905-preview",
        "price_in": 4.0, "price_out": 16.0,
        "doc": "https://platform.moonshot.cn",
    },
}

# 关系类型调色板（按分类体系顺序取色；每本书的分类数不超过此长度）
REL_PALETTE = [
    "#f48498", "#4aa3df", "#2ec4a6", "#f4a259", "#e45756",
    "#b57edc", "#9bc53d", "#e07b7b", "#118ab2", "#f5c542",
    "#06d6a0", "#8d99ae",
]

# 默认关系分类体系（v1.3.0 之前缓存的旧数据无 rel_taxonomy，渲染时回落到此表）
DEFAULT_TAXONOMY = [
    {"rel": "family", "label": "亲属", "style": "solid"},
    {"rel": "work", "label": "同事/团队", "style": "solid"},
    {"rel": "recruit", "label": "招募/招揽", "style": "dashed"},
    {"rel": "corp", "label": "收购/治理", "style": "solid"},
    {"rel": "rival", "label": "竞争/博弈", "style": "dashed"},
    {"rel": "mentor", "label": "师承/启蒙", "style": "solid"},
    {"rel": "idea", "label": "思想影响", "style": "dashed"},
    {"rel": "match", "label": "对弈", "style": "solid"},
    {"rel": "invest", "label": "投资", "style": "solid"},
    {"rel": "found", "label": "共同创立", "style": "solid"},
]


def normalize_taxonomy(taxonomy):
    """清洗模型提出的分类体系：去重、校验代码与字段，返回合法列表。"""
    out, seen = [], set()
    for t in taxonomy or []:
        if not isinstance(t, dict):
            continue
        rel = str(t.get("rel", "")).strip().lower()
        label = str(t.get("label", "")).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,19}", rel) or rel in seen or not label:
            continue
        style = "dashed" if str(t.get("style", "")).strip().lower() == "dashed" else "solid"
        seen.add(rel)
        out.append({"rel": rel, "label": label[:8], "style": style,
                    "def": str(t.get("def", "")).strip()[:100]})
    return out[:12]


def rel_defs(taxonomy, edges):
    """分类体系 → 渲染样式表 {rel: {label, color, dash}}；兜底未知 rel 用灰色。"""
    defs = {}
    for i, t in enumerate(taxonomy or []):
        defs[t["rel"]] = {
            "label": t["label"], "color": REL_PALETTE[i % len(REL_PALETTE)],
            "dash": "6,4" if t.get("style") == "dashed" else None,
        }
    for e in edges:
        if e["rel"] not in defs:
            defs[e["rel"]] = {"label": e["rel"], "color": "#8d99ae", "dash": None}
    return defs


def taxonomy_digest(taxonomy):
    return hashlib.sha1(json.dumps(taxonomy, ensure_ascii=False, sort_keys=True)
                        .encode("utf-8")).hexdigest()[:8]

MAX_RETRIES = 3


# ---------------- 配置 ----------------
def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def get_provider(cfg):
    p = cfg.get("provider", "deepseek")
    if p not in PROVIDERS:
        die(f"未知 provider：{p}（可选 {list(PROVIDERS)}）")
    base = dict(PROVIDERS[p])
    base.update({k: cfg[k] for k in ("base_url", "model", "price_in", "price_out") if k in cfg})
    if not cfg.get("api_key"):
        die(f"未配置 API key。请先运行：\n"
            f"  python3 scripts/book_graph.py config --provider {p} --key sk-你的key\n"
            f"（key 只写入本机 {CONFIG_PATH}，权限 600，不经任何第三方）")
    base["api_key"] = cfg["api_key"]
    return p, base


def die(msg):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------- API 调用 ----------------
def chat(messages, prov, json_mode=True, max_tokens=8000, return_finish=False):
    url = prov["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": prov["model"], "messages": messages, "max_tokens": max_tokens, "temperature": 0.1}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {prov['api_key']}",
            })
            with urllib.request.urlopen(req, timeout=300) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            content = out["choices"][0]["message"]["content"]
            usage = out.get("usage", {})
            finish = out["choices"][0].get("finish_reason")
            return (content, usage, finish) if return_finish else (content, usage)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:500]
            last = f"HTTP {e.code}: {detail}"
            if e.code in (401, 403):
                die(f"API 鉴权失败（{e.code}）。请检查 config 里的 key 是否有效：{CONFIG_PATH}")
            if e.code == 404 and "model" in detail.lower():
                die(f"模型名 {prov['model']} 不存在。请到 {prov['doc']} 查最新模型名，"
                    f"然后运行 config 命令更新 model。")
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        wait = 5 * attempt
        print(f"  [重试 {attempt}/{MAX_RETRIES}] {last}，{wait}s 后继续…", file=sys.stderr)
        time.sleep(wait)
    die(f"API 调用失败：{last}")


def parse_json_loose(text):
    """容忍 markdown 代码围栏；失败返回 None。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# ---------------- 书籍解析 ----------------
class _TextHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in ("p", "br", "div", "h1", "h2", "h3", "h4", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag in ("p", "div", "h1", "h2", "h3", "h4", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def strip_html(raw):
    p = _TextHTML()
    p.feed(raw)
    text = "".join(p.parts)
    text = re.sub(r"[ \t 　]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_epub(path):
    z = zipfile.ZipFile(path)
    # 1) 从 container.xml 找 OPF
    opf_name = None
    try:
        c = z.read("META-INF/container.xml").decode("utf-8", "ignore")
        m = re.search(r'full-path="([^"]+\.opf)"', c)
        if m:
            opf_name = m.group(1)
    except KeyError:
        pass
    # 2) 找 spine 顺序
    ordered = []
    if opf_name:
        opf = z.read(opf_name).decode("utf-8", "ignore")
        base = os.path.dirname(opf_name)
        id_map = dict(re.findall(r'<item[^>]*id="([^"]+)"[^>]*href="([^"]+)"', opf))
        id_map.update(dict((i, h) for h, i in re.findall(r'<item[^>]*href="([^"]+)"[^>]*id="([^"]+)"', opf)))
        for rid in re.findall(r'<itemref[^>]*idref="([^"]+)"', opf):
            href = id_map.get(rid)
            if href:
                name = os.path.normpath(os.path.join(base, href)).replace("\\", "/")
                if name in z.namelist():
                    ordered.append(name)
    if not ordered:
        ordered = sorted(n for n in z.namelist()
                         if n.lower().endswith((".xhtml", ".html", ".htm")))
    texts = []
    for name in ordered:
        raw = z.read(name).decode("utf-8", "ignore")
        t = strip_html(raw)
        if t:
            texts.append(t)
    return "\n\n".join(texts)


def read_book(path):
    p = Path(path)
    if not p.exists():
        die(f"文件不存在：{path}")
    suf = p.suffix.lower()
    if suf == ".epub":
        text = read_epub(p)
    elif suf in (".txt", ".md", ".markdown"):
        raw = p.read_bytes()
        for enc in ("utf-8", "gb18030", "big5"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            die("文本编码无法识别（尝试了 utf-8 / gb18030 / big5）")
    else:
        die(f"暂不支持 {suf} 格式（支持 .txt / .md / .epub；扫描版 PDF 不支持）")
    if len(text) < 5000:
        die(f"正文太短（{len(text)} 字符），不像是一本完整的书")
    return text


CHAPTER_RE = re.compile(
    r"^\s*(第[0-9０-９一二三四五六七八九十百千零两]+[章回节卷部][：: ]?[^\n]{0,30}|"
    r"(?:Chapter|CHAPTER|Ch\.?)\s+[0-9IVX]+[^\n]{0,40})\s*$", re.MULTILINE)


def split_chapters(text):
    marks = list(CHAPTER_RE.finditer(text))
    chapters = []
    if len(marks) >= 3:
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            chapters.append((m.group(1).strip(), text[m.start():end]))
    else:
        chapters = [("全文", text)]
    return [(t, b) for t, b in chapters if len(b.strip()) > 200]


def split_oversize(title, body, max_chars):
    """单章超过上限时按段落硬切，防止超出模型上下文。
    极端情况：整章没有换行（如脏数据 txt）仍超限时，按字符硬切兜底。"""
    if len(body) <= max_chars:
        return [(title, body)]
    paras, buf, out = body.split("\n"), [], []
    for p in paras:
        if sum(len(x) for x in buf) + len(p) > max_chars - 600 and buf:
            out.append((title, "\n".join(buf)))
            buf = []
        buf.append(p)
    if buf:
        out.append((title, "\n".join(buf)))
    # 无换行整段超长的兜底：按字符硬切，保证任何单块都不超过模型上下文
    final = []
    for t, b in out:
        while len(b) > max_chars:
            final.append((t, b[:max_chars - 600]))
            b = b[max_chars - 600:]
        final.append((t, b))
    return final


def pack_chunks(chapters, max_chars=40000):
    chapters = [piece for title, body in chapters for piece in split_oversize(title, body, max_chars)]
    chunks, buf, buf_title = [], [], ""
    for title, body in chapters:
        if sum(len(b) for _, b in buf) + len(body) > max_chars - 1200 and buf:
            chunks.append(buf)
            buf, buf_title = [], ""
        buf.append((title, body))
    if buf:
        chunks.append(buf)
    out = []
    for chs in chunks:
        t0 = chs[0][0]
        t1 = chs[-1][0]
        label = t0 if t0 == t1 else f"{t0} ~ {t1}"
        body = "\n\n".join(b for _, b in chs)
        out.append({"range": label, "text": body})
    return out


def estimate_cost(text, n_chunks, prov):
    # 中文字符≈1 token 的保守估法；分类体系1次 + 抽取1轮 + 归并1轮
    est_in = len(text) * 1.0 + n_chunks * 1500 + 21000
    est_out = n_chunks * 3000 + 31000
    cost = est_in / 1e6 * prov["price_in"] + est_out / 1e6 * prov["price_out"]
    return est_in, est_out, cost


# ---------------- 分块抽取与归并 ----------------
class ChunkFailed(Exception):
    pass


def propose_taxonomy(title, chunks, prov):
    """为这本书量身定制关系分类体系：均匀采样每个分块的开头，一次小调用。失败回落默认体系。
    均匀采样（而非只取头中尾）避免后段才登场的关系类型（抄家、归晋、出家…）整类盲区。"""
    sample = "\n\n".join("……\n\n" + ch["text"][:700] for ch in chunks)[:9000]
    prompt = PROMPT_TAXONOMY.replace("{title}", title).replace("{text}", sample)
    messages = [{"role": "system", "content": PROMPT_MERGE_SYSTEM},
                {"role": "user", "content": prompt}]
    for attempt in range(3):
        content, usage, finish = chat(messages, prov, max_tokens=3000, return_finish=True)
        data = parse_json_loose(content)
        rels = data.get("relations") if isinstance(data, dict) else None
        taxonomy = normalize_taxonomy(rels)
        if taxonomy and finish == "stop":
            print(f"  关系分类体系（本书定制）：" +
                  "、".join(f"{t['label']}" for t in taxonomy), file=sys.stderr)
            return taxonomy, usage
        print(f"  [分类体系重试 {attempt+1}/3] 输出解析失败或被截断", file=sys.stderr)
        messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "请重新输出完整合法的 JSON。"}]
    print("  [警告] 分类体系定制失败，退回默认分类", file=sys.stderr)
    return list(DEFAULT_TAXONOMY), {"prompt_tokens": 0, "completion_tokens": 0}


def extract_chunk(chunk, prov, taxonomy):
    """抽取一个分块，最多 4 次尝试（含修复追问）。失败抛 ChunkFailed，由调用方决定跳过或中止。"""
    rel_codes = {t["rel"] for t in taxonomy}
    tax_lines = "\n".join(f'   {t["rel"]} {t["label"]}（{t.get("def", "")}）' for t in taxonomy)
    prompt = (PROMPT_EXTRACT.replace("{taxonomy}", tax_lines)
                            .replace("{range}", chunk["range"])
                            .replace("{text}", chunk["text"]))
    messages = [{"role": "system", "content": PROMPT_EXTRACT_SYSTEM},
                {"role": "user", "content": prompt}]
    usage, data = {}, None
    for attempt in range(4):
        content, usage, finish = chat(messages, prov, max_tokens=16000, return_finish=True)
        data = parse_json_loose(content)
        ok = isinstance(data, dict) and isinstance(data.get("characters"), list) and finish == "stop"
        if ok:
            break
        reason = "JSON 解析失败" if not (isinstance(data, dict) and isinstance(data.get("characters"), list)) \
                 else f"输出被截断（finish={finish}）"
        print(f"  [片段重试 {attempt+1}/4] {chunk['range']}：{reason}", file=sys.stderr)
        messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content":
             "你上次输出有问题（不是合法 JSON 或被截断）。请严格控制篇幅，只输出完整合法的 JSON 对象。"}]
        data = None
    if data is None:
        raise ChunkFailed(chunk["range"])
    chars = []
    n_dropped_rel = 0    # rel 代码不在本书分类体系中，被丢弃
    n_truncated_rel = 0  # 超过单角色 20 条上限，被截断
    for c in data["characters"]:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        raw_rels = [r for r in (c.get("relations") or [])
                    if isinstance(r, dict) and r.get("target")]
        valid = [r for r in raw_rels if r.get("rel") in rel_codes]
        n_dropped_rel += len(raw_rels) - len(valid)
        n_truncated_rel += max(0, len(valid) - 20)
        chars.append({
            "name": str(c.get("name", "")).strip(),
            "aliases": [str(a).strip() for a in (c.get("aliases") or []) if str(a).strip()][:8],
            "en": str(c.get("en", "") or "").strip(),
            "importance": max(1, min(5, int(c.get("importance", 3) or 3))),
            "group_hint": str(c.get("group_hint", "") or "").strip(),
            "bio": str(c.get("bio", "") or "").strip()[:200],
            "relations": [
                {"target": str(r.get("target", "")).strip(), "rel": str(r.get("rel", "")).strip(),
                 "note": str(r.get("note", "")).strip()[:60]}
                for r in valid[:20]
            ],
        })
    return chars, usage, {"dropped_rel": n_dropped_rel, "truncated_rel": n_truncated_rel}


def norm_name(s):
    return re.sub(r"[\s·・,，.。!！?？:：'\"“”‘’()（）《》〈〉\[\]【】\-—_~～]", "", s).lower()


_BIO_SCRAP_ANY_RE = re.compile(r"(本片段|本段|本回|本章|本部分|片段|该片段|此片段|本节)(中|里|内)?[，,：:、]?")
_BIO_PUNCT_FIX_RE = re.compile(r"([。！？；])[，,、：:]+|([，,、：:])[，,、：:]+")


def clean_bio(bio):
    """剥掉抽取分块留下的视角痕迹——'本片段中'可出现在 bio 任何位置：
    '与黛玉情投意合。本片段中过生日' → '与黛玉情投意合。过生日'。"""
    b = (bio or "").strip()
    while True:
        cleaned = _BIO_SCRAP_ANY_RE.sub("", b)
        cleaned = _BIO_PUNCT_FIX_RE.sub(r"\1\2", cleaned).strip()
        if cleaned == b:
            return b
        b = cleaned


# 作匹配键时剔除的通用称谓/单字键：会把曹操（"孤"/"操"）、刘岱与马岱（"岱"）等无关人物错误合并
STOP_KEYS = {"孤", "孤家", "寡人", "朕", "王", "侯", "公", "卿", "相", "丞相", "相国",
             "魏王", "武王", "大王", "将军", "使君", "陛下", "主公", "明公", "主", "君",
             "先主", "后主", "万岁", "天子", "圣人", "先生", "师傅", "大人",
             "都督", "老将", "吴主", "吴王", "吴侯", "汉中王", "皇叔", "君侯",
             "老贼", "贼", "奸贼", "逆贼", "国贼", "匹夫", "老匹夫", "竖子", "鼠辈"}


_TITLE_SUFFIX = ("将军", "都督", "谋士", "军师", "主")


def _is_generic_title(k):
    """官职/头衔类键：作匹配键只会把大将军、魏王、军师等一群人错误并成一团。"""
    if k in STOP_KEYS:
        return True
    if k.endswith(_TITLE_SUFFIX):
        return True
    if re.fullmatch(r"[一-龥]{1,3}王", k):  # 魏王/晋王/吴王/陈留王…
        return True
    if k.endswith(("太傅", "太尉", "太师", "太保", "司徒", "司空", "尚书")):
        return True
    return False


def usable_keys(c):
    name = norm_name(c["name"])
    keys = {name} | {norm_name(a) for a in c["aliases"]}
    keys.discard("")
    return {k for k in keys if len(k) >= 2 and not _is_generic_title(k)} or {name}


def cluster_characters(all_chars):
    """按名字/别称精确匹配做并查集聚类。"""
    clusters = []          # 每项: {"names": set, "chars": [ ... ]}
    index = {}           # norm_name -> cluster idx
    for c in all_chars:
        keys = usable_keys(c)
        keys.discard("")
        hit = sorted({index[k] for k in keys if k in index})  # 去重，避免自合并
        if hit:
            i = hit[0]
            clusters[i]["chars"].append(c)
            for k in keys:
                index[k] = i
            for j in hit[1:]:  # 合并被别名桥接的簇
                for k, v in list(index.items()):
                    if v == j:
                        index[k] = i
                clusters[i]["chars"].extend(clusters[j]["chars"])
                clusters[j]["chars"] = []
        else:
            index.update({k: len(clusters) for k in keys})
            clusters.append({"names": keys, "chars": [c]})
    return [cl for cl in clusters if cl["chars"]]


def summarize_clusters(clusters):
    out = []
    for cl in clusters:
        chars = cl["chars"]
        names = sorted({c["name"] for c in chars}, key=len)
        aliases = sorted({a for c in chars for a in c["aliases"]})
        hints = sorted({c["group_hint"] for c in chars if c["group_hint"]},
                       key=lambda h: -sum(c["group_hint"] == h for c in chars))
        out.append({
            "names_seen": names[:6],
            "aliases": aliases[:10],
            "mentions": len(chars),
            "max_importance": max(c["importance"] for c in chars),
            "group_hints": hints[:3],
            "bio_draft": max((c["bio"] for c in chars), key=len, default="")[:120],
        })
    return out


def merge_clusters(clusters, prov):
    """Map-Reduce 归并：分批让模型在批内归并（输出量受控），跨批由代码并查集去重。
    返回 (groups, mapping, merged 全集, usage)——节点上限不在此处裁。"""
    clusters = sorted(clusters, key=lambda cl: -len(cl["chars"]))
    summary = summarize_clusters(clusters)
    BATCH = 60
    total_batches = (len(summary) + BATCH - 1) // BATCH
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # ---- Map：逐批归并 ----
    batch_outputs = []
    for bi, bstart in enumerate(range(0, len(summary), BATCH)):
        batch = summary[bstart:bstart + BATCH]
        prompt = PROMPT_MERGE_BATCH.replace("{clusters}", json.dumps(batch, ensure_ascii=False))
        messages = [{"role": "system", "content": PROMPT_MERGE_SYSTEM},
                    {"role": "user", "content": prompt}]
        data = None
        for attempt in range(4):
            content, usage, finish = chat(messages, prov, max_tokens=8000, return_finish=True)
            for k in total_usage:
                total_usage[k] += usage.get(k, 0)
            data = parse_json_loose(content)
            if isinstance(data, dict) and isinstance(data.get("characters"), list) and finish == "stop":
                break
            print(f"  [归并批次 {bi+1}/{total_batches} 重试 {attempt+1}/4] 输出解析失败或被截断",
                  file=sys.stderr)
            messages = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "请重新输出完整合法的 JSON，控制篇幅，确保在限制内结束。"}]
            data = None
        if data is None:
            die(f"归并批次 {bi+1}/{total_batches} 失败，已重试 4 次")
        batch_outputs.append(data["characters"])
        print(f"  [归并批次 {bi+1}/{total_batches}] 完成，本批输出 {len(data['characters'])} 人",
              file=sys.stderr)

    # ---- 跨批代码级并查集去重 ----
    entries = []
    for chars in batch_outputs:
        for c in chars:
            if not isinstance(c, dict) or not str(c.get("name", "")).strip():
                continue
            entries.append({
                "name": str(c["name"]).strip(),
                "aliases": [str(a).strip() for a in (c.get("aliases") or []) if str(a).strip()][:10],
                "en": str(c.get("en", "") or "").strip(),
                "group_hint": str(c.get("group_hint", "") or "").strip(),
                "importance": max(1, min(5, int(c.get("importance", 3) or 3))),
            })
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in entries:
        e["keys"] = usable_keys(e)
        if not e["keys"]:
            continue
        r = find(next(iter(e["keys"])))
        for k in e["keys"]:
            rk = find(k)
            if rk != r:
                parent[rk] = r

    byroot = {}
    for e in entries:
        if not e.get("keys"):
            continue
        byroot.setdefault(find(next(iter(e["keys"]))), []).append(e)

    merged = []
    for group_entries in byroot.values():
        m = {"aliases": [], "en": "", "group_hint": "", "importance": 1, "name": ""}
        for e in group_entries:
            if not m["name"]:
                m["name"] = e["name"]
            elif len(e["name"]) < len(m["name"]):
                m["name"] = e["name"]
            for a in e["aliases"]:
                if a not in m["aliases"]:
                    m["aliases"].append(a)
            m["aliases"] = m["aliases"][:10]
            if not m["en"] and e["en"]:
                m["en"] = e["en"]
            m["importance"] = max(m["importance"], e["importance"])
            if not m["group_hint"] and e["group_hint"]:
                m["group_hint"] = e["group_hint"]
        m["aliases"] = [a for a in m["aliases"] if norm_name(a) != norm_name(m["name"])]
        merged.append(m)

    # ---- 回联原始簇：mentions、bio 与全量别名（代码拼合，不再依赖模型输出）----
    def match_cluster(m):
        keys = usable_keys(m)
        best, bio, mentions = 0, "", 0
        extra_aliases = set()
        for cl in clusters:
            if cl["names"] & keys:
                mentions += len(cl["chars"])
                if len(cl["chars"]) > best:
                    best = len(cl["chars"])
                cand = max((c["bio"] for c in cl["chars"] if c["bio"]), key=len, default="")
                if len(cand) > len(bio):
                    bio = cand
                for c in cl["chars"]:
                    extra_aliases.update(a for a in c["aliases"] if len(a) >= 2)
        return mentions, bio, extra_aliases

    # 全书人名集合：别名绝不许等于"另一个角色的名字"（防抽取噪声把刘璋并进刘备后又当别名）
    book_names = {}
    for cl in clusters:
        for c in cl["chars"]:
            book_names.setdefault(norm_name(c["name"]), c["name"])

    for m in merged:
        m["mentions"], m["bio"], extra = match_cluster(m)
        m["bio"] = (m["bio"] or "")[:200]
        # 别名并集：模型归并输出 + 原始抽取中出现过的写法（剔除通用头衔与他人姓名，防错误归边）
        merged_aliases = list(dict.fromkeys(m["aliases"] + sorted(extra)))
        m["aliases"] = [a for a in merged_aliases
                        if norm_name(a) != norm_name(m["name"])
                        and not _is_generic_title(norm_name(a))
                        and norm_name(a) not in book_names][:12]
    merged.sort(key=lambda m: (-m["importance"], -m["mentions"], m["name"]))
    # 注意：这里不做 max_nodes 裁剪——裁剪是纯代码层操作，留给 build 阶段，便于零 token 调参

    # ---- 阵营命名（两阶段，输出量受控）：top hints 定组 → 其余 hints 分批映射 ----
    hint_count = {}
    for m in merged:
        hint_count[m["group_hint"] or "其他"] = hint_count.get(m["group_hint"] or "其他", 0) + 1
    ordered_hints = sorted(hint_count.items(), key=lambda kv: -kv[1])

    def naming_call(user_prompt, expect, max_tokens=3000, retries=3):
        messages = [{"role": "system", "content": PROMPT_MERGE_SYSTEM},
                    {"role": "user", "content": user_prompt}]
        for attempt in range(retries):
            content, usage, finish = chat(messages, prov, max_tokens=max_tokens, return_finish=True)
            for k in total_usage:
                total_usage[k] += usage.get(k, 0)
            data = parse_json_loose(content)
            if isinstance(data, dict) and isinstance(data.get(expect), (list, dict)) and finish == "stop":
                return data
            print(f"  [阵营命名重试 {attempt+1}/{retries}] 输出解析失败或被截断", file=sys.stderr)
            messages = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "请重新输出完整合法的 JSON。"}]
        return None

    TOP_N = 30
    top_json = json.dumps([{"hint": h, "count": n} for h, n in ordered_hints[:TOP_N]],
                          ensure_ascii=False)
    gdata = naming_call(PROMPT_GROUPS.replace("{hints}", top_json).replace("{top_n}", str(TOP_N)),
                        expect="groups")
    if gdata is None:
        print("  [警告] 阵营命名失败，退回按 hint 原始值分组", file=sys.stderr)
        gdata = {"groups": [h for h, _ in ordered_hints[:9]] or ["主要人物"], "mapping": {}}
    groups = [str(g).strip() for g in gdata.get("groups", []) if str(g).strip()][:9] \
        or [h for h, _ in ordered_hints[:9]] or ["主要人物"]
    mapping = {str(k): str(v).strip() for k, v in (gdata.get("mapping") or {}).items()}

    # 阶段 2：剩余 hints 分批映射（每批 ≤40，输出量受控）
    rest = [h for h, _ in ordered_hints[TOP_N:] if h not in mapping]
    BATCH_H = 40
    for bstart in range(0, len(rest), BATCH_H):
        batch = rest[bstart:bstart + BATCH_H]
        prompt = (PROMPT_MAP_HINTS.replace("{groups}", "、".join(groups))
                                   .replace("{hints}", json.dumps(batch, ensure_ascii=False)))
        mdata = naming_call(prompt, expect="mapping", retries=2)
        if mdata is None:
            print(f"  [警告] 线索批次 {bstart//BATCH_H+1} 映射失败，该批归入默认组", file=sys.stderr)
            continue
        for k, v in (mdata.get("mapping") or {}).items():
            mapping[str(k)] = str(v).strip()
    return groups, mapping, merged, total_usage


# ---------------- 图谱构建与渲染 ----------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompts import (PROMPT_EXTRACT_SYSTEM, PROMPT_EXTRACT, PROMPT_MERGE_SYSTEM,
                     PROMPT_MERGE_BATCH, PROMPT_GROUPS, PROMPT_MAP_HINTS, PROMPT_TAXONOMY)

GROUP_PALETTE = [
    "#f5c542", "#2ec4a6", "#4aa3df", "#f48498", "#b57edc",
    "#f4a259", "#e45756", "#9bc53d", "#8d99ae", "#e07b7b",
    "#06d6a0", "#118ab2", "#ef476f", "#ffd166",
]


def derive_timeline(chunk_chars_list, merged):
    """从分块抽取结果离线推导时间轴（零 token）：
    人物/关系首次出现的分块号。chunk_chars_list 按分块顺序排列。"""
    name_map = {}
    for m in merged:
        for k in {norm_name(m["name"])} | {norm_name(a) for a in m.get("aliases", [])}:
            name_map.setdefault(k, m["name"])

    def resolve(raw):
        return name_map.get(norm_name(str(raw or "")))

    node_first, edge_first = {}, {}
    for ci, chars in enumerate(chunk_chars_list):
        for c in chars:
            a = resolve(c.get("name"))
            if a is None:
                continue
            node_first.setdefault(a, ci)
            for r in c.get("relations") or []:
                b = resolve(r.get("target"))
                if not b or b == a:
                    continue
                key = "\u0001".join(sorted((a, b))) + "\u0001" + str(r.get("rel", ""))
                edge_first.setdefault(key, ci)
    return node_first, edge_first


def derive_status_timeline(chunk_chars_list, merged, mapping, groups):
    """从分块缓存挖掘"每人每块的阵营 hint 序列"（零 token），只存变化点。
    这是"官→匪→官"式身份演变的数据来源：归并阶段丢掉的 hint 时间维度在这里捡回。"""
    name_map = {}
    for m in merged:
        for k in {norm_name(m["name"])} | {norm_name(a) for a in m.get("aliases", [])}:
            name_map.setdefault(k, m["name"])

    def resolve(raw):
        return name_map.get(norm_name(str(raw or "")))

    def pick(hint):
        hint = (hint or "").strip()
        if not hint:
            return None
        g = mapping.get(hint)
        if g in groups:
            return g
        return hint if hint in groups else None   # 未映射的 hint 不瞎猜，保持上一次已知

    seq = {}
    for ci, chars in enumerate(chunk_chars_list):
        for c in chars:
            a = resolve(c.get("name"))
            if a is None:
                continue
            g = pick(c.get("group_hint"))
            if g is None:
                continue
            lst = seq.setdefault(a, [])
            if not lst or lst[-1][1] != g:
                lst.append([ci, g])
    return seq


def build_graph(finals, raw_edges, taxonomy=None, node_first=None, edge_first=None,
                node_groups=None):
    """finals: 归并后人物表（调用方决定节点上限）; raw_edges: 所有分块的 relations;
    taxonomy: 本书定制的关系分类体系（无则回落 DEFAULT_TAXONOMY）。"""
    taxonomy = normalize_taxonomy(taxonomy) or list(DEFAULT_TAXONOMY)
    kept = finals
    id_of = {}
    nid_name = {}   # nid -> 人物名（edge_first 的键用名归一化）
    name_map = {}  # norm -> id
    for i, f in enumerate(kept):
        nid = f"n{i}"
        id_of[f["name"]] = nid
        nid_name[nid] = f["name"]
        for k in {norm_name(f["name"])} | {norm_name(a) for a in f["aliases"]}:
            name_map.setdefault(k, nid)

    def resolve(target):
        return name_map.get(norm_name(target))

    edge_best = {}
    dangling = 0
    rdefs = rel_defs(taxonomy, [])
    for e in raw_edges:
        sid = resolve(e["from"])
        tid = resolve(e["target"])
        if not sid or not tid or sid == tid:
            if e["target"] and resolve(e["target"]) is None:
                dangling += 1
            continue
        key = (frozenset((sid, tid)), e["rel"])  # 无向去重：刘备→张飞 与 张飞→刘备 算同一条
        first = None
        if edge_first is not None and sid in nid_name and tid in nid_name:
            ek = "\u0001".join(sorted((nid_name[sid], nid_name[tid]))) + "\u0001" + e["rel"]
            first = edge_first.get(ek)
        if key not in edge_best or len(e["note"]) > len(edge_best[key]["label"]):
            edge_best[key] = {"s": sid, "t": tid, "rel": e["rel"],
                              "label": e["note"] or rdefs.get(e["rel"], {}).get("label", e["rel"]),
                              "first": first}

    # 只保留至少有一条边的人物（孤立主角除外：第一名强制保留）
    linked = {e["s"] for e in edge_best.values()} | {e["t"] for e in edge_best.values()}
    final_kept = [f for f in kept if f["name"] in id_of and id_of[f["name"]] in linked]
    if kept and kept[0] not in final_kept:
        final_kept.insert(0, kept[0])
    remap = {}
    nodes = []
    for i, f in enumerate(final_kept):
        nid = f"n{i}"
        remap[id_of[f["name"]]] = nid
        nodes.append({
            "id": nid, "name": f["name"][:12], "en": f["en"][:40],
            "aliases": f.get("aliases", [])[:12],
            "group": f["group"][:12], "size": min(34, 8 + f["importance"] * 5),
            "bio": f["bio"] or f"（{f['group']}）书中人物",
            "first": (node_first or {}).get(f["name"]),
            "changes": (node_groups or {}).get(f["name"]) or [],
        })
    edges = [e for e in edge_best.values() if e["s"] in remap and e["t"] in remap]
    for e in edges:
        e["s"], e["t"] = remap[e["s"]], remap[e["t"]]

    # 分组着色：按组内"最大节点权重"排序（传记类保证主角组拿主色），平手比总权重
    gmax, gsum = {}, {}
    for n in nodes:
        gmax[n["group"]] = max(gmax.get(n["group"], 0), n["size"])
        gsum[n["group"]] = gsum.get(n["group"], 0) + n["size"]
    gnames = sorted(gmax, key=lambda g: (-gmax[g], -gsum[g]))
    # 渲染模板要求 GROUPS 值为 {name, color} 对象，键为组名
    groups = {g: {"name": g, "color": GROUP_PALETTE[i % len(GROUP_PALETTE)]}
              for i, g in enumerate(gnames)}
    for n in nodes:
        n["group"] = n["group"] if n["group"] in groups else gnames[0]

    report = {
        "人物": len(nodes), "关系": len(edges), "分组": len(groups),
        "剔除无关系人物": len(kept) - len(nodes),
        "无法归边的关系提及": dangling,
        "孤立分组": [g for g in gnames if not any(n["group"] == g for n in nodes)],
    }
    return {"title": None, "nodes": nodes, "edges": edges, "groups": groups,
            "rel_defs": rel_defs(taxonomy, edges),
            "primary_rel": taxonomy[0]["rel"]}, report


def render(graph, title, out_path, scenes=None, subtitle=""):
    template = Path(__file__).resolve().parent / "template.html"
    if not template.exists():
        die(f"模板文件缺失：{template}")
    html = template.read_text(encoding="utf-8")
    esc = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    sub_esc = subtitle.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    sub_clean = sub_esc.lstrip("·　 ").strip()
    tab_title = f"《{esc}》人物关系图谱" + (f" · {sub_clean}" if sub_clean else "")
    html = html.replace("__TAB_TITLE__", tab_title)
    nodes_js = json.dumps(graph["nodes"], ensure_ascii=False)
    edges_js = json.dumps(graph["edges"], ensure_ascii=False)
    groups_js = json.dumps(graph["groups"], ensure_ascii=False)
    # 关系样式：优先用建图阶段算好的；旧版数据（无 rel_defs）按默认体系现场补算
    rel_defs_js = json.dumps(graph.get("rel_defs")
                             or rel_defs(DEFAULT_TAXONOMY, graph["edges"]), ensure_ascii=False)
    primary_rel = graph.get("primary_rel") or DEFAULT_TAXONOMY[0]["rel"]
    # 时间轴元数据：旧缓存（未迁移）为 None，模板据此隐藏时间轴 UI
    timeline = {"ranges": graph["timeline_ranges"]} if graph.get("timeline_ranges") else None
    timeline_js = json.dumps(timeline, ensure_ascii=False)
    # 防御：防止内容里出现 </script> 提前闭合标签
    for var in (nodes_js, edges_js, groups_js, rel_defs_js, timeline_js):
        if "</script" in var.lower():
            die("生成的数据中含有 </script>，无法安全注入页面")
    # 搜索提示用本书的头部人物，不再出现其他书的例子
    top2 = sorted(graph["nodes"], key=lambda n: -n["size"])[:2]
    tips = "、".join(n["name"] for n in top2) or "人物名"
    html = html.replace("__SEARCH_TIPS__", f"搜人物或「甲 乙」查路径，如：{tips}…")
    html = html.replace("__TITLE__", esc)
    html = html.replace("__SUBTITLE__", sub_esc)
    # 名场面榜入口：仅当 render 时显式提供画廊相对路径才注入按钮，否则留空
    if scenes:
        if '"' in scenes or "<" in scenes or ">" in scenes:
            die("--scenes 路径含非法字符")
        scenes_btn = (f'<a id="scenesBtn" href="{scenes}" target="_blank" rel="noopener">'
                      f'名场面榜</a>')
    else:
        scenes_btn = ""
    html = html.replace("__SCENES_BTN__", scenes_btn)
    html = html.replace("/*__GROUPS__*/", groups_js)
    html = html.replace("/*__NODES__*/", nodes_js)
    html = html.replace("/*__EDGES__*/", edges_js)
    html = html.replace("/*__REL_STYLES__*/", rel_defs_js)
    html = html.replace("/*__TIMELINE__*/", timeline_js)
    html = html.replace("__PRIMARY_REL__", primary_rel)
    if ("__TITLE__" in html or "__SUBTITLE__" in html or "__TAB_TITLE__" in html or "__SEARCH_TIPS__" in html or "/*__NODES__*/" in html
            or "/*__REL_STYLES__*/" in html or "__PRIMARY_REL__" in html
            or "__SCENES_BTN__" in html
            or "/*__TIMELINE__*/" in html):
        die("模板替换不完整，占位符残留")
    Path(out_path).write_text(html, encoding="utf-8")
    print(f"✅ 已生成：{out_path}（{Path(out_path).stat().st_size // 1024} KB）")
    return out_path


# ---------------- 小红书长图导出（纯 PIL，零 token） ----------------
IMG_W, IMG_H = 1080, 1440          # 小红书 3:4 竖版
IMG_MARGIN = 56
IMG_BG = (247, 244, 238)           # 暖纸底色
IMG_CARD = (255, 255, 255)
IMG_TEXT = (34, 34, 34)
IMG_MUTED = (130, 126, 118)

# 中文字体候选（按平台优先级）；找不到时明确报错，不静默出方块字
_FONT_REG = [
    "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
_FONT_BOLD = [
    "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]


def _font_loader():
    from PIL import ImageFont

    def pick(cands):
        for p in cands:
            if os.path.exists(p):
                return p
        return None

    reg, bold = pick(_FONT_REG), pick(_FONT_BOLD)
    if not reg:
        die("找不到可用的中文字体（尝试了微软雅黑/苹方/Noto CJK/文泉驿）。"
            "请安装中文字体后重试。")
    cache = {}

    def font(size, bold_=False):
        key = (size, bold_)
        if key not in cache:
            cache[key] = ImageFont.truetype(bold or reg if bold_ else reg, size)
        return cache[key]

    return font


def _wrap(draw, text, font, max_w, max_lines):
    """按像素宽度换行（中英文混排按字符累积），超行数时末行加省略号。"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n" or draw.textlength(cur + ch, font=font) > max_w:
            lines.append(cur)
            cur = "" if ch == "\n" else ch
            if len(lines) == max_lines:
                break
        else:
            cur += ch
    else:
        if cur:
            lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    # 没放完就截断末行加 …
    consumed = len("".join(lines))
    if consumed < len(text.replace("\n", "")):
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def export_long_images(graph, title, out_dir):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        die("export-image 需要 Pillow：请先 pip install pillow")

    font = _font_loader()
    nodes = graph["nodes"]
    edges = graph["edges"]
    groups = graph["groups"]
    by_id = {n["id"]: n for n in nodes}

    # 每人 top3 关系（按对方戏份权重排序，同人合并留最长描述）
    rel_by_node = {}
    for e in edges:
        for a, b in ((e["s"], e["t"]), (e["t"], e["s"])):
            rel_by_node.setdefault(a, {})
            cur = rel_by_node[a].get(b, "")
            if len(e["label"]) > len(cur):
                rel_by_node[a][b] = e["label"]

    def top_rels(nid):
        items = sorted(rel_by_node.get(nid, {}).items(),
                       key=lambda kv: -by_id[kv[0]]["size"])[:3]
        return "；".join(f"{by_id[oid]['name']}（{lbl}）" for oid, lbl in items)

    # 测量用临时画布
    measure = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    content_w = IMG_W - IMG_MARGIN * 2

    # ---- 布局：生成 (页 -> 元素列表) ----
    def card_height(n):
        h = 26  # 上边距
        h += 46                      # 姓名行
        if n.get("en"):
            h += 32                  # 英文名行
        bio_lines = _wrap(measure, n["bio"], font(25), content_w - 96, 3)
        h += len(bio_lines) * 36
        rels = top_rels(n["id"])
        if rels:
            h += 10 + len(_wrap(measure, f"关系：{rels}", font(23), content_w - 96, 2)) * 33
        return h + 22                # 下边距

    COVER_H, GROUP_H, SLIM_H, FOOT_H = 330, 86, 92, 64
    ordered_groups = [g for g in groups if any(n["group"] == g for n in nodes)]
    # 封面区高度按阵营 chips 实际行数动态调整（超宽换行）
    def chip_rows():
        rows, gx = [[]], IMG_MARGIN
        for g in ordered_groups:
            cnt = sum(1 for n in nodes if n["group"] == g)
            w = measure.textlength(f"{groups[g]['name']} {cnt}", font=font(22)) + 34
            if gx + w > IMG_W - IMG_MARGIN and rows[-1]:
                rows.append([])
                gx = IMG_MARGIN
            rows[-1].append((g, w))
            gx += w + 14
        return rows
    cover_h = COVER_H + max(0, len(chip_rows()) - 1) * 50

    pages, cur_page, y = [], [], cover_h
    for gi, g in enumerate(ordered_groups):
        gnodes = sorted((n for n in nodes if n["group"] == g), key=lambda n: -n["size"])
        group_items = [("group", g)] + [("card", n) for n in gnodes]
        for kind, payload in group_items:
            h = GROUP_H if kind == "group" else card_height(payload) + 18
            # 组标题不做页尾孤儿：空间放不下标题+一张卡时整组翻页
            need = h + (card_height(group_items[1][1]) + 18 if kind == "group" and len(group_items) > 1 else 0)
            if y + need > IMG_H - FOOT_H and cur_page:
                pages.append(cur_page)
                cur_page, y = [], SLIM_H
            cur_page.append((kind, payload))
            y += h
    if cur_page:
        pages.append(cur_page)

    def hex2rgb(hx):
        hx = hx.lstrip("#")
        return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))

    # ---- 绘制 ----
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title)
    total = len(pages)
    made = []
    for pi, page in enumerate(pages, 1):
        img = Image.new("RGB", (IMG_W, IMG_H), IMG_BG)
        d = ImageDraw.Draw(img)
        if pi == 1:
            # 封面区：书名 + 统计 + 阵营一览
            d.text((IMG_MARGIN, 72), f"《{title}》", font=font(60, True), fill=IMG_TEXT)
            d.text((IMG_MARGIN, 152), "人物关系图谱", font=font(40, True), fill=IMG_MUTED)
            d.text((IMG_MARGIN, 214),
                   f"{len(nodes)} 位人物 · {len(edges)} 条关系 · {len(ordered_groups)} 个阵营",
                   font=font(26), fill=IMG_MUTED)
            gx, gy = IMG_MARGIN, 268
            for row in chip_rows():
                for g, w in row:
                    cnt = sum(1 for n in nodes if n["group"] == g)
                    d.rounded_rectangle([gx, gy, gx + w, gy + 40], radius=20,
                                        fill=hex2rgb(groups[g]["color"]))
                    d.text((gx + 17, gy + 7), f"{groups[g]['name']} {cnt}",
                           font=font(22), fill=(14, 17, 23))
                    gx += w + 14
                gx, gy = IMG_MARGIN, gy + 50
        else:
            d.text((IMG_MARGIN, 30), f"《{title}》人物关系图谱", font=font(26, True),
                   fill=IMG_MUTED)
        y = cover_h if pi == 1 else SLIM_H
        for kind, payload in page:
            if kind == "group":
                g = payload
                color = hex2rgb(groups[g]["color"])
                cnt = sum(1 for n in nodes if n["group"] == g)
                d.rounded_rectangle([IMG_MARGIN, y + 20, IMG_MARGIN + 10, y + 58],
                                    radius=4, fill=color)
                d.text((IMG_MARGIN + 26, y + 16), groups[g]["name"],
                       font=font(34, True), fill=IMG_TEXT)
                d.text((IMG_MARGIN + 26 + d.textlength(groups[g]["name"], font=font(34, True)) + 14,
                        y + 30), f"{cnt} 人", font=font(23), fill=IMG_MUTED)
                y += GROUP_H
                continue
            # ---- 人物卡片 ----
            n = payload
            ch = card_height(n)
            gcolor = hex2rgb(groups[n["group"]]["color"])
            d.rounded_rectangle([IMG_MARGIN, y, IMG_W - IMG_MARGIN, y + ch],
                                radius=18, fill=IMG_CARD)
            d.rounded_rectangle([IMG_MARGIN, y, IMG_MARGIN + 8, y + ch], radius=4, fill=gcolor)
            tx = IMG_MARGIN + 32
            ty = y + 22
            d.text((tx, ty), n["name"], font=font(34, True), fill=IMG_TEXT)
            # 阵营标签（右侧）
            gname = groups[n["group"]]["name"]
            gw = d.textlength(gname, font=font(21)) + 26
            gx0 = IMG_W - IMG_MARGIN - 24 - gw
            d.rounded_rectangle([gx0, ty + 4, gx0 + gw, ty + 40], radius=18, fill=gcolor)
            d.text((gx0 + 13, ty + 11), gname, font=font(21), fill=(14, 17, 23))
            ty += 46
            if n.get("en"):
                d.text((tx, ty), n["en"], font=font(23), fill=IMG_MUTED)
                ty += 32
            for line in _wrap(d, n["bio"], font(25), content_w - 96, 3):
                d.text((tx, ty), line, font=font(25), fill=(60, 58, 54))
                ty += 36
            rels = top_rels(n["id"])
            if rels:
                ty += 10
                for line in _wrap(d, f"关系：{rels}", font(23), content_w - 96, 2):
                    d.text((tx, ty), line, font=font(23), fill=IMG_MUTED)
                    ty += 33
            y += ch + 18
        # 页脚：页码 + 出品
        d.text((IMG_MARGIN, IMG_H - 46), "由 book-graph 生成", font=font(20), fill=IMG_MUTED)
        pg = f"{pi} / {total}"
        d.text((IMG_W - IMG_MARGIN - d.textlength(pg, font=font(20)), IMG_H - 46),
               pg, font=font(20), fill=IMG_MUTED)
        path = out_dir / f"{safe}_{pi:02d}.png"
        img.save(path, "PNG")
        made.append(path)
    return made


def export_top_scenes(graph, data, title, chunks, top_n, out_dir):
    """名场面榜：Top N 重磅关系（关系线 + 解读 + 原文摘句 + 回目出处），单页连续长图，零 token。
    chunks 为书稿重切的处理块（带 text/range），用于摘原文句子；None 则只出关系榜。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        die("export-top 需要 Pillow：请先 pip install pillow")

    font = _font_loader()
    nodes = graph["nodes"]
    by_id = {n["id"]: n for n in nodes}
    rdefs = graph.get("rel_defs") or {}
    measure = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    content_w = IMG_W - IMG_MARGIN * 2
    ranges = data.get("chunk_ranges") or []

    # 挑榜：以两人中较小的戏份权重为主排序（避免榜被主角的边角关系占满），解读长度为次
    pairs = {}
    for e in graph["edges"]:
        if not (e.get("label") or "").strip():
            continue
        a, b = by_id.get(e["s"]), by_id.get(e["t"])
        if not a or not b:
            continue
        key = frozenset((e["s"], e["t"]))
        score = min(a["size"], b["size"]) * 10 + min(len(e["label"]), 60)
        if key not in pairs or score > pairs[key][0]:
            pairs[key] = (score, e)
    ranked = sorted(pairs.values(), key=lambda x: -x[0])[:top_n]

    def find_quote(edge):
        """在关系首发分块的原文里，找同时提到双方（含别称）的句子。
        优先两人常用名同现的，其次较短者；剥掉头尾被切断的引号括号。"""
        ci = edge.get("first")
        if chunks is None or ci is None or ci >= len(chunks):
            return None
        text = chunks[ci]["text"]
        a, b = by_id[edge["s"]], by_id[edge["t"]]
        na = [a["name"]] + [x for x in a.get("aliases", []) if len(x) >= 2]
        nb = [b["name"]] + [x for x in b.get("aliases", []) if len(x) >= 2]
        strip = "「」『』“”\"'‘’()（）【】《》、，, "
        best = None
        for s in re.split(r"(?<=[。！？!?…])", text):
            s = s.strip().strip(strip).strip()
            if len(s) < 8 or len(s) > 120:
                continue
            exact = int(a["name"] in s) + int(b["name"] in s)
            if exact < 2 and not (any(n in s for n in na) and any(n in s for n in nb)):
                continue
            key = (-exact, len(s))
            if best is None or key < best[0]:
                best = (key, s)
        return best[1] if best else None

    items = []
    for _, e in ranked:
        a, b = by_id[e["s"]], by_id[e["t"]]
        rd = rdefs.get(e["rel"], {})
        ci = e.get("first")
        items.append({
            "a": a["name"], "b": b["name"],
            "rel": rd.get("label", e["rel"]), "color": rd.get("color", "#888888"),
            "dash": bool(rd.get("dash")), "note": e["label"],
            "where": ranges[ci] if (ranges and ci is not None and ci < len(ranges)) else "",
            "quote": find_quote(e),
        })

    def hex2rgb(hx):
        hx = (hx or "#888888").lstrip("#")
        return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))

    def draw_sw(d, x, y, w, color, dash):
        c = hex2rgb(color)
        if dash:
            cx = x
            while cx < x + w:
                d.line([cx, y, min(cx + 14, x + w), y], fill=c, width=5)
                cx += 22
        else:
            d.line([x, y, x + w, y], fill=c, width=5)

    def card_h(it):
        h = 30 + 48                                    # 上距 + 人名关系行
        h += 12 + len(_wrap(measure, it["note"], font(25), content_w - 120, 2)) * 36
        if it["quote"]:
            h += 12 + len(_wrap(measure, f"\u201c{it['quote']}\u201d", font(23),
                                 content_w - 120, 3)) * 33
        h += 34                                        # 出处行
        return h + 24

    # 单页连续长图：高度按内容自适应（小红书 native 玩法，不设固定分页），零 token
    COVER_H, FOOT_H = 300, 64
    total_h = COVER_H + sum(card_h(it) + 20 for it in items) + FOOT_H

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title)
    made = []
    img = Image.new("RGB", (IMG_W, total_h), IMG_BG)
    d = ImageDraw.Draw(img)
    d.text((IMG_MARGIN, 70), f"《{title}》", font=font(60, True), fill=IMG_TEXT)
    d.text((IMG_MARGIN, 150), f"名场面榜 · Top {len(items)} 关系",
           font=font(40, True), fill=IMG_MUTED)
    nq = sum(1 for it in items if it["quote"])
    d.text((IMG_MARGIN, 214),
           f"{len(nodes)} 位人物中挑出的 {len(items)} 对重磅关系"
           + (f" · {nq} 条附原文摘句与回目" if nq else ""),
           font=font(24), fill=IMG_MUTED)
    y = COVER_H
    for rank, it in enumerate(items, 1):
        ch = card_h(it)
        d.rounded_rectangle([IMG_MARGIN, y, IMG_W - IMG_MARGIN, y + ch],
                            radius=18, fill=IMG_CARD)
        tx = IMG_MARGIN + 34
        ty = y + 26
        d.text((tx, ty - 6), f"{rank:02d}", font=font(38, True), fill=(200, 90, 60))
        title_x = tx + 84
        draw_sw(d, title_x, ty + 20, 46, it["color"], it["dash"])
        d.text((title_x + 62, ty), f"{it['a']} —{it['rel']}→ {it['b']}",
               font=font(31, True), fill=IMG_TEXT)
        ty += 56
        for line in _wrap(d, it["note"], font(25), content_w - 120, 2):
            d.text((tx, ty), line, font=font(25), fill=(60, 58, 54))
            ty += 36
        if it["quote"]:
            ty += 12
            for line in _wrap(d, f"\u201c{it['quote']}\u201d", font(23),
                              content_w - 120, 3):
                d.text((tx, ty), line, font=font(23), fill=IMG_MUTED)
                ty += 33
        if it["where"]:
            wt = f"出处：{it['where']}"
            f20 = font(20)
            max_w = content_w - 68
            while wt and d.textlength(wt + "…", font=f20) > max_w:
                wt = wt[:-1]
            if wt != f"出处：{it['where']}":
                wt += "…"
            d.text((IMG_W - IMG_MARGIN - 34 - d.textlength(wt, font=f20),
                    y + ch - 42), wt, font=f20, fill=(150, 145, 136))
        y += ch + 20
    d.text((IMG_MARGIN, total_h - 46), "由 book-graph 生成", font=font(20), fill=IMG_MUTED)
    path = out_dir / f"{safe}_名场面榜.png"
    img.save(path, "PNG")
    made.append(path)
    return made, items


def cmd_export_top(args):
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    if data.get("version") != 2:
        die("export-top 仅支持 v2 缓存")
    merged = data["merged"]
    est_chars = sum(m["mentions"] for m in merged) * 450
    graph, _report = build_from_data(data, args.max_nodes or auto_max_nodes(est_chars))
    title = args.title or data.get("title") or "书籍"

    chunks = None
    if args.book:
        text = read_book(args.book)
        chapters = split_chapters(text)
        chunks = pack_chunks(chapters, max_chars=args.chunk_chars)
        cr = data.get("chunk_ranges") or []
        if cr and len(chunks) != len(cr):
            die(f"书稿重切为 {len(chunks)} 块，与缓存的回目分块 {len(cr)} 不一致"
                f"（原 run 可能用了不同 --chunk-chars，请同步指定）")

    out_dir = args.out_dir or f"{re.sub(r'[\\/:*?\"<>|\\s]+', '_', title)}_名场面榜"
    made, items = export_top_scenes(graph, data, title, chunks, args.top, out_dir)
    print(f"✅ 名场面榜长图 {len(made)} 张 → {Path(out_dir).resolve()}")
    for i, it in enumerate(items, 1):
        mark = " · 附摘句" if it["quote"] else ""
        print(f"  {i:2d}. {it['a']} —{it['rel']}→ {it['b']}{mark}")
    if chunks is None:
        print("提示：加 --book 书稿文件可为每条关系摘原文句子并标注回目出处。")


def cmd_export_image(args):
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    if data.get("version") == 2:
        merged = data["merged"]
        est_chars = sum(m["mentions"] for m in merged) * 450
        graph, _report = build_from_data(data, args.max_nodes or auto_max_nodes(est_chars))
    else:
        graph = {k: data[k] for k in ("nodes", "edges", "groups")}
    title = args.title or data.get("title") or "书籍"
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title)
    out_dir = args.out_dir or f"{safe}_小红书长图"
    made = export_long_images(graph, title, out_dir)
    print(f"✅ 已导出 {len(made)} 页长图到：{Path(out_dir).resolve()}")
    print(f"   规格：{IMG_W}×{IMG_H} PNG（3:4 竖版，适配小红书）；零 token，纯本地绘制")


# ---------------- CLI ----------------
def cmd_config(args):
    cfg = load_config()
    if args.provider:
        cfg["provider"] = args.provider
    if args.key:
        cfg["api_key"] = args.key
    if args.model:
        cfg["model"] = args.model
    if args.base_url:
        cfg["base_url"] = args.base_url
    if args.price_in is not None:
        cfg["price_in"] = args.price_in
    if args.price_out is not None:
        cfg["price_out"] = args.price_out
    if not (args.provider or args.key or args.model or args.base_url
            or args.price_in is not None or args.price_out is not None):
        key = cfg.get("api_key", "")
        masked = key[:8] + "…" + key[-4:] if len(key) > 12 else ("(未设置)" if not key else "(太短)")
        print(json.dumps({**cfg, "api_key": masked}, ensure_ascii=False, indent=2))
        print(f"\n配置路径：{CONFIG_PATH}")
        print(f"deepseek 文档：{PROVIDERS['deepseek']['doc']} ｜ kimi 文档：{PROVIDERS['kimi']['doc']}")
        return
    save_config(cfg)
    print(f"✅ 配置已保存到 {CONFIG_PATH}（仅本机；权限 600 仅在 POSIX 系统生效）")
    print(f"   provider: {cfg.get('provider', 'deepseek')}  model: {cfg.get('model', PROVIDERS[cfg.get('provider','deepseek')]['model'])}")


def prepare(args):
    """读取书籍 → 分块 → （必要时）打印预估。返回 (text, chunks, prov, pname)。"""
    _pname, prov = get_provider(load_config())
    text = read_book(args.book)
    chapters = split_chapters(text)
    chunks = pack_chunks(chapters, max_chars=args.chunk_chars)
    return text, chapters, chunks, prov


def cmd_estimate(args):
    text, chapters, chunks, prov = prepare(args)
    est_in, est_out, cost = estimate_cost(text, len(chunks), prov)
    print(f"书名：{args.title}")
    print(f"正文：{len(text):,} 字符 ≈ {len(text)//10000} 万字，识别章节 {len(chapters)} 个，打包为 {len(chunks)} 个处理块")
    print(f"provider：{load_config().get('provider','deepseek')} / {prov['model']}")
    print(f"预估消耗：输入 {est_in/10000:.1f} 万 tokens + 输出 {est_out/10000:.1f} 万 tokens")
    print(f"预估费用：约 ¥{cost:.2f}（单价以官网为准，可在 config 中校准）")
    print("提示：确认费用后 run 实际执行（终端中会要求输入 yes 确认，非交互环境加 --yes）；"
          "同一本书重复处理会命中本地缓存，不再消耗 token。")


def collect_edges(all_chars):
    edges = []
    for c in all_chars:
        for r in c["relations"]:
            edges.append({"from": c["name"], "target": r["target"], "rel": r["rel"], "note": r["note"]})
    return edges


def auto_max_nodes(nchars):
    """按书的体量给默认节点上限：精简版看得清，完整版不漏人。"""
    if nchars < 300_000:
        return 80
    if nchars < 800_000:
        return 120
    return 150


def build_from_data(data, max_nodes):
    """纯代码层：按上限裁剪归并结果并建图。调上限/改样式重跑均为零 token。"""
    merged = data["merged"]
    groups, mapping = data["groups"], data.get("mapping") or {}

    def pick_group(m):
        hint = m["group_hint"] or "其他"
        return mapping.get(hint, hint if hint in groups else groups[0])

    finals = [{
        "name": m["name"], "aliases": m["aliases"], "en": m["en"],
        "group": pick_group(m), "importance": m["importance"],
        "bio": clean_bio(m["bio"]), "mentions": m["mentions"],
    } for m in merged[:max_nodes]]
    graph, report = build_graph(finals, data["raw_edges"], data.get("rel_taxonomy"),
                                data.get("node_first"), data.get("edge_first"),
                                data.get("node_groups"))
    graph["timeline_ranges"] = data.get("chunk_ranges")   # 无则 None，模板自动隐藏时间轴

    dropped = merged[max_nodes:]
    if dropped:
        top = "、".join(m["name"] for m in sorted(dropped, key=lambda m: -m["mentions"])[:5])
        report["被节点上限裁掉"] = (f"{len(dropped)} 人（提及最多：{top}…）；"
                                  f"调大 --max-nodes 重新生成，零 token")
    if data.get("skipped"):
        report["跳过的分块（该部分人物未抽取）"] = data["skipped"]
    return graph, report


def backup_cache(data_path, out_path, title):
    """把图谱数据缓存自动备份到输出目录，与 HTML 并排，便于长期保存/零 token 重出。"""
    try:
        dest = Path(out_path).resolve().parent / f"{re.sub(r'[\\/:*?\"<>|\\s]+', '_', title)}_图谱数据.json"
        if dest.resolve() == Path(data_path).resolve():
            return
        shutil.copyfile(data_path, dest)
        print(f"  图谱数据已备份：{dest}（换电脑/清缓存后用 render --data 零 token 重出）")
    except OSError as e:
        print(f"  [警告] 缓存备份失败（不影响出图）：{e}", file=sys.stderr)


def backfill_timeline(data, digest, chunks):
    """从分块缓存目录为旧缓存补算时间轴字段（零 token）。成功返回 True。"""
    dirs = sorted(CACHE_DIR.glob(f"{digest}_t*_chunks")) \
        or sorted(CACHE_DIR.glob(f"{digest}_chunks"))   # v1.2.0 及更早的目录无分类体系后缀
    if not dirs:
        return False
    files = sorted(dirs[0].glob("*.json"))
    if len(files) != len(chunks):
        print(f"  [提示] 分块缓存 {len(files)} 块 ≠ 书稿重切 {len(chunks)} 块，跳过时间轴补算"
              f"（可用 --chunk-chars 指定原分块字数）", file=sys.stderr)
        return False
    ccl = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    data["chunk_ranges"] = [c["range"] for c in chunks]
    data["node_first"], data["edge_first"] = derive_timeline(ccl, data["merged"])
    data["node_groups"] = derive_status_timeline(ccl, data["merged"],
                                                 data.get("mapping") or {}, data["groups"])
    return True


def cmd_migrate_timeline(args):
    """为旧版 v2 缓存补算时间轴字段（离线，零 token），需指定书稿文件以拿回目范围。"""
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    if data.get("version") != 2:
        die("仅 v2 缓存支持时间轴迁移")
    text = read_book(args.book)
    chapters = split_chapters(text)
    chunks = pack_chunks(chapters, max_chars=args.chunk_chars)
    digest = Path(args.data).stem
    if not backfill_timeline(data, digest, chunks):
        die("迁移失败：找不到分块缓存目录，或分块数不匹配（试试 --chunk-chars 指定原分块字数）")
    bak = Path(str(args.data) + ".bak")
    if not bak.exists():
        shutil.copyfile(args.data, bak)
    Path(args.data).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ 时间轴迁移完成：人物登场 {len(data['node_first'])} 条 · 关系首发 {len(data['edge_first'])} 条 · "
          f"阵营轨迹 {len(data.get('node_groups') or {})} 条")
    print(f"  已回写 {args.data}（原文件备份：{bak}）")
    print("提示：render --data 零 token 重出网页即可看到时间轴。")


def cmd_run(args):
    cfg = load_config()
    text, chapters, chunks, prov = prepare(args)
    est_in, est_out, cost = estimate_cost(text, len(chunks), prov)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    cache_path = CACHE_DIR / f"{digest}.json"

    max_nodes = args.max_nodes or auto_max_nodes(len(text))
    tier = {80: "精简版", 120: "标准版", 150: "完整版"}.get(max_nodes, "自定义")

    print(f"《{args.title}》 {len(text)//10000} 万字 → {len(chunks)} 块")
    print(f"预估：输入 {est_in/10000:.1f} 万 tok + 输出 {est_out/10000:.1f} 万 tok ≈ ¥{cost:.2f}"
          f"（{prov['model']}，单价以官网为准）")
    print(f"节点上限：{max_nodes} 人（{tier}；--max-nodes 可调整，命中缓存后调整零 token）")

    rel_stats = {"dropped_rel": 0, "truncated_rel": 0}  # 仅统计本次实际抽取的分块（命中分块缓存的不重复计数）
    if cache_path.exists() and not args.no_cache:
        print(f"命中本地缓存 {cache_path}，跳过抽取与归并（0 token 消耗）")
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("version") == 2:
            if "node_groups" not in data and backfill_timeline(data, digest, chunks):
                cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                print("  旧缓存已补算时间轴/阵营轨迹字段（零 token）")
            graph, report = build_from_data(data, max_nodes)
        else:  # 旧版缓存：直接渲染
            graph = data
            report = graph.pop("_report", {})
    else:
        # 花费确认（BYOK：只有这条路径会真正调用 API；命中缓存不花钱，无需确认）
        if not args.yes:
            if not sys.stdin.isatty():
                die("即将调用 API 产生费用，但当前是非交互环境（stdin 非 TTY），无法等待确认。\n"
                    "如已确认费用，请显式追加 --yes 重试；或在终端直接运行并输入 yes。")
            try:
                ans = input(f"确认按以上估算调用 API（约 ¥{cost:.2f}）？输入 yes 继续：")
            except EOFError:
                ans = ""
            if ans.strip().lower() not in ("yes", "y"):
                print("已取消，未调用 API。")
                sys.exit(0)
        t0 = time.time()
        all_chars = []
        chunk_chars_list = []   # 按分块顺序保留抽取结果，供时间轴离线推导
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        skipped = []

        # 关系分类体系（本书定制）：先提一份并落盘，其哈希进分块缓存目录名，
        # 保证"同一套分类"的分块缓存才被复用，换分类体系自动重抽，不会新旧混用。
        tax_cache = CACHE_DIR / f"{digest}_taxonomy.json"
        if tax_cache.exists():
            taxonomy = (normalize_taxonomy(json.loads(tax_cache.read_text(encoding="utf-8")))
                        or list(DEFAULT_TAXONOMY))
            print(f"  复用已缓存的关系分类体系：" +
                  "、".join(t["label"] for t in taxonomy))
            tax_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        else:
            taxonomy, tax_usage = propose_taxonomy(args.title, chunks, prov)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tax_cache.write_text(json.dumps(taxonomy, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        total_usage["prompt_tokens"] += tax_usage.get("prompt_tokens", 0)
        total_usage["completion_tokens"] += tax_usage.get("completion_tokens", 0)

        chunk_dir = CACHE_DIR / f"{digest}_t{taxonomy_digest(taxonomy)}_chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        for i, ch in enumerate(chunks, 1):
            cache_file = chunk_dir / f"{i:02d}.json"
            if cache_file.exists():
                print(f"  [{i}/{len(chunks)}] 命中分块缓存 {ch['range']}", flush=True)
                chars = json.loads(cache_file.read_text(encoding="utf-8"))
                all_chars.extend(chars)
                chunk_chars_list.append(chars)
                continue
            print(f"  [{i}/{len(chunks)}] 抽取 {ch['range']}（{len(ch['text'])} 字）…", flush=True)
            try:
                chars, usage, stats = extract_chunk(ch, prov, taxonomy)
                rel_stats["dropped_rel"] += stats["dropped_rel"]
                rel_stats["truncated_rel"] += stats["truncated_rel"]
            except ChunkFailed:
                if args.strict:
                    die(f"严格模式：片段 {ch['range']} 抽取失败，已中止。可去掉 --strict 允许跳过。")
                print(f"  [跳过] {ch['range']} 重试 4 次仍失败，已跳过该片段（加 --strict 可改为中止）",
                      file=sys.stderr)
                skipped.append(ch["range"])
                continue
            cache_file.write_text(json.dumps(chars, ensure_ascii=False), encoding="utf-8")
            all_chars.extend(chars)
            chunk_chars_list.append(chars)
            total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        print(f"  分块抽取完成，共 {len(all_chars)} 人次；开始归并…")
        clusters = cluster_characters(all_chars)
        groups, mapping, merged, merge_usage = merge_clusters(clusters, prov)
        total_usage["prompt_tokens"] += merge_usage.get("prompt_tokens", 0)
        total_usage["completion_tokens"] += merge_usage.get("completion_tokens", 0)
        raw_edges = collect_edges(all_chars)
        data = {"version": 2, "title": args.title, "groups": groups, "mapping": mapping,
                "merged": merged, "raw_edges": raw_edges, "skipped": skipped,
                "rel_taxonomy": taxonomy}
        # 时间轴字段随本次抽取免费获得（离线推导，零 token）
        data["chunk_ranges"] = [c["range"] for c in chunks]
        data["node_first"], data["edge_first"] = derive_timeline(chunk_chars_list, merged)
        data["node_groups"] = derive_status_timeline(chunk_chars_list, merged, mapping, groups)
        real = (total_usage["prompt_tokens"] / 1e6 * prov["price_in"]
                + total_usage["completion_tokens"] / 1e6 * prov["price_out"])
        print(f"  实际消耗：输入 {total_usage['prompt_tokens']/10000:.1f} 万 tok + "
              f"输出 {total_usage['completion_tokens']/10000:.1f} 万 tok ≈ ¥{real:.2f}，"
              f"用时 {time.time()-t0:.0f}s")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  归并结果已缓存：{cache_path}（此后调节点上限/改样式重跑零 token）")
        graph, report = build_from_data(data, max_nodes)

    graph["title"] = args.title
    safe_title = re.sub(r'[\\/:*?"<>|\s]+', "_", args.title)
    out = args.out or f"{safe_title}_人物关系图谱.html"
    render(graph, args.title, out)
    if cache_path.exists():
        backup_cache(cache_path, out, args.title)

    if rel_stats["dropped_rel"] or rel_stats["truncated_rel"]:
        report["抽取阶段丢弃的关系"] = (
            f"非法类型 {rel_stats['dropped_rel']} 条 + 超上限截断 {rel_stats['truncated_rel']} 条"
            "（仅统计本次实际抽取的分块；数字偏大说明分类体系覆盖不全或个别分块输出过猛）")

    print("\n===== 自检报告 =====")
    for k, v in report.items():
        print(f"  {k}：{v}")
    warn = report.get("剔除无关系人物", 0)
    if warn:
        print(f"  提示：{warn} 个人物因没有任何可归边的关系被剔除；如主角缺失，可加大 --max-nodes 重跑。")
    print(f"  提示：改样式可直接编辑生成的 HTML；改数据可用 render 子命令复用缓存，零 token。")


def cmd_render(args):
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    if data.get("version") == 2:
        merged = data["merged"]
        est_chars = sum(m["mentions"] for m in merged) * 450  # 粗略估书体量用于分档
        graph, report = build_from_data(data, args.max_nodes or auto_max_nodes(est_chars))
        print("===== 自检报告 =====")
        for k, v in report.items():
            print(f"  {k}：{v}")
        print()
    else:
        graph = {k: data[k] for k in ("nodes", "edges", "groups")}
    title = args.title or data.get("title") or "书籍"
    out = args.out or f"{re.sub(r'[\\/:*?\"<>|\\s]+','_',title)}_人物关系图谱.html"
    render(graph, title, out, scenes=args.scenes, subtitle=getattr(args, "subtitle", ""))
    if data.get("version") == 2:
        backup_cache(args.data, out, title)


def main():
    ap = argparse.ArgumentParser(description="书籍人物关系图谱生成器（BYOK：DeepSeek / Kimi API）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("config", help="查看或写入 API 配置（仅存本机）")
    c.add_argument("--provider", choices=list(PROVIDERS))
    c.add_argument("--key")
    c.add_argument("--model")
    c.add_argument("--base-url")
    c.add_argument("--price-in", type=float)
    c.add_argument("--price-out", type=float)

    def book_args(sp):
        sp.add_argument("book", help="书稿文件（.txt / .md / .epub）")
        sp.add_argument("--title", required=True, help="书名（用于页面标题）")
        sp.add_argument("--chunk-chars", type=int, default=40000, help="每处理块字数（默认 40000）")

    e = sub.add_parser("estimate", help="只估算 token 与费用，不调用 API")
    book_args(e)
    r = sub.add_parser("run", help="全流程：抽取 → 归并 → 生成图谱网页")
    book_args(r)
    r.add_argument("--out", help="输出 HTML 路径")
    r.add_argument("--max-nodes", type=int, default=None,
                   help="图谱人物上限（默认按书体量分档：<30万字 80 / <80万字 120 / 更多 150；"
                        "命中缓存后调整本参数重新生成为零 token）")
    r.add_argument("--no-cache", action="store_true", help="忽略本地缓存强制重抽")
    r.add_argument("--strict", action="store_true", help="任一分块失败即中止（默认跳过该片段继续）")
    r.add_argument("--yes", action="store_true",
                   help="跳过花费确认直接执行（非交互环境如 agent 宿主必需；命中缓存时不涉及费用）")
    d = sub.add_parser("render", help="用缓存的中间数据重新生成网页（零 token）")
    d.add_argument("--data", required=True, help="缓存 JSON 路径")
    d.add_argument("--title", help="书名")
    d.add_argument("--out", help="输出 HTML 路径")
    d.add_argument("--max-nodes", type=int, default=None,
                   help="节点上限（仅 v2 缓存支持；不传则按书体量分档）")
    d.add_argument("--scenes", help="名场面榜画廊的相对路径（如 scenes/index.html）；提供后页面右上角出现「名场面榜」入口按钮")
    d.add_argument("--subtitle", default="", help="副标题/版本标识（如「· 水浒英雄全图」），显示在标题书名号之后")

    x = sub.add_parser("export-image", help="导出小红书竖版长图（分页 PNG，零 token，复用缓存数据）")
    x.add_argument("--data", required=True, help="缓存 JSON 路径")
    x.add_argument("--title", help="书名")
    x.add_argument("--out-dir", help="输出目录（默认：书名_小红书长图/）")
    x.add_argument("--max-nodes", type=int, default=None,
                   help="导出人物上限（仅 v2 缓存支持；不传则按书体量分档）")

    m = sub.add_parser("migrate-timeline", help="为旧版 v2 缓存补算时间轴字段（离线，零 token）")
    m.add_argument("--data", required=True, help="缓存 JSON 路径")
    m.add_argument("--book", required=True, help="书稿文件（用于拿回目范围，.txt/.md/.epub）")
    m.add_argument("--chunk-chars", type=int, default=40000,
                   help="原 run 时的每块字数（默认 40000，分块数不匹配时调整）")

    t = sub.add_parser("export-top", help="导出「名场面榜」长图：Top N 重磅关系 + 原文摘句 + 回目出处（零 token）")
    t.add_argument("--data", required=True, help="缓存 JSON 路径")
    t.add_argument("--title", help="书名")
    t.add_argument("--book", help="书稿文件（用于摘原文句子；不提供则只出关系榜）")
    t.add_argument("--out-dir", help="输出目录（默认：书名_名场面榜/）")
    t.add_argument("--top", type=int, default=10, help="上榜关系数（默认 10）")
    t.add_argument("--max-nodes", type=int, default=None,
                   help="人物上限（仅 v2 缓存支持；不传则按书体量分档）")
    t.add_argument("--chunk-chars", type=int, default=40000,
                   help="原 run 时的每块字数（需与书稿重切分块数一致）")

    args = ap.parse_args()
    {"config": cmd_config, "estimate": cmd_estimate, "run": cmd_run, "render": cmd_render,
     "export-image": cmd_export_image, "export-top": cmd_export_top,
     "migrate-timeline": cmd_migrate_timeline}[args.cmd](args)


if __name__ == "__main__":
    main()
