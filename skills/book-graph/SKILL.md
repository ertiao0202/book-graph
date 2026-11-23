---
name: book-graph
description: 上传书籍文件（txt/md/epub），用用户自己的 DeepSeek 或 Kimi API key 自动抽取人物与关系，生成可发布分享的单文件人物关系图谱网页。触发词：人物关系图谱、把这本书做成关系图、生成图谱、book graph、上传书籍做图谱、这本书的人物关系；用户上传书稿文件并提到关系图/图谱/可视化时启用。
---

# 书籍人物关系图谱生成器（BYOK）

把一整本书变成一张可交互、可发布的人物关系图谱网页。核心资产是 `scripts/` 下的
流水线脚本与 `prompts.py` 里的抽取提示词（以 39 万字人物传记全流程产出为基准固化）。

## 铁律

1. **API key 永远用用户自己的**：只从本机配置 `~/.config/book-graph/config.json`
   （或环境变量 `BOOK_GRAPH_CONFIG` 指定的路径）读取；key 只写入该文件（权限 600），
   不得打印完整 key、不得发往任何其他地址。脚本直连官方 API，不经第三方服务器。
2. **先估算、后运行**：正式抽取前必须先跑 `estimate` 把 token 量和费用告诉用户，
   等用户确认再跑 `run`。
3. **脚本的调用方式**：`python3 scripts/book_graph.py ...`（插件包挂载无执行位，
   禁止 `./scripts/book_graph.py`）。
4. 工作目录随意，脚本内部用 `__file__` 定位模板与提示词，不受 cwd 影响。

## 首次使用（配置 key）

用户没配置过 key 时，引导执行（二选一或都配）：

```bash
python3 scripts/book_graph.py config --provider deepseek --key sk-用户的key
python3 scripts/book_graph.py config --provider kimi --key sk-用户的key --model kimi-k2-0905-preview
```

- provider 切换：`config --provider kimi`；查看当前配置：`config`（不带参数，key 打码显示）。
- deepseek 文档 https://platform.deepseek.com ；kimi 文档 https://platform.moonshot.cn 。
- 若报模型不存在（404），让用户去文档页查最新模型名，用 `--model` 更新。

## 标准工作流

```bash
# 1) 估算（不调 API，免费）
python3 scripts/book_graph.py estimate 红楼梦.txt --title "红楼梦"

# 2) 用户确认费用后，全流程（抽取→归并→生成网页）
python3 scripts/book_graph.py run 红楼梦.txt --title "红楼梦"

# 3) 想换样式/改输出，用缓存重渲染，零 token
python3 scripts/book_graph.py render --data ~/.cache/book-graph/<hash>.json --title "红楼梦"

# 4) 导出小红书竖版长图（分页 PNG，零 token，需要 Pillow）
python3 scripts/book_graph.py export-image --data 红楼梦_图谱数据.json --title "红楼梦"

# 5) 旧缓存补齐时间轴数据（v1.4.0，离线零 token，需书稿文件拿回目范围）
python3 scripts/book_graph.py migrate-timeline --data ~/.cache/book-graph/<hash>.json --book 红楼梦.txt

# 6) 导出「名场面榜」（v1.6.0）：Top 10 重磅关系 + 原文摘句 + 回目出处，零 token
python3 scripts/book_graph.py export-top --data ~/.cache/book-graph/<hash>.json --book 水浒传.txt --title "水浒传"

# 7) 发布与分享（v1.7.0）：重出带名场面榜入口的页面 → 组包 → kimix 发布，零 token
python3 scripts/book_graph.py render --data 书名_图谱数据.json --title "书名" --scenes scenes/index.html --out index.html
```

第 7) 步「发布与分享」标准流程（页面右上角「分享」按钮由模板自带，仅 http(s)
在线打开时显示，本地双击自动隐藏）：

1. **组包**：分享包目录 = `index.html`（图谱页）+ `scenes/index.html`（名场面榜
   画廊页，链回 `../`）+ `scenes/*.png`（名场面榜长图）。画廊页 links 必须写
   显式 `scenes/index.html`（kimix 静态托管不提供目录索引，`/scenes/` 裸路径 404）；
   画廊页内的 `<img src>` 是相对画廊页自身所在目录写的（直接写文件名，不要再带
   `scenes/` 前缀），中文文件名建议 URL 编码，否则浏览器侧可能解析失败。
2. **压缩**：zip 的根目录必须直接是 `index.html`，不能套一层 dist/（Windows 无
   zip 命令时用 Python zipfile，包体需 < 64 MiB）。
3. **校验**：`kimix website validate static <bundle>.zip`，有错先修再往下。
4. **发布**：`kimix website create static <bundle>.zip --app-name "书名" --visibility public --wait`。
   公开/私密必须显式 `--visibility`；首次 401 时先 `kimix auth login`。
5. **验证**：读回 website-id、URL、published_version_id、visibility；再匿名抓
   canonical URL 确认含本书唯一标记，抓 `scenes/index.html` 与 PNG 确认 200。
6. **记录**：把 title、website_id、url、visibility、version_id、包体 sha256、时间
   追加到用户归档目录的 `发布记录.json`。
7. **重发**：内容更新后用 `kimix website publish <website_id> static <bundle>.zip --wait`
   发新版——**URL 不变**、可追溯、可 `kimix website rollback` 回滚；访问权限用
   `kimix website visibility <website_id> public|private` 单独切换，不用重发包。

`run` 会打印逐块进度、实际 token 消耗与花费、自检报告。最终产出
`书名_人物关系图谱.html`：单文件、可双击打开、可上传任意静态托管、已适配手机端
（双指缩放、图例折叠、触屏无悬停提示框）。图谱页交互：点阵营图例高亮该阵营
（其余压暗，再点恢复）；点右侧关系类型图例只看该类关系（再点恢复），两者可叠加；
手机端关系图例收在"图例筛选"面板里。v1.4.0 起新增：搜索框输入「甲→乙」查两人
最短关系链（路径高亮、链路卡每一跳都可点开档案）；底部时间轴滑块或 ▶ 播放，
按分块回目范围逐步展开关系网络，新登场人物带生长动画（时间轴数据由分块缓存
离线推导；2026-11 前的旧缓存需 migrate-timeline 补算一次）。v1.5.0 起时间轴播放
时节点颜色随"当时所处阵营"演变——谁何时落草、何时流落江湖，一眼可见（阵营轨迹
同样由分块缓存的 group_hint 离线挖掘，零 token）。

`export-image` 基于同一份缓存数据导出 1080×1440（3:4）分页长图到
`书名_小红书长图/`：首页带书名/统计/阵营一览，每页含阵营分区标题与人物卡片
（姓名/阵营/简介/关系 top3），纯本地 PIL 绘制，找不到中文字体会明确报错。布局自动适配：分组 2-10 个且无绝对
主导组时按阵营环形聚团，否则退回普通力导布局；仿真结束自动缩放至全图可见。
搜索支持常用名、英文名、别称（如"阿瞒"能找到曹操）和简介，搜索提示自动用
本书头部人物举例。

## 关系分类体系（按书定制，v1.3.0）

不同体裁的书人物关系完全不同（征战演义、世情家族、武侠、职场、传记……），
所以关系类型**不是一套写死的词表**：`run` 处理新书时，会先读开头/中段/结尾样章
让模型为这本书量身定制 6-12 个关系类型（代码 + 中文显示名 + 实/虚线 + 定义，
如红楼用「主仆/隶属」「请托/引荐」「嫌隙/争竞」，三国用「招募/招揽」「兼并/从属」），
之后所有分块抽取都严格使用本书这套分类。图例、颜色、虚线说明全部由数据驱动。

- 分类体系随主缓存保存（`rel_taxonomy` 字段），`render`/`export-image` 重出自动沿用；
- 体系哈希进分块缓存目录名，换体系会自动重抽，不会与旧分块缓存混用；
- 旧版缓存（无 `rel_taxonomy`）渲染时回落内置默认词表，行为与 v1.2.0 一致；
- 用户想微调类型名称：直接改缓存 JSON 里的 `rel_taxonomy` 再 `render`，零 token；
- 分类体系定制失败（API 异常）时自动退回默认词表，不中断流程。

## 节点上限（用户可选）

`--max-nodes` 默认按书体量分档：30 万字以内 80 人、30-80 万字 120 人、80 万字
以上 150 人（对应精简版/标准版/完整版）。上限只在建图前裁剪，**调大调小都不增加
token 消耗**——归并结果缓存后，反复生成不同版本零成本。自检报告会列出被裁掉的
人物（提及最多的前几名），供用户决定是否加人。

## 缓存规则

同一本书（按正文哈希）重复 `run` 自动命中 `~/.cache/book-graph/` 缓存：分块抽取
结果与归并结果分开缓存，命中后零 token 出图；`--max-nodes` 调整、render 重出均
零 token。用户改了提示词想重抽时才加 `--no-cache`。

**自动备份**：`run` 和 `render` 成功后，会把自包含的图谱数据缓存（v2）复制到
输出目录，命名 `书名_图谱数据.json`，与 HTML 并排。换电脑、清缓存、沙箱重置后，
用它直接 `render --data 书名_图谱数据.json` 零 token 重出任意节点上限版本，
不必重新抽取。

## 读懂自检报告

- `被节点上限裁掉`：名单里若有用户在意的人物，调大 `--max-nodes` 重出（零 token）；
- `剔除无关系人物` 较多时：提示用户可加大 `--max-nodes`；
- `无法归边的关系提及` 较多时：多为抽取阶段人物名不统一或目标人物被上限裁掉，属正常现象；
- `抽取阶段丢弃的关系`：模型输出的 rel 代码不在本书分类体系中的条数 + 单角色超 20 条被截断的
  条数。数字偏大说明分类体系覆盖不全（可考虑在缓存 JSON 里补类型后重出，零 token）；
- 主角缺失/明显漏人：换更长 `--chunk-chars`（默认 40000）或换 provider 重试。

## 边界与失败处理

- 支持 `.txt / .md / .epub`；**扫描版 PDF 不支持**，用户上传 PDF 时直接说明。
- 编码自动尝试 utf-8 / gb18030 / big5；全部失败时报错让用户转码。
- 正文不足 5000 字符视为"不是一本书"，报错。
- API 重试 3 次仍失败：把错误原文转告用户（欠费、限流、key 失效），不要擅自换 key。
- 价格默认值可能与官网有出入，估算仅供参考；脚本会在 config 里让用户可校准
  `--price-in / --price-out`。

## 输出网页的二次加工

用户想调整谱图样式时，直接编辑生成的 HTML（搜索 `:root` 改配色、`const GROUPS`
改分组）。改数据（加人、改关系）建议改缓存 JSON 后用 `render` 重出，零成本。
