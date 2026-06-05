# 询盘优先前端改版 + Medusa 接入企划

## 1. 项目结论

当前仓库不是一个可直接改造的业务前端项目，而是一个用于镜像站抓取与修复的工具仓库。`README.md` 明确说明它的职责是 `HTTrack + Playwright` 的克隆流程，不包含可运营的前后端应用壳。

从 PakFactory 镜像内容看，现有业务链路本质上是：

- 前端为 Magento 风格站点，保留 `Cart`、`My Quote`、`Customer Account` 等旧语义。
- `contact-us.html` 的询盘页直接提交到 `Zoho CRM WebToLead` 表单。
- 询盘表单字段已经具备商业价值，包括：
  - 服务类型
  - 姓名 / 邮箱 / 电话 / 公司
  - 产品类型
  - 数量
  - 时间周期
  - 项目描述
- 页面还带有 MOQ 前端校验、reCAPTCHA、PostHog/GA 事件、Zoho SalesIQ 在线咨询等遗留脚本。

这意味着本项目不是“改几个变量名和 UI”就能上线，而是需要从“镜像参考站”升级为“可运营的询盘优先商务站”。

## 2. 目标定义

本次改造目标不是完整复刻 PakFactory 的 Magento 结构，而是构建一个更适合获客询盘的现代化站点，满足下面 6 个目标：

1. 完成品牌替换：名称、文案、变量名、元信息、埋点事件、域名相关配置全部替换为你的品牌。
2. 前端改版：保留包装行业的可信感，但把核心 CTA 从传统电商导向改为“提交项目需求 / 获取报价 / 专家回访”。
3. 接入 Medusa：用 Medusa 承担商品、客户、认证、后台管理、草稿订单/商机承接。
4. 加入 ToC 能力：支持普通消费者或小商家浏览产品、登录、维护资料、提交需求。
5. 完善 Google 登录：从按钮、回调、账号绑定、异常处理到生产环境配置闭环。
6. 上线 Cloudflare + Railway：前端走 Cloudflare，自定义域名与性能/CDN 在前；Medusa 后端与数据库走 Railway。

## 3. 核心原则

### 3.1 不重复造轮子

严格遵循当前工作区 `AGENTS.md` 约束：

- 前端底座优先复用 Medusa 官方 storefront 能力，不从零手写商城底盘。
- 认证优先使用 Medusa 官方 Auth Module Provider，不自建 Google OAuth 服务。
- 后台运营能力优先使用 Medusa Admin、Draft Orders、现成插件，不自造 CRM 后台。
- 域名、证书、回源、CORS 走 Cloudflare / Railway 官方能力，不做自定义代理拼装。

### 3.2 不做高维护度“伪重命名”

用户提出“改名字改变量名”，这件事要分层处理：

- 可以改：
  - 站点品牌名
  - 文案 copy
  - 组件名 / 页面名 / 本项目封装层变量名
  - 埋点事件名
  - 自定义 API DTO 名
- 不应该硬改：
  - Medusa 内部领域模型名，例如 `cart`、`customer`、`region`、`draft-order`
  - 官方 SDK 接口字段
  - 官方插件配置项

原因很简单：把框架内部语义强行改成自定义命名，只会提高维护成本，后续升级 Medusa 会非常痛苦。

## 4. 推荐目标架构

### 4.1 总体架构

- 前端：基于 Medusa 官方 Next.js storefront/starter 二次开发
- 后端：Medusa v2 部署在 Railway
- 数据库：Railway PostgreSQL
- 文件与图片：先沿用第三方对象存储或 Medusa 默认方案，后续再扩展
- 认证：Medusa Auth Module + Email/Password + Google
- 前台域名：`www.xxx.com` / `xxx.com` 走 Cloudflare
- API 域名：`api.xxx.com` 走 Railway Custom Domain
- 管理入口：`https://api.xxx.com/app`

### 4.2 为什么不建议继续基于镜像页面直接修补

因为当前镜像页的问题是结构性问题：

- 页面是静态镜像，不具备真实可维护的应用结构。
- 业务逻辑仍然耦合 Magento/Cart2Quote/Zoho 表单。
- 登录、询盘、购物车、报价都依赖旧站实现。
- 后续接 Medusa 时，会出现“双语义系统并存”：UI 是新站，底层还是旧流程。

所以推荐方案不是“在镜像 HTML 上打补丁”，而是：

1. 保留镜像作为视觉和信息架构参考。
2. 用 Medusa 官方 storefront 作为新前端底座。
3. 仅复用必要内容、信息层级、包装行业素材与表单字段设计。

## 5. 业务模型建议

### 5.1 询盘优先，而不是传统购物车优先

PakFactory 镜像现在同时存在 `Cart` 和 `My Quote` 两套逻辑，这对你不是最优解。更适合你的做法是把用户链路重构成：

1. 浏览包装类型 / 行业案例 / 工艺能力
2. 选择产品方向
3. 填写项目需求
4. 登录或继续留资
5. 进入“我的项目 / 我的询盘”
6. 由销售或运营在 Medusa Admin 中跟进，必要时生成 draft order

### 5.2 前台建议保留的核心对象

- Product：包装产品或包装能力页
- Customer：登录用户
- Inquiry：询盘/项目需求
- Draft Order：销售确认后的报价单/草稿订单
- Company Profile：企业客户资料扩展

### 5.3 ToC 插件解释与处理建议

这里先做一个务实假设：

- 你说的“toC 插件”我暂按“面向普通消费者/小商家的一套前台购物与账号能力”理解；
- 如果你指的是某个具体第三方插件，请补充插件仓库地址或 npm 包名。

在没有明确插件名称的前提下，推荐直接采用 Medusa 官方 storefront 的 ToC 基础能力：

- 商品浏览
- 账户注册 / 登录
- 地址与基础资料
- 订单/草稿订单承接
- 第三方登录

这样风险最低，也最符合“不重复造轮子”。

## 6. 前端改版方向

### 6.1 UI 改造目标

当前 PakFactory 风格偏传统工业电商。你的目标应该是“商务咨询型获客站”，因此建议：

- 弱化 `Cart` 的中心地位
- 强化 `Get Quote` / `Start Your Project` / `Talk to Packaging Expert`
- 首页第一屏突出：
  - 行业可信背书
  - 起订量与交期能力
  - 支持定制范围
  - 询盘 CTA
- 产品页不只展示 SKU，而是展示：
  - 包装用途
  - 材料与工艺可选项
  - MOQ
  - 打样与交期
  - 提交项目需求

### 6.2 设计语言建议

建议保留包装制造业的商业可信感，但不要延续老 Magento 味道：

- 视觉方向：工业现代感 + 商务咨询感
- 关键词：可信、专业、效率高、可定制、样品真实
- 首页结构建议：
  - Hero：一句价值主张 + 双 CTA
  - 热门包装类型
  - 行业解决方案
  - 工艺/材质/印刷能力
  - 样品展示
  - 交期与 MOQ 说明
  - 客户证言 / 合作品牌
  - 询盘表单入口

### 6.3 变量与文案重命名策略

建议统一做一轮“品牌抽象层”：

- `pakfactory` -> 你的品牌标识
- `quote` 在前台 copy 中可改成：
  - `project`
  - `inquiry`
  - `request`
  - `estimate`
- 但在 Medusa/数据库领域模型中，保留框架原生概念，不强改底层。

## 7. Medusa 接入方案

### 7.1 推荐接入方式

推荐采用“双仓或单 monorepo 双应用”结构：

- `apps/storefront`
- `apps/medusa`

或者：

- `storefront/`
- `backend/`

前端与后端分离，便于分别部署到 Cloudflare 与 Railway。

### 7.2 后端能力规划

第一阶段必须接入的 Medusa 能力：

- 产品目录
- 客户账户
- 认证
- Admin
- Draft Orders
- Store API

第二阶段再扩展：

- 价格阶梯
- 自定义询盘实体
- 附件上传
- 邮件通知
- CRM 同步

### 7.3 询盘如何落在 Medusa

推荐分两步，不要一开始就做复杂报价引擎：

第一阶段：

- 前台表单提交到你自己的 backend route
- route 将数据写入 Medusa 的自定义模块或 metadata
- 运营在 Admin 侧查看并手动创建 draft order

第二阶段：

- 引入正式 Inquiry Module
- 支持上传刀版图 / 参考图 / 包装尺寸
- 支持询盘状态流转
- 支持从询盘一键转 draft order

### 7.4 为什么推荐 Draft Orders

Medusa 官方已有 Draft Orders 插件与能力，适合处理“销售代客创建订单 / 线下报价 / 协商成交”场景。对包装定制这种非标准化商品，这是比纯购物车更贴切的过渡方案。

## 8. Google 登录方案

### 8.1 推荐实现

Google 登录不要单独做一套认证系统，直接复用 Medusa 官方 Auth Module Provider：

- 后端注册 `@medusajs/medusa/auth-google`
- 同时保留 `@medusajs/medusa/auth-emailpass`
- 前端通过 Medusa JS SDK 触发 `/auth/customer/google`
- 回调页完成 callback validate
- 成功后创建或绑定 customer

### 8.2 生产级闭环

必须补齐这些点，否则“按钮能点”不等于流程可用：

- Google Cloud Console 中的生产域名和回调地址
- Cloudflare 正式 HTTPS 域名
- Railway API 域名与前台域名的 CORS 配置
- 登录成功后的跳转页
- 新用户首次登录后的资料补全
- Google 已存在邮箱与本地账号的合并策略
- 登录失败 / 取消授权 / callback 过期处理

### 8.3 业务建议

Google 登录应作为“降低询盘提交阻力”的能力，而不是页面主角：

- 访客可先填写项目需求
- 提交前提示“登录后可保存项目和查看进度”
- 已登录用户可直接进入“我的项目”

这样更符合询盘业务，而不是强行把站点做成标准零售商城。

## 9. Cloudflare + Railway 上线方案

### 9.1 推荐拓扑

- `example.com` -> Cloudflare 前端
- `www.example.com` -> Cloudflare 前端
- `api.example.com` -> Railway Medusa

### 9.2 部署顺序

正确顺序应该是：

1. 先把 Medusa backend 在 Railway 跑通
2. 配置 PostgreSQL 与基础 env
3. 配置 `api.example.com`
4. 配置 Medusa 的 `storeCors` / `adminCors` / `authCors`
5. 再部署前端到 Cloudflare
6. 前端切换 `MEDUSA_BACKEND_URL`
7. 最后接生产域名与 Google OAuth 回调

### 9.3 不建议的做法

- 不建议把前端和 Medusa API 混在一个老镜像站里热修
- 不建议直接使用默认 `workers.dev` 或 `*.up.railway.app` 作为正式域名
- 不建议在正式环境里保留 Zoho WebToLead 直连作为主询盘链路

Zoho/SalesIQ 可以作为 CRM 辅助集成，但不应继续做主业务入口。

## 10. 分阶段实施企划

## 阶段 0：项目落地准备

工期：2 到 3 天

输出：

- 新品牌命名清单
- 页面地图
- PakFactory 内容映射表
- 旧字段 -> 新字段映射
- 目标域名规划

任务：

- 确认品牌名、域名、主打行业
- 确认 ToC 插件具体含义
- 确认是否保留 Zoho / PostHog / SalesIQ
- 盘点 PakFactory 里真正要保留的页面，不做全站复刻

## 阶段 1：技术底座重建

工期：4 到 6 天

输出：

- 新 storefront 项目
- 新 Medusa backend 项目
- 基础环境变量
- 本地联调环境

任务：

- 使用 Medusa 官方 starter 初始化前后端
- 建立新目录结构
- 导入基础设计 token
- 接上产品、分类、客户、认证
- 完成 Railway 本地/远程配置

## 阶段 2：询盘优先前端改版

工期：5 到 8 天

输出：

- 首页
- 列表/产品详情页
- 询盘页
- 登录/注册页
- 我的项目页一期

任务：

- 替换品牌名和所有前台 copy
- 重构 CTA
- 做首页与产品详情页的商业化改版
- 将询盘表单字段标准化
- 接入埋点与表单校验

## 阶段 3：询盘数据与 Medusa 业务打通

工期：4 到 7 天

输出：

- Inquiry API
- Admin 承接方案
- Draft Order 工作流一期

任务：

- 建立 inquiry 提交接口
- 存储用户项目资料
- 运营后台可查看/筛选询盘
- 支持从询盘转成 draft order

## 阶段 4：Google 登录与账户流程完善

工期：2 到 4 天

输出：

- Google 登录生产可用
- callback 页面
- 登录后跳转与资料补全

任务：

- 配置 Medusa Google Auth Provider
- 配置 Google Cloud OAuth
- 处理登录成功 / 失败 / 取消授权
- 做账号合并和异常提示

## 阶段 5：Cloudflare + Railway 正式上线

工期：2 到 3 天

输出：

- Cloudflare 前端正式域名
- Railway API 正式域名
- HTTPS、CORS、回调地址全部打通

任务：

- Railway custom domain
- Cloudflare custom domain
- 环境变量切换
- 域名探活
- 生产回归测试

## 11. 里程碑与验收标准

### 里程碑 M1：基础架构可跑

验收标准：

- 前后端可本地联调
- Medusa Admin 可登录
- 前端能从 Medusa 取商品数据

### 里程碑 M2：询盘链路闭环

验收标准：

- 用户可提交项目需求
- 数据落库
- Admin 可查看
- 可生成 draft order 或跟进记录

### 里程碑 M3：登录闭环

验收标准：

- Email/Password 正常
- Google 登录正常
- 登录后能返回正确页面
- 跨域与回调无报错

### 里程碑 M4：生产上线

验收标准：

- `www` 与根域正常打开
- `api` 域名可访问
- `/app` 后台可登录
- 表单、登录、询盘、图片、埋点均正常

## 12. 风险与注意事项

### 12.1 最大风险

最大的风险不是技术，而是 scope：

- 如果你要求“保留 PakFactory 全站信息架构 + 所有产品页 + 全部旧流程 + 再接 Medusa”，工期会显著膨胀。

因此必须做裁剪，只保留真正影响询盘转化的页面。

### 12.2 技术风险

- 旧 Zoho 表单字段命名混乱，迁移时容易丢业务语义。
- Google 登录最常见问题是 redirect URI 与生产域名不一致。
- Cloudflare 与 Railway 组合时，域名、证书、代理模式、CORS 顺序一旦搞错，会出现前台正常但登录失败。
- 如果试图重命名 Medusa 内核字段，会抬高后续升级成本。

## 13. 推荐实施策略

如果你要的是“最快落地且后续能持续运营”的方案，我建议按下面执行：

1. 不在当前镜像仓库上直接开发正式站。
2. 把当前仓库当作参考资料库与内容提取源。
3. 新建一个真正的 storefront + medusa 项目。
4. 第一版只做询盘优先闭环，不追求一步到位复刻全部商城能力。
5. 先跑通 Email/Password 与 Google 登录，再补更复杂的销售流程。

## 14. 本企划对应的下一步

如果按这个企划推进，下一步最合理的执行顺序是：

1. 确认你的品牌名、正式域名、目标国家市场。
2. 明确“toC 插件”具体指哪一个插件或能力包。
3. 我基于 Medusa 官方 starter 新建前后端骨架。
4. 先做首页 + 询盘页 + 登录页三页闭环。
5. 再接 Inquiry -> Medusa -> Draft Order。

## 15. 参考依据

- Medusa 官方 Next.js Starter：<https://docs.medusajs.com/resources/nextjs-starter>
- Medusa 第三方/社交登录流程：<https://docs.medusajs.com/resources/storefront-development/customers/third-party-login>
- Medusa Google Auth Provider：<https://docs.medusajs.com/resources/commerce-modules/auth/auth-providers/google>
- Medusa Draft Orders Plugin：<https://docs.medusajs.com/cloud/draft-order-plugin>
- Cloudflare Workers 自定义域名 / routes：<https://developers.cloudflare.com/workers/best-practices/workers-best-practices/>
- Cloudflare Routes and domains：<https://developers.cloudflare.com/workers/configuration/routing/>
- Railway 自定义域名：<https://docs.railway.com/networking/domains/working-with-domains>
- Google OAuth 生产规则：<https://developers.google.com/identity/protocols/oauth2/web-server>

