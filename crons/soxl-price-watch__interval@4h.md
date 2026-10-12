---
id: soxl-price-watch
title: SOXL/TECL/TQQQ 四小时盯盘
enabled: true
owner: goal:soxl-100
mode: task
schedule:
  kind: interval
  timezone: Asia/Shanghai
  at: 2026-09-23T07:22:35
  every: 4h
metadata:
  tags: [cron:automatic-interval-anchor]
  originating_chat_context_json: '{"chat_id":"c74b6ecd-5b8d-477d-900b-a803ec886340","origin_provider":"main","chat_kind":"direct","event_kind":"message","require_mention":false,"device_id":"6f00387f934ef57e"}'
  presentation_locale: en-US
---
# SOXL/TECL/TQQQ 四小时盯盘（含告警）

## 标的（以 ticker 为准）
用户指定的三只三倍做多 ETF：SOXL、TECL、TQQQ。ticker 是唯一标识；每次从行情站抓取时，报价页会显示基金全称，交叉核验时一并确认 ticker 与基金对应无误——如发现某站点的 ticker 对应了明显不符的基金，弃用该站数据并注明。

## 执行方式（混合：脚本优先，浏览器兜底；告警判定只有一份实现；两阶段提交防丢告警）
1. 每次运行先执行 `python3 ~/workspace/goals/soxl-100/hidden_files/fetch_quotes.py`，解析其 JSON 输出。注意：脚本此时只做判定并写 staging 文件，不更新主状态文件。
2. `status == "market_closed"`（周末 Sat 04:00–Sun 20:00 ET、美股假期）：不抓取、不推送简报。检查 `~/workspace/goals/soxl-100/hidden_files/checks.log` 最后一行：若已包含 `market_closed`（本轮不是休市期第一轮），则追加一行日志后静默结束；否则先推送一条休市通知再追加日志。日志行格式为 `{ISO时间} market_closed weekend` 或 `{ISO时间} market_closed holiday {holiday_name}`（例如 `2026-10-03T15:22:35+08:00 market_closed weekend`）；若文件不存在或最后一行为空，视为第一轮。休市通知文案（英文）：周末用"Weekend — markets closed. No quote updates; briefing resumes next trading session."；假期用"US markets closed today ({holiday_name}). No quote updates; briefing resumes next trading session."（holiday_name 取自脚本输出，已为英文）。不写目标时间线、不写 memory 日志。
3. `status == "ok"`：直接采用其 `briefing`、`alerts`、`quotes`——**判定的告警必须原样推送，不得省略、改写或"再想想"**。`notes` 中如有来源分歧提示，一并如实告诉用户。
4. `status == "needs_browser"`（夜盘时段 20:00–04:00 ET、接口失败或数据过期）：用浏览器按下方数据源/时段覆盖/报价时效要求抓取三只 ETF 最新价（带报价时间戳、所属时段）；把结果写成 JSON 文件（/tmp/quotes.json），格式如下，然后执行 `python3 ~/workspace/goals/soxl-100/hidden_files/fetch_quotes.py --quotes-file /tmp/quotes.json`——脚本统一完成告警判定（只写 staging）；**其判定的告警必须原样推送，worker 不自行计算告警**。
   夜盘（20:00–04:00 ET）来源路由（硬性）：发布 BOATS 夜盘成交价的只有 Yahoo Finance 报价页和 TradingView 图表页，按脚本 `needs_browser` 输出里的 `browser_targets` 地址抓取——TradingView 符号必须带交易所前缀（SOXL=AMEX:SOXL、TECL=AMEX:TECL、TQQQ=NASDAQ:TQQQ），写错前缀会 404（2026-10-05 夜盘轮次出过）。Nasdaq、Google Finance、CNBC、MarketWatch、StockAnalysis 的报价页没有夜盘价，只能用来核对参考收盘，不得算作夜盘的第二源。两家都拿到才算完成夜盘交叉核验；只拿到 Yahoo 一家时按降级处理并注明（脚本也会对 sources 不足 2 家的标的自动记降级 note）。
   JSON 格式：{"etfs": {"SOXL": {"price": 154.37, "change_pct": 4.40, "ref_date": "9/30", "session": "overnight", "quote_time": "00:03", "sources": "Yahoo Finance+TradingView"}, "TECL": {...}, "TQQQ": {...}}}
   session 取值一律英文：premarket / intraday / after-hours / overnight。
   单位硬性约定：所有 change_pct 一律为百分点——4.40 表示 +4.40%，0.23 表示 +0.23%；禁止写小数形式（如 0.0231），否则简报涨跌幅会算错。ETF 的 change_pct 为相对 ref_date 常规收盘的涨跌幅（带符号）。**拿不到涨跌幅数据时填 null（JSON 里写 null，不是 0），脚本会显示 (n/a)，绝不编造数字。**
   防呆自查（2026-10-01 出过把收盘价填成现价的事故）：price 必须是报价页上当前时段的最新成交价，绝不能填参考收盘价。填完后必须心算验证：(price − ref_date收盘价) ÷ ref_date收盘价 应约等于 change_pct；若 price 恰好等于某个已知收盘价，或算出来对不上，说明抓错了，必须重抓，不得将错就错写入文件。
   若脚本返回 `status == "validation_failed"`：绝不推送简报。重开报价页，核对被点名 ticker 的 price/change_pct/ref_date，修正 /tmp/quotes.json 后重跑 `--quotes-file`。若人工复核确认数字确实无误（如市场真正持平），加 `--skip-validation` 重跑并注明原因。
5. 若脚本崩溃（非零退出）或输出不是合法 JSON，按 `needs_browser` 处理。
6. 推送顺序（防丢告警，严格执行）：① 若有告警，先用 tracking.create_entry 在目标时间线上记录（durable 留底）；② 执行 `python3 ~/workspace/goals/soxl-100/hidden_files/fetch_quotes.py --commit` 落盘（失败则重试一次；仍失败就如实注明，下轮会基于旧状态重新判定，可能重复告警、不会漏报）；③ 最后发送简报（告警置顶）。日志与每日小结按原步骤记录。

## 任务
每 4 小时检查一次上述三只 ETF 的最新报价与涨跌幅。交易时段每次运行都推送英文简报（休市时只发一条通知后静默，见执行方式第2条）；任一只 ETF 触发告警条件时按下方规则追加英文提醒。

## 数据源（只读，不登录任何网站）
仅使用主流公开行情站：Yahoo Finance、Nasdaq.com、MarketWatch、CNBC、Bloomberg、Google Finance、Investing.com、TradingView、StockAnalysis.com。每个标的至少 2 家交叉核验（尽可能 3 家；不足时明确降级并注明）。绝不访问券商网站（Robinhood、IBKR、Firstrade、Schwab、富途、老虎、长桥等）。遇到验证码/反爬/机器人检查：直接跳过该源，不尝试绕过。

## 时段覆盖
盘前（04:00–09:30 ET）、盘中（09:30–16:00 ET）、盘后（16:00–20:00 ET）、隔夜（20:00–04:00 ET）——取最新可用报价，注明所属时段与报价时间。来源选择以时间戳新鲜度为准，不取中位数。

## 报价时效（硬要求，2026-10-01 因发出过期报价而设立）
- 每个报价必须核验其时间戳，"最新"必须是真最新。
- 盘中时段：报价时间戳早于当前时间 15 分钟以上视为过期；盘前/盘后/隔夜时段：超过 60 分钟视为过期。
- 过期报价绝不能当作最新价发出：继续换源找更新的；若所有可用源都只有过期报价，如实注明"最新可得报价为 X（N 小时前），可能已滞后"，绝不隐瞒时效问题。
- 搜索结果摘要里的时间（"Just now"/"x hours ago"）指的是抓取时间，不是报价时间，一律以报价页标注的报价时间为准。

## 简报格式（用户定版：英文，严格执行，一字不差）
SOXL 153.62 +3.90% · premarket 05:09 ET
TECL 235.16 +1.01% · premarket 05:09 ET
TQQQ 79.41 +1.77% · premarket 05:09 ET
- 每行固定为：`{TICKER} {price} {±x.xx}% · {session} {HH:MM} ET`，现价保留 2 位小数；session 为 premarket / intraday / after-hours / overnight（机构标准说法）。
- 拿不到涨跌幅数据时显示 `(n/a)`，不编造数字。
- 涨跌幅基准：ETF 一律相对上一个常规交易日收盘价。基准固定，无需每行重复。
- 有告警时，告警（⚠️开头，英文）置顶，简报原文附后；无告警时只发简报。不许加戏、不许改格式、不许省略任何一行。
- 简报以纯文本直接发送，禁止用代码块（```）包裹。

## 告警规则（对 SOXL、TECL、TQQQ 逐只独立判断；告警为英文，含具体数值）
判定逻辑只有一份实现（fetch_quotes.py 内，确定性执行）；浏览器路径下 worker 只负责抓数，判定同样走脚本。
1. 下跌 10% → 加仓提醒（机构标准：相对前一交易日收盘价）。
   - 本次价格 <= 前收 × 0.90 → 触发加仓提醒，内容必须含：现价、跌幅百分比、前收数值、报价时间、数据来源。
   - 每再跌 10% 触发一次（-20%、-30%…形成阶梯）；同一交易日内同一阶梯不重复告警；新交易日重置。
2. 突破历史新高 → 减仓提醒：为每只 ETF 维护 ath（拆股调整后的真实历史最高价，机构标准，见状态文件）。
   - ATH 用拆股调整价（Yahoo 的 high 已拆股调整，直接取历史最大）。机构的价格 ATH 口径只调拆股，不调分红；分红调整是总回报口径，会压低 ATH 导致提前误报。
   - 若该 ETF 尚无 ath 记录：先查其历史最高并存入；若查到且现价已超过该值，同样触发减仓提醒（真实突破）。查不到则本轮不告警、下轮重试。绝不拿首次观测值当历史最高。
   - 本次价格 > ath → 触发减仓提醒，内容必须含：现价、新高数值、报价时间、数据来源；提醒后 ATH 只从历史周线数据重新确认更新（机构收盘确认口径，防盘中假突破），不拿 tick 价直接覆盖 ath。
3. 只提醒，不替用户做任何交易、仓位决定。

## 告警呈现（用户明确要求）
- 任一告警条件满足，必须当轮直接推送消息给用户，不得静默、不得攒到下轮。
- 有告警时，本轮消息以告警内容开头（加 ⚠️ 标识、写清哪只 ETF、触发了哪条、具体数值），常规简报附后，确保用户一眼看到，不会错过。

## 执行要求（用户明确，不可打折）
- Token 预算充足：宁可多查几个源、把交叉验证做实，也不要为省 token 跳过步骤、压缩核验或编造数据。2 家交叉核验是硬要求（尽可能 3 家）。
- 单个源打不开、被墙、返回 500/403/404 是常态：换下一个源继续，不要因此放弃整轮。只有可用源不足 2 家时才算本轮降级，此时仍用已有数据给出结论，并如实注明哪些源不可用、结论可信度打折。
- 浏览器任务若首次失败：换方式重试一次（例如改用 live browser 任务直读），重试仍失败再降级，禁止"试一次不行就交白卷"。
- 告警条件一旦满足必须触发推送，禁止为"省事"而静默。只有"无任何条件触发"时才只发简报不告警。

## 状态文件
`/home/hatch/workspace/goals/soxl-100/hidden_files/soxl-watch.json`，结构：
{"etfs": {"SOXL": {"dip_level": 0, "dip_ref": "", "ath": 302.00}, "TECL": {"dip_level": 0, "dip_ref": "", "ath": 275.40}, "TQQQ": {"dip_level": 0, "dip_ref": "", "ath": 88.09}}, "updated_at": "..."}
由脚本统一读写（staging + 原子 commit 两阶段）；worker 不直接改状态文件。告警触发或 ath 更新时写入文件。绝不编造价格，无法核验的跳过该条件并注明缺口。

## 记录
运行日志追加到 `~/workspace/goals/soxl-100/hidden_files/checks.log`，并在 `~/memory/YYYY-MM-DD.md` 追加一条摘要。`market_closed` 的轮次只追加一行日志，不写 memory 日志。

## 目标时间线（保持目标状态新鲜）
- 告警触发时：用 tracking.create_entry 在目标 goal_a618587384a2 上记录告警内容（标题写清哪只 ETF 触发了哪条，英文）。
- 每天第一次运行（以北京时间日期为准；先检查 hidden_files/checks.log，若今日已有记录则跳过）：用 tracking.create_entry 在目标 goal_a618587384a2 上记一条英文一句话小结，例如"Oct 1 recap: SOXL 145.48 (+1.2%), TECL 96.30 (+0.8%), TQQQ 88.15 (-0.3%), no alerts"。其余运行只写日志文件，不写时间线。
