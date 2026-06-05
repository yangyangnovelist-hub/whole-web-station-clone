# PackOasis Medusa 后端上线基线

## 1. 当前目录边界

- 前端基线：`/Users/handsomeboy/Downloads/pakfactory-clone-workspace/whole-web-station-clone/apps/storefront`
- 后端基线：`/Users/handsomeboy/Downloads/pakfactory-clone-workspace/whole-web-station-clone/apps/backend`
- 镜像页面与静态资源整理：`/Users/handsomeboy/Downloads/pakfactory-clone-workspace/whole-web-station-clone`

当前阶段先在单工作区内把新前后端基线补到可部署。正式推送时，仍然按你的要求拆成两个全新仓库：

- `packoasis-frontend`
- `packoasis-backend`

## 2. 后端基线已落地内容

`apps/backend` 当前已经补上这些基础能力：

- Medusa Admin 固定挂载到 `/app`
- 官方 `emailpass` provider 已启用
- 官方 `google` provider 改为按环境变量条件启用
- `user` actor 固定使用 `emailpass`
- `customer` actor 默认 `emailpass`，当 Google 环境变量齐全时开放 `google`
- 新增管理员创建脚本：`yarn admin:create`

## 3. Railway 后端必须配置的环境变量

至少配置以下变量：

- `DATABASE_URL`
- `REDIS_URL`
- `JWT_SECRET`
- `COOKIE_SECRET`
- `MEDUSA_BACKEND_URL`
- `STORE_CORS`
- `ADMIN_CORS`
- `AUTH_CORS`
- `MEDUSA_ADMIN_EMAIL`
- `MEDUSA_ADMIN_PASSWORD`

建议值：

- `MEDUSA_ADMIN_EMAIL=packoasis@gmail.com`
- `MEDUSA_BACKEND_URL=https://api.packoasis.com`
- `STORE_CORS=https://packoasis.com,https://www.packoasis.com`
- `ADMIN_CORS=https://packoasis.com,https://admin.packoasis.com`
- `AUTH_CORS=https://packoasis.com,https://www.packoasis.com,https://admin.packoasis.com`

注意：

- `MEDUSA_ADMIN_PASSWORD` 不写入仓库，只放 Railway 项目变量
- 你指定的初始化密码就在 Railway 变量里设置，然后执行管理员创建脚本

## 4. Google 登录环境变量

只有在以下 3 个值都配置后，官方 Google provider 才会启用：

- `AUTH_GOOGLE_CLIENT_ID`
- `AUTH_GOOGLE_CLIENT_SECRET`
- `AUTH_GOOGLE_CALLBACK_URL`

建议回调地址：

- `https://api.packoasis.com/auth/customer/google/callback`

如果后续需要 Admin 也走 Google，再单独评估 Admin 侧入口，不和当前邮箱密码后台管理入口混在一起。

## 5. 管理员初始化步骤

Railway 部署成功后，在后端服务里执行：

```bash
yarn admin:create
```

这个脚本会读取：

- `MEDUSA_ADMIN_EMAIL`
- `MEDUSA_ADMIN_PASSWORD`

默认邮箱已经预置为 `packoasis@gmail.com`，密码必须从环境变量注入。

## 6. 验证标准

后端可用的最低标准：

1. `GET /app/login` 返回后台登录页
2. 用 `packoasis@gmail.com` 可以登录 Medusa Admin
3. storefront 的 `MEDUSA_BACKEND_URL` 指向 Railway 后端，而不是 localhost
4. `NEXT_PUBLIC_MEDUSA_PUBLISHABLE_KEY` 已换成该后端真实 publishable key

## 7. 下一阶段迁移顺序

后端当前只是官方 Medusa 基线，下一步按这个顺序推进：

1. 从旧 `oasis-platform` 迁移 `vendor`
2. 迁移 `rfq`
3. 迁移 `settlement`
4. 将镜像站的 `Start Your Project` / 询盘 / 筛选动作正式接到 Medusa API
5. 再拆成独立 `packoasis-backend` 仓库并绑定 Railway

## 8. 双仓库拆分建议

### 后端仓库

保留：

- `apps/backend`
- 后续迁移进来的 Medusa 模块
- Railway 部署配置

### 前端仓库

保留：

- `apps/storefront`
- 镜像站清洗后的页面和资源
- Cloudflare 部署配置

不要带入：

- 废弃的 `new_architecture/frontend`
- 旧 FastAPI 后端
