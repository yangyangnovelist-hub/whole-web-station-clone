# 全量页面保留与迁移策略

## 目标

保留 PakFactory 镜像站的：

- 页面层级
- URL 热点跳转
- 图片与商品图素材
- 列表与筛选入口
- 询盘入口与文案结构

同时把动态能力迁移到正式工程：

- Medusa 商品与目录
- 登录 / Google 登录
- 询盘 / Draft Order
- 账户中心
- Cloudflare + Railway 部署

## 当前仓库的新结构

- `captures/`: 原始镜像资产，不作为正式应用源码
- `apps/storefront/`: Medusa 官方 Next.js storefront
- `apps/backend/`: Medusa 官方 backend starter
- `docs/pakfactory-route-inventory.json`: 全量镜像路由清单

## 迁移原则

1. 不删除镜像页面资产。
2. 不在镜像 HTML 上继续堆积业务逻辑。
3. 在 `apps/storefront` 中逐页重建 URL 和视觉结构。
4. 优先迁移高价值页面，再处理长尾内容页。
5. 所有动态流程都接回 Medusa 或正式 API，不再保留 Magento/Zoho 直连逻辑。

## 分层迁移

### Phase 1 Core

- 首页
- 询盘页
- 联系页
- 核心 CTA 与导航

### Phase 2 Listing

- 产品详情页
- 分类页
- 筛选页
- 账户页

### Phase 3 Commerce / Long Tail

- Draft order 承接
- Inspiration 长尾内容
- About / Sustainability / Why 等内容页

### Phase 4 Legacy Fallback

- 低频页面先保留镜像语义
- 等核心链路稳定后再逐步替换

## 下一步执行

1. 基于 `docs/pakfactory-route-inventory.json` 锁定优先迁移批次。
2. 在 `apps/storefront` 中先复刻首页、列表页、详情页、询盘页。
3. 在 `apps/backend` 中接入 email/password、Google、draft order。
4. 再把长尾页面逐步接管。
