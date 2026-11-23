# book-graph · 书籍人物关系图谱生成器（BYOK）

上传一本书（txt / md / epub），用**你自己的** DeepSeek 或 Kimi API key，自动完成
章节分块 → LLM 抽取（人物 / 关系 / 阵营 / 权重）→ 全局归并 → 力导向图网页渲染，
产出一张可交互、可发布分享的单文件人物关系图谱网页。

**纯 Python 3 标准库实现，不依赖 Kimi、不依赖任何第三方运行时**；
仅导出小红书长图（`export-image`）时需要可选依赖 Pillow。
API key 只写入本机 `~/.config/book-graph/config.json`（权限 600），
脚本直连官方 API，不经任何第三方服务器。

## 在线示例

样例数据不进仓库，直接看线上成品（手机端可开）：

- [水浒传 · 英雄全图](https://shuihu.ok.kimi.link)
- [红楼梦人物关系图谱](https://hongloumeng.ok.kimi.link)
- [三国演义人物关系图谱](https://sanguo-graph.ok.kimi.link)
- [西游记人物关系图谱](https://xiyouji-graph.ok.kimi.link)
- [产品落地页](https://guanxitu.ok.kimi.link)

## 两条使用路径

### 路径 A：裸 CLI（任何环境，只要有 Python 3）

```bash
git clone https://github.com/ertiao0202/book-graph.git
cd book-graph

# 1) 配置你自己的 API key（二选一或都配）
python3 scripts/book_graph.py config --provider deepseek --key sk-你的key
python3 scripts/book_graph.py config --provider kimi --key sk-你的key --model kimi-k2-0905-preview

# 2) 估算 token 与费用（不调 API，免费）
python3 scripts/book_graph.py estimate 红楼梦.txt --title "红楼梦"

# 3) 确认费用后，全流程：抽取 → 归并 → 生成网页
python3 scripts/book_graph.py run 红楼梦.txt --title "红楼梦"

# 4) 用缓存零 token 重出 / 换样式
python3 scripts/book_graph.py render --data ~/.cache/book-graph/<hash>.json --title "红楼梦"

# 5) 导出小红书竖版长图（分页 PNG，纯本地，需要 Pillow）
python3 scripts/book_graph.py export-image --data 红楼梦_图谱数据.json --title "红楼梦"

# 6) 导出「名场面榜」：Top 10 重磅关系 + 原文摘句 + 回目出处（零 token）
python3 scripts/book_graph.py export-top --data ~/.cache/book-graph/<hash>.json --book 水浒传.txt --title "水浒传"
```

产出 `书名_人物关系图谱.html`：单文件、双击可看、可传任意静态托管。

### 路径 B：Kimi 插件（推荐 Kimi Work 桌面用户）

本仓库就是 Kimi Work / Kimi 桌面端 book-graph 插件的完整源码：

- `kimi.plugin.json` — 插件清单（名称、版本、能力声明）
- `skills/book-graph/SKILL.md` — 插件技能说明（Agent 的工作流指引）
- `skills/book-graph/scripts/` — 与 `scripts/` 同一份流水线代码

在 Kimi Work 桌面端安装 book-graph 插件后，直接对 Agent 说
「把这本书做成人物关系图谱」即可：Agent 会引导你配置 key、先估算费用、
再运行全流程，并附自检报告。

## 主要特性

- **BYOK**：key 只存本机，权限 600，绝不外发；支持 DeepSeek 与 Kimi 双 provider
- **先估算后运行**：正式抽取前必先看 token 量与费用
- **按书定制关系分类体系**：先读样章为这本书量身设计 6–12 个关系类型
  （红楼是主仆 / 请托 / 嫌隙，三国是招募 / 兼并 / 对弈），图例、颜色、虚线全数据驱动
- **本地缓存**：同一本书（按正文哈希）重复处理零 token；`--max-nodes` 调整、
  `render` 重出均零成本；每次 `run`/`render` 自动备份自包含数据文件
  `书名_图谱数据.json`，换机器后可直接重出
- **交互图谱**：阵营高亮、关系类型筛选、人物搜索（支持别称，如「阿瞒」找到曹操）、
  「甲→乙」最短关系链查询、底部时间轴滑块 / 播放看关系网络随回目生长、
  时间轴播放时节点颜色随当时所处阵营演变
- **自检报告**：列出被节点上限裁掉的人物、无法归边的关系等，便于决策
- **边界说明**：支持 `.txt / .md / .epub`；扫描版 PDF 不支持；正文不足 5000 字符
  视为「不是一本书」

## 依赖

- Python 3（仅用标准库：`argparse / urllib / json / html.parser / zipfile / pathlib` 等）
- 可选：Pillow（仅 `export-image` 长图导出需要）

## 配置

- 配置文件：`~/.config/book-graph/config.json`（可用环境变量 `BOOK_GRAPH_CONFIG` 改路径）
- key 打码查看：`python3 scripts/book_graph.py config`（不带参数）
- 模型名以各家平台文档为准；报 404 时用 `--model` 更新

## 许可证

MIT · © 张巍，见 [LICENSE](LICENSE)
