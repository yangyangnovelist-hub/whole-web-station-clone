# PackOasis 网站开发企划

## 1. 项目目标

在保留镜像站核心前台页面结构、图片组织、热点跳转、筛选体验和商业表达的前提下，完成 `PackOasis` 的首版正式上线。

本阶段只聚焦：

- 网站先上线
- 前后端打通
- 询盘流程可用
- 管理后台可用
- Cloudflare + Railway 可部署

本阶段暂不展开：

- 通用插件开源
- 社区化产品包装
- 与网站首发无关的高级生态建设

## 2. 产品定位

`PackOasis` 不是一个纯标准零售商城，也不是一个纯展示站，而是：

- 以镜像站为基础的高完成度包装行业前台网站
- 以 Medusa 为后端内核的电商与项目询盘混合平台
- 同时兼容 `ToC` 下单能力与 `ToB` 项目制询盘能力

第一阶段上线目标是先把“能获客、能承接、能进入项目流、能后台处理”跑通。

## 3. 当前基线

### 3.1 前端基线

- 当前有效前端基线：PakFactory 镜像站
- 废弃基线：`new_architecture/frontend`
- 当前已完成：核心镜像页品牌清洗样本、PackOasis 命名替换、旧 RFQ 前端语义初步映射

### 3.2 后端基线

- 当前有效后端基线：`whole-web-station-clone/apps/backend`
- 技术内核：Medusa 官方
- 当前已完成：Admin 登录基线、emailpass auth、Google auth 预留、管理员初始化脚本
- 当前已完成：RFQ 模块、project-draft 模块、`/store/rfqs` 与 `/store/project-drafts/*` 首版接口

### 3.4 当前推进进度（2026-04-19）

已落地：

- `contact-us` 已在 `apps/storefront` 中重建为 `PackOasis` 页面
- 页面保留镜像站的核心信息架构：主视觉、FAQ、项目表单、Live Support、销售办公室与产能网络
- 原 Zoho WebToLead 已替换为 Medusa RFQ 提交动作
- 页面可读取当前 project draft，并在提交时一并带入 RFQ
- 产品详情页已增加 “Start quote request” 入口，可先写入 project draft 再跳转到 `contact-us`
- 顶部导航已增加 project drawer，可查看项目项、移除项目项并进入 RFQ 提交页
- storefront 前台主要品牌文案已从 `Medusa Store` 清理为 `PackOasis`
- Railway 后端已上线，当前公网地址为 `https://packoasis-api-production.up.railway.app`
- Medusa Admin 登录页已可访问，管理员 `packoasis@gmail.com` 已可通过官方 `emailpass` 登录
- storefront 所需的默认 `sales channel`、`publishable key` 和 `United States / usd / us` region 已创建
- 使用当前 publishable key 请求 `/store/regions` 已返回成功，storefront 生产对接前置条件已补齐
- storefront 已补齐 Cloudflare Workers 所需的 `@opennextjs/cloudflare`、`wrangler.jsonc`、`open-next.config.ts`、静态资源缓存头与 `.dev.vars.example`
- `apps/storefront` 已可对 live Railway backend 成功执行 `next build` 与 `opennextjs-cloudflare build`
- `wrangler preview` 已在本地 `workerd` 中返回首页 HTML，说明 Cloudflare 运行时链路已基本跑通
- storefront 已增加针对 Railway 瞬时网络抖动的 retry / timeout 防护，重点覆盖 SDK 请求与 `middleware` 里的 region 拉取

当前未完成：

- Cloudflare 账号登录、正式 Worker/域名绑定与 `packoasis.com` cutover
- Cloudflare dashboard / Worker 环境变量正式写入
- 产品页镜像化与产品页询盘入口接入
- 顶部项目抽屉 UI 深化与镜像站细节对齐
- Google 登录正式打通
- Cloudflare / Railway 正式双域联调与上线

### 3.3 商业目标基线

首版网站必须具备：

- 可访问的品牌官网首页
- 可浏览产品/类目/详情
- 可发起询盘或项目请求
- 可登录后台处理询盘与订单
- 可部署到正式域名体系

## 4. 第一阶段核心原则

### 4.1 保留镜像站的商业完成度

优先保留：

- 页面结构
- 视觉层次
- 图片和商品图
- 入口热点
- 类目与筛选表达
- 产品详情页的信息密度

不做无意义推翻重做。

### 4.2 不保留旧技术耦合

必须替换掉：

- Magento 账号流程
- Cart2Quote 前端依赖
- Zoho WebToLead
- 旧搜索接口
- 旧登录、购物车、询价接口

### 4.3 后端能力优先走官方 Medusa

优先级：

1. 先用 Medusa 官方已有能力
2. 再用官方插件或官方 provider
3. 最后才补自定义模块

### 4.4 先跑通闭环，再做增强

先上线版只做最必要闭环：

- 流量进入
- 页面浏览
- 询盘提交
- 后台处理
- 登录和基础账户

营销增强、积分、拼团、复杂支付、复杂审批不作为首发阻塞项。

## 5. 第一阶段范围

## 5.1 前台网站范围

必须交付：

- 首页
- 关于页
- 联系/发起项目页
- 产品列表页
- 产品详情页
- 基础搜索
- 基础登录/注册页
- 询盘入口与项目抽屉

建议保留：

- 页脚订阅区
- FAQ 区块
- 材质/工艺/用途内容模块
- 热点跳转和 CTA 入口

### 5.2 用户功能范围

必须交付：

- 用户注册
- 用户登录
- Google 登录
- 用户发起项目请求
- 用户查看基础项目状态

可第二阶段再做：

- 完整客户中心
- 地址簿增强
- 收藏夹
- 积分
- 优惠券中心

### 5.3 询盘与项目流范围

首发必须交付：

- 产品页发起询盘
- 联系页表单发起询盘
- 项目抽屉暂存项目项
- 表单提交到 Medusa 后端
- 后台可查看询盘
- 后台可更新状态

首发不强制：

- 自动智能报价
- 多轮供应商比价
- 完整审批流
- 自动结算

### 5.4 后台范围

首发必须交付：

- Medusa Admin 可登录
- 管理员账号可用
- 可查看客户
- 可查看询盘
- 可查看商品与订单

首发建议补充：

- RFQ 列表页
- RFQ 详情页
- 项目状态更新

### 5.5 部署范围

必须交付：

- Cloudflare 前端项目
- Railway 后端项目
- 正式域名绑定
- 基础环境变量配置
- 前后端联通验证

## 6. 功能拆解

### 模块 A：品牌与镜像清洗

目标：

- 完成 `PackOasis` 全站品牌替换
- 替换旧域名、旧邮箱、旧文案
- 保留页面结构与商业密度

交付：

- 高优先页面批量清洗
- 关键素材落地到本地资源或新资源域

### 模块 B：前台页面迁移

目标：

- 从镜像站迁移首页、类目、详情、联系页
- 在新前端仓库中转成可维护页面

交付：

- 高优先页面可运行版本
- 关键路由表
- 图片和静态资源整理

### 模块 C：RFQ / 项目流

目标：

- 用 Medusa 承接旧站 RFQ 壳层

交付：

- `/api/store/rfqs`
- `/api/store/project-drafts/*`
- 联系页表单接入新接口
- 产品页询盘入口接入新接口

### 模块 D：认证与账户

目标：

- 打通 Medusa 官方登录
- 补上 Google 登录

交付：

- emailpass 可用
- Google 登录可用
- 登录页、注册页、跳转逻辑可用

### 模块 E：商品与目录

目标：

- 用 Medusa 标准商品能力承接前台商品浏览

交付：

- 商品分类
- 产品详情基础字段
- 材质/工艺等扩展字段方案

### 模块 F：后台处理

目标：

- 让后台人员能处理前台承接到的业务

交付：

- 管理员可登录
- 询盘列表
- 询盘详情
- 基础状态维护

### 模块 G：上线部署

目标：

- 正式跑在生产环境

交付：

- Cloudflare 前端部署
- Railway 后端部署
- 域名解析
- 环境变量文档

## 7. 技术路线

### 前端

- 基于镜像站重建
- 保留原页面商业结构
- 渐进式替换旧表单和旧交互
- 最终归入新的前端仓库

### 后端

- 基于 Medusa 官方能力
- 官方 Auth
- 官方 Admin
- 自定义 RFQ / Project Draft 模块

### 数据

- 商品、客户、订单走 Medusa 主体系
- RFQ、项目草稿、供应商协作走自定义模块

## 8. 仓库规划

正式拆分后：

- `packoasis-frontend`
- `packoasis-backend`

当前工作区先作为集成实验场，待第一阶段跑通后再拆仓。

## 9. 里程碑

### M1：品牌与前台基线完成

目标：

- PackOasis 品牌替换完成
- 高优先页面样本完成
- 页面结构基本可复用

验收：

- 首页、联系页、产品详情页视觉可用
- 无明显 PakFactory 旧品牌残留

### M2：后端基线与认证完成

目标：

- Medusa 后端启动
- Admin 登录可用
- 管理员账号可创建
- Google 登录基线准备好

验收：

- `/app/login` 可访问
- `packoasis@gmail.com` 可登录后台

### M3：RFQ 闭环完成

目标：

- 联系页表单不再投 Zoho
- 产品页与项目抽屉接入 Medusa

验收：

- 用户提交询盘后，后台能看到记录
- 前台基础状态提示可用

### M4：生产部署完成

目标：

- Cloudflare + Railway 正式部署
- 域名打通

验收：

- `packoasis` 正式域名可访问
- 前后端 API 联通正常

## 10. 风险与对策

### 风险 1：镜像页技术残留过重

对策：

- 保留视觉，逐步剥离旧 JS
- 优先替换关键提交动作与路由

### 风险 2：Medusa 无现成 RFQ 模块

对策：

- 先做最小 RFQ 自定义模块
- 只覆盖首发需要的字段和状态

### 风险 3：首发范围失控

对策：

- 严格把营销、积分、拼团、复杂支付放到第二阶段
- 第一阶段只做获客承接闭环

### 风险 4：部署阶段变量混乱

对策：

- 提前固定域名、环境变量命名、部署步骤
- 先打通 staging 再切 production

## 11. 第一阶段不做项

- 开源插件发布
- 复杂供应商分单
- 完整财务报表系统
- 多租户 SaaS
- 秒杀、拼团、积分、直播电商
- 支付宝/微信支付正式集成

这些不是不做，而是不作为网站首发阻塞条件。

## 12. 下一步执行顺序

1. 扩大镜像高优先页面清洗范围
2. 把联系页 Zoho 表单替换为 Medusa RFQ 提交
3. 实现 `project-drafts` 与 `rfqs` 后端接口
4. 将产品页和顶部项目抽屉接入新接口
5. 完成登录、注册、Google 登录
6. 整理前端与后端部署配置
7. 创建正式 Cloudflare 与 Railway 项目并联调
