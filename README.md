# soxl-etf-monitor

每 4 小时盯盘三只三倍做多 ETF（SOXL / TECL / TQQQ）的报价与告警监控。

## 做什么

- 每 4 小时（Asia/Shanghai）抓取 SOXL、TECL、TQQQ 最新报价与涨跌幅，覆盖盘前、盘中、盘后、夜盘四个时段，推送三行英文简报。
- 休市（周末、NYSE 假期）只发一条休市通知，之后静默。

## 告警规则（逐只独立判断）

- **加仓提醒**：任一只较前一交易日收盘价下跌 10% 触发；每再跌 10%（-20%、-30%…）再触发一档，同一交易日同一档不重复，新交易日重置。
- **减仓提醒**：任一只突破历史新高（拆股调整口径的真实 ATH）触发；ATH 只从历史周线数据重新确认，不拿盘中 tick 价覆盖。
- 只提醒，不做任何交易或仓位决定。

## 数据源

只用主流公开行情站交叉验证（每只至少 2 家，尽可能 3 家）：Yahoo Finance、Nasdaq、MarketWatch、CNBC、Bloomberg、Google Finance、Investing.com、TradingView、StockAnalysis.com。绝不访问券商网站。

- 美东 04:00–20:00：脚本走 Yahoo Finance chart API + Nasdaq API + CNBC quote API。
- 夜盘 20:00–04:00 ET：免费接口无数据，脚本返回 `needs_browser`，由浏览器读 Yahoo Finance 报价页与 TradingView 图表页（唯一发布 BOATS 夜盘价的两家；TradingView 符号须带交易所前缀：`AMEX:SOXL`、`AMEX:TECL`、`NASDAQ:TQQQ`），抓到的报价回脚本统一判定。来源不足 2 家自动标注降级。

## 文件

- `fetch_quotes.py` — 唯一的行情抓取 + 告警判定实现。默认只判定并写 staging 文件，推送后 `--commit` 才落盘主状态（两阶段提交，防丢告警）。状态文件 `soxl-watch.json` 记录每只的 `dip_level` / `dip_ref` / `ath`，属运行时产物，不入库（见 `.gitignore`）。
- `crons/soxl-price-watch__interval@4h.md` — 定时任务定义（每 4 小时的执行说明：数据源、报价时效、简报格式、告警呈现与记录要求）。

## 用法

```bash
python3 fetch_quotes.py                 # 判定并写 staging，JSON 输出到 stdout
python3 fetch_quotes.py --commit        # 推送完成后落盘主状态
python3 fetch_quotes.py --quotes-file /tmp/quotes.json  # 浏览器路径：对抓到的报价统一判定
```
