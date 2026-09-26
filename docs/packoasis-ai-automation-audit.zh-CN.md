# PackOasis AI 自动化审计与"一键估价 → 直接下单"落地

更新日期：2026-09-26

## 1. 结论速览

- **仓库**：后端（Medusa）就在本仓库 `apps/backend`。`apps/storefront` 是已废弃的副本（见 `apps/storefront/DEPLOY-DEPRECATED.md`），线上前端在 `yangyangnovelist-hub/packoasis-storefront`，当前 Claude GitHub App 无权访问该仓库。前端改动已做成补丁 `docs/patches/packoasis-storefront-instant-quote.patch`，可直接 `git apply`。
- **审计前**：网站没有任何 AI 自动化（没有 LLM 调用、没有自动报价、没有自动联系客户），询价表单提交不出去，后端按当前代码无法启动，RFQ 也没有路径变成订单。
- **现在**：客户点页面上的 "Request a Quote / Submit a quick quote"（或右下角 Instant quote），0.1–0.2 秒出价，改尺寸/数量/工艺实时重算，填姓名邮箱后直接进入结账。实测：
  - 浏览器自动化：从点击询价到订单确认 **5.6–6.6 秒**（共 9 次运行全部通过，最终代码上 4 次为 5.6 / 5.7 / 5.9 / 5.9 秒）
  - 按真人节奏模拟（逐字输入约 250 个字符，每步停顿阅读 4 秒）：**80.7 秒**
  - 纯 API 链路（报价 → 购物车 → 地址 → 配送 → 支付 → 下单）：**1.5 秒**
- **自动联系与线索情报**：报价邮件即时发出；访客浏览轨迹、公司官网信息、AI 画像与 A–D 评分汇总成一封销售提醒；未下单的报价在 24h / 72h 自动跟进（可一键退订）；下单后自动发订单确认并记录"报价到下单用了几分钟"。
- **SEO / GEO**：新增幂等的优化器 `scripts/optimize-seo-geo.mjs`，一次处理全部 399 个镜像页，并生成 robots.txt（放行主流 AI 爬虫）、sitemap.xml、llms.txt、llms-full.txt；后端提供免密钥的报价 API 与 OpenAPI 描述，AI 助手可直接给用户报价并附上一键下单链接。
- **需要你拍板的事**见第 6 节，最重要的两件：**校准价格表**（默认价是按公开市场价估算的占位值）和**清理 PakFactory 品牌残留**（logo、电话、评价）。

## 2. 审计发现（按优先级）

### P0：阻断业务

| # | 问题 | 证据 | 状态 |
|---|------|------|------|
| 1 | 后端按当前代码启动即崩溃：`medusa-config.ts` 给 PostHog 传 `apiKey/apiUrl`，而 `@medusajs/analytics-posthog` 读取 `posthogEventsKey`，缺失即抛 "Posthog API key is not set"（seed、develop、start 都失败） | 原 `apps/backend/medusa-config.ts:77-91` | 已修复：有 key 才注册 PostHog，否则用官方 analytics-local |
| 2 | `medusa build` 因 `populate-packoasis-products.ts` 类型错误退出码为 1，部署构建会失败 | `src/scripts/populate-packoasis-products.ts:50,75`（`status: "published"`） | 已修复（`ProductStatus.PUBLISHED`） |
| 3 | 询价表单提交不出去：`/us/contact-us` 重定向到镜像页 `contact-us.html`，其表单 `action='#'` 且页面无任何脚本；React 版表单 `ContactTemplate` 未被任何路由使用 | `public/contact-us.html:1866`，`src/app/[countryCode]/(main)/contact-us/page.tsx:19` | 已修复：组件接管旧表单并提交到 `/store/rfqs`（带 project draft），显示回执 |
| 4 | 没有任何 AI 自动化、没有自动报价，RFQ 状态只能后台手改，没有 RFQ → 购物车/订单的路径 | 全仓库无 LLM 调用；`src/modules/rfq/service.ts` 仅状态机 | 已实现（见第 3 节） |
| 5 | 镜像页头 logo 图片就是 PakFactory 商标，390 页；页头电话 1-888-622-2819（PakFactory 的号码），393 页 | `public/media.packoasis.com/logo/websites/1/logo1.jpg` | **需你提供 PackOasis logo/电话**；优化器 `--logo` 可一次替换全部页头 logo |
| 6 | 38 页仍有 PakFactory 客户评价原文（如 "I used Pakfactory for my jewelry boxes…"），以及 "4.5 stars on Google Reviews / 5,000+ customers" 等背书，存在虚假评价与商标法律风险 | 优化器报告 `pakfactoryMentions` | **需你删除或替换为真实评价**（未自动改写，避免捏造内容） |
| 7 | 导航里的 coffee / ecommerce / pet packaging 三个商品页是 HTTrack 跳转桩，meta refresh 指向自身，打开即无限刷新 | `public/custom-coffee-packaging.html/index.html` 等 | 优化器已改为跳转到最接近的现有页面（pouches / corrugated） |

### P1：严重影响转化 / 获客

| # | 问题 | 状态 |
|---|------|------|
| 8 | `rfq-analytics` 订阅器监听 `rfq.created`，但自定义模块实际发出的是 `rfq.rfq.created`（实测），从未触发；且 PostHog `track` 缺少必填的 `actor_id` | 已修复 |
| 9 | 没有任何邮件：Notification 模块未配置 provider，Medusa 默认也不发订单确认 | 已实现：Resend / SendGrid / 本地日志自动选择 |
| 10 | 后台看不到询盘（只有 API） | 已新增 Admin 页 "Leads & RFQs" |
| 11 | SEO：351 页 canonical / og:url 是相对地址；没有 robots.txt、sitemap.xml（`next-sitemap.js` 未安装、`siteUrl` 用的是 Vercel 变量）；没有 Organization / FAQPage / Product 报价结构化数据 | 优化器已处理 |
| 12 | GEO：没有 llms.txt、没有 AI 爬虫策略、页面上没有可被引用的价格信息 | 优化器 + 公开报价 API 已处理 |
| 13 | 98 张商品页横幅背景图从未被镜像抓取，白色标题落在白底上看不见 | 优化器加了深色兜底底色并在报告中列出缺图清单；**需要重新抓取或替换图片** |
| 14 | 镜像页引用的 Magento requirejs 模块（jquery、mage/bootstrap 等）不存在，控制台大量报错，轮播等交互失效；`contact-us.html` 文件被截断（没有 `</body></html>`） | 已记录，建议后续逐页替换为原生组件 |
| 15 | 后端未配置 Stripe，只有手动支付（线下开票） | 已支持：设置 `STRIPE_API_KEY` 即启用，初始化脚本会把 Stripe 挂到区域上 |

### P2：隐患

- `JWT_SECRET` / `COOKIE_SECRET` 缺省为 `supersecret`（`medusa-config.ts`），生产必须在 Railway 设置。
- `apps/backend/yarn.lock` 只有 94 条记录，完整的是 `package-lock.json`（npm）；仓库同时声明 Yarn 4，锁文件格式却是 v1。本次新增依赖同步更新了两个锁文件，建议统一只保留 npm。
- storefront `next.config.js` 忽略 TS/ESLint 错误，现存 19 个 TS 错误。
- `/store/project-drafts/current` 可凭 draft_id 读取草稿（ID 不可猜测，风险低）。

## 3. 实现了什么

```mermaid
flowchart LR
  A[任意页面<br/>Request a Quote / Instant quote] -->|0.1s| B[即时报价组件<br/>按页面自动选品类]
  B -->|改规格实时重算| B
  B -->|自由描述| AI1[Claude 解析规格<br/>无 key 时正则兜底]
  AI1 --> B
  B -->|姓名+邮箱| C[/store/instant-quote/order<br/>服务端重新计价]
  C --> D[RFQ + Quote<br/>SUBMITTED→QUOTED]
  C --> E[Medusa 购物车<br/>锁定价格的自定义行项目]
  E --> F[storefront /api/quote-checkout<br/>写入购物车 cookie]
  F --> G[结账：地址 → 运费已含 $0 → 支付 → 下单]
  G --> H[order.placed<br/>RFQ→ACCEPTED→ORDERED<br/>订单确认+销售提醒]
  C -. 事件 .-> L[线索流水线<br/>报价邮件 → 浏览轨迹 → 官网抓取 → AI 画像评分 → 销售提醒]
  L -.24h/72h 未下单.-> M[AI 跟进邮件 + 恢复报价链接]
```

### 3.1 即时报价引擎（确定性，价格绝不交给 LLM）

`apps/backend/src/lib/instant-quote/`：13 个品类（邮寄盒、运输箱、折叠彩盒、精品盒、自立袋、纸袋、标签、拷贝纸、内托、快递袋、定制胶带可即时下单；地堆展架、铁盒转人工复核）。

价格 = (面积 × 材料单价 + 基础成本 + 印刷 + 工艺 + 附加件) × 批量折扣 + 刀模/制版等一次性费用，再乘毛利系数；加急 ×1.2、交期 ×0.6。输出单价、总价、阶梯价、生产交期、预计送达日期、报价有效期（14 天）。默认价（MOQ 下）：

| 品类 | MOQ | MOQ 单价 | 10×MOQ 单价 | 生产期（工作日） |
|------|-----|---------|------------|----------------|
| 邮寄盒 10×8×4 in 全彩 | 500 | $4.17 | $2.20 | 10–15 |
| 运输箱 12×10×8 in 单色 | 500 | $2.98 | $1.91 | 10–15 |
| 折叠彩盒 4×2×6 in 全彩 | 1,000 | $0.91 | $0.31 | 8–12 |
| 精品盒 8×6×3 in 全彩 | 500 | $5.07 | $2.96 | 15–22 |
| 自立袋 6×9×3 in 全彩 | 10,000 | $0.38 | $0.23 | 12–18 |
| 纸袋 10×5×13 in 单色 | 1,000 | $1.49 | $0.95 | 12–18 |
| 标签 3×3 in 全彩 | 2,000 | $0.17 | $0.07 | 5–8 |

所有数字集中在 `catalog.ts`，生产环境用环境变量 `INSTANT_QUOTE_PRICING_JSON` 深度覆盖（无需改代码），例如：

```bash
INSTANT_QUOTE_PRICING_JSON='{"margin_multiplier":1.5,"product_types":{"mailer-box":{"moq":300,"base_unit_cost":0.4}}}'
```

### 3.2 前端组件（后端直接下发，前端只需一个 script 标签）

`GET /packoasis/widget.js`（源码 `src/lib/widget/widget-client.ts`，Shadow DOM 隔离样式）：

- 自动接管页面上已有的 "Request a Quote / Submit a quick quote / Get started" 按钮，并提供右下角浮动按钮；打开即按当前页面品类出价（真正"一键估价"）。
- 数量快捷档、阶梯价点选、工艺/加急勾选实时重算；"Describe your project" 自由描述由 Claude 解析成规格。
- 结账、账户页自动静默；尊重 Do Not Track / Global Privacy Control（只报价，不埋点）。
- 支持 `?po_quote=`（AI 助手发来的预填深链）与 `#instant-quote` 自动打开。
- 接管旧 Zoho 表单，提交到 `/store/rfqs`，展示 project drawer 里的草稿。

### 3.3 下单闭环

- `POST /store/instant-quote/order`：服务端重新计价 → 建 RFQ（`instant_quote`）+ Quote（`packoasis-instant`）→ 建 Medusa 购物车（锁定价格、`requires_shipping: false` 的自定义行项目）→ 返回同源结账链接。非美国地址、需结构评审的品类自动转人工复核（不建购物车）。
- storefront `GET /api/quote-checkout`：写入 `_medusa_cart_id`（SameSite=Lax，保证从邮件跳转也有效）并进入结账地址页。
- `src/scripts/setup-instant-quote.ts`（幂等）：确保美国区域/税区、仓库、服务区和 **"Freight included in your quote" $0 配送选项**。
- `order.placed` 订阅器：RFQ → ACCEPTED → ORDERED，发订单确认与销售提醒，记录"报价到下单分钟数"。
- 邮件里的"完成订单"走 `/packoasis/resume`（HMAC 签名）：报价过期会按现价自动重算、换新购物车。

### 3.4 AI 与线索情报

- 模型：`claude-opus-5`（`PACKOASIS_AI_MODEL` 可改），通过官方 `@anthropic-ai/sdk` 调用，结构化输出（zod schema），并默认开启服务端回退 `fallbacks: "default"`（被安全分类器拒答时自动由 Anthropic 推荐模型接手）。
- 三处用 AI：解析客户自由描述（effort low，面向客户需低延迟）、线索画像与 0–100 评分（行业、规模、可能需求、下一步动作、个性化开场白）、跟进邮件文案（禁止出现价格/链接，价格区块由模板确定性渲染）。
- 任一 AI 调用失败（无 key、拒答、超时、限流）都会回退到确定性逻辑，流程不中断。
- 浏览轨迹：组件以首方匿名 `po_vid` 记录页面浏览、滚动深度、报价交互、UTM 来源（不存 IP），提交询价时关联到线索；默认保留 180 天后自动清理。
- 公司信息补全：只读取客户自己填写的网站或企业邮箱域名的**公开首页**（免费邮箱跳过），遵守 robots.txt；防 SSRF（仅 http(s) 80/443、连接时校验解析 IP、重定向逐跳校验、限时限大小）。

### 3.5 自动联系客户

| 邮件 | 触发 | 收件人 |
|------|------|--------|
| 即时报价单（含"完成订单"按钮、阶梯价） | 下单请求提交后即刻 | 客户 |
| 请求已收到（含参考预算） | 需人工复核的品类 / 联系表单 | 客户 |
| 线索提醒（评分、理由、浏览轨迹、官网摘要、下一步） | 同上 | `SALES_NOTIFY_EMAIL` |
| 跟进 #1 / #2（AI 文案） | 报价后 24h / 72h 仍未下单 | 客户（带 List-Unsubscribe 一键退订） |
| 订单确认 / 新订单提醒 | `order.placed` | 客户 / 销售 |

### 3.6 SEO / GEO 自动优化

`node scripts/optimize-seo-geo.mjs <public 目录> --backend <后端> --publishable-key <pk> [--logo <PackOasis logo>]`，幂等，可在每次部署前运行（`--check` 用于 CI）。对当前镜像的实测结果：

- 399 页：351 页修正 canonical / og:url 为绝对地址，18 页（账户、结账、哈希重复页）加 noindex
- 42 页生成 FAQPage，41 个商品页生成 Product + AggregateOffer（起价/MOQ 价来自实时报价引擎），并在横幅里加一行可被抓取引用的价格说明
- 修复 3 个自刷新死循环页；sitemap 375 条
- robots.txt 显式放行 GPTBot、OAI-SearchBot、ChatGPT-User、ClaudeBot、Claude-SearchBot、PerplexityBot、Google-Extended 等
- llms.txt / llms-full.txt：公司简介、价格指南、每个商品页的摘要、面向 AI 助手的报价 API 说明
- 报告同时列出：PakFactory 文字残留页、logo/电话残留、缺失背景图、重复标题

面向 AI 助手 / 代理（GEO）：`GET /packoasis/quote?product_type=mailer-box&dimensions=10x8x4&quantity=1000`（免密钥、限流），返回报价与 `order_url`（打开 packoasis.com 上预填好的报价组件）；`GET /packoasis/catalog`、`GET /packoasis/openapi.json`。

## 4. 部署步骤

### 4.1 后端（Railway，本仓库）

1. 合并本 PR 后按原方式部署（`medusa build` 现已通过；`start:railway` 会自动跑新迁移：rfq 新字段、lead_event 表）。
2. 设置环境变量（见 4.3），至少 `STOREFRONT_URL`、`SALES_NOTIFY_EMAIL`、邮件 provider、`ANTHROPIC_API_KEY`。
3. 在 Railway 服务终端执行一次：`npx medusa exec ./src/scripts/setup-instant-quote.ts`
4. 验证：`curl "https://<后端>/packoasis/quote?product_type=mailer-box&quantity=1000"` 返回报价；`https://<后端>/packoasis/widget.js` 返回脚本；后台左侧出现 "Leads & RFQs"。

### 4.2 前端（packoasis-storefront）

1. 在该仓库根目录：`git apply <本仓库>/docs/patches/packoasis-storefront-instant-quote.patch`（新增 `/api/quote-checkout`、全站挂载组件、自定义行项目显示、商品页 canonical + Product JSON-LD）。
2. 复制 `scripts/optimize-seo-geo.mjs` 到该仓库，部署前运行：
   ```bash
   node scripts/optimize-seo-geo.mjs public \
     --backend https://packoasis-api-production.up.railway.app \
     --publishable-key "$NEXT_PUBLIC_MEDUSA_PUBLISHABLE_KEY"
   ```
   建议写进 `deploy` 脚本，保证每次部署都用最新价格重新生成。拿到 PackOasis logo 后加 `--logo /path/to/logo.svg`。
3. `yarn deploy`，然后在线上走一遍：商品页 → Instant quote → Order now → 结账 → 下单。

### 4.3 环境变量（后端，全部可选，括号内为默认值）

| 变量 | 作用 |
|------|------|
| `ANTHROPIC_API_KEY` | 开启 AI（未设置时用确定性兜底）；`PACKOASIS_AI_MODEL`（`claude-opus-5`）、`PACKOASIS_AI_DISABLED` |
| `RESEND_API_KEY` 或 `SENDGRID_API_KEY` | 邮件发送（都没有时只记日志）；`PACKOASIS_EMAIL_FROM`（`PackOasis <quotes@packoasis.com>`）、`PACKOASIS_REPLY_TO`（`hello@packoasis.com`） |
| `SALES_NOTIFY_EMAIL` | 销售提醒收件人，逗号分隔 |
| `STOREFRONT_URL` | 邮件链接用的前端域名（`https://packoasis.com`） |
| `INSTANT_QUOTE_PRICING_JSON` | 价格表覆盖 |
| `INSTANT_QUOTE_DEFAULT_COUNTRY`（`us`）/ `INSTANT_QUOTE_COUNTRIES`（`us`） | 默认国家 / 初始化脚本开通的国家 |
| `PACKOASIS_FOLLOWUPS_ENABLED`（`true`）/ `PACKOASIS_FOLLOWUP_DELAYS_HOURS`（`24,72`） | 自动跟进 |
| `PACKOASIS_ENRICHMENT_ENABLED`（`true`）/ `PACKOASIS_LEAD_EVENT_RETENTION_DAYS`（`180`） | 官网补全 / 浏览数据保留 |
| `PACKOASIS_SIGNING_SECRET` | 退订/恢复链接签名（缺省用 `COOKIE_SECRET`） |
| `POSTHOG_EVENTS_API_KEY` / `POSTHOG_HOST` | 可选，PostHog 分析 |
| `STRIPE_API_KEY` / `STRIPE_WEBHOOK_SECRET` / `STRIPE_AUTO_CAPTURE` | 可选，在线刷卡（需同时设置 storefront 的 `NEXT_PUBLIC_STRIPE_KEY`） |

前端无需新增变量（组件脚本用已有的 `MEDUSA_BACKEND_URL` 与 `NEXT_PUBLIC_MEDUSA_PUBLISHABLE_KEY`）；`STORE_CORS` 需包含前端域名（现有配置已包含）。

## 5. 验证记录

- 单元测试 57 个全部通过：报价引擎、页面→品类映射、自由描述解析、SSRF 防护与 robots、网页抽取、浏览汇总与打分、签名链接、邮件模板、组件脚本自包含性，以及 **Claude 调用结构（离线 mock）**：beta 头 `server-side-fallback-2026-07-01`、`fallbacks: "default"`、JSON schema 输出、拒答回退。
- 本地 Postgres + Medusa：迁移、初始化脚本（含幂等重跑）、`medusa build`、生产模式 `medusa start` 均通过。
- SEO/GEO 优化器：对完整 `public/` 副本连续运行两次，第二次输出逐字节一致，`--check` 通过。
- API 端到端：浏览信标 → 估价 → 下单 → 地址 → $0 运费 → 支付会话 → 完成订单，1.5 秒；RFQ 状态 ORDERED、报价 ACCEPTED、邮件 4 封、浏览轨迹关联正确。
- 浏览器端到端（Playwright + 本地 storefront）：主流程共 9 次运行全部通过（5.6–6.6 秒）；旧联系表单提交、AI 深链预填、过期报价重算、跟进任务（不重复发送）、一键退订、后台 Leads 页均通过。
- 未在本环境验证：真实 Claude / Resend 调用（没有该应用的密钥）、线上域名（本环境网络策略拦截 packoasis.com 与 Railway 域名）、真实公司官网抓取（沙箱无直连外网）。

## 6. 需要你决定 / 提供

1. **校准价格**：默认价是按公开市场价估算的占位值，上线前请用 `INSTANT_QUOTE_PRICING_JSON` 校准（尤其毛利系数、各品类 MOQ、材料单价）。在校准前如不想开放直接下单，可把所有品类设为 `"instant": false`，流程会自动转为"人工确认报价"。
2. **品牌与合规**：提供 PackOasis logo 与电话；删除/替换 38 页里的 PakFactory 评价和评分背书。
3. **密钥**：`ANTHROPIC_API_KEY`、`RESEND_API_KEY`（或 SendGrid，并配置发信域名 SPF/DKIM）、`SALES_NOTIFY_EMAIL`；如需在线刷卡再加 Stripe。
4. **隐私政策**：补充首方浏览分析、根据客户提供的网站/邮箱域名查询公开企业信息、报价跟进邮件及退订方式。
5. **前端仓库权限**：在 https://claude.ai/connect-github 为 `packoasis-storefront` 安装 Claude GitHub App，我就能直接提交前端改动；或按 4.2 手动应用补丁。
6. **缺失图片**：重新抓取或替换 98 张横幅背景图（清单见优化器报告 `missingBackgroundImages`）。
