# 电商 Excel 上传入库经营看板

这是一个轻量版 Streamlit 电商经营看板，用于上传 Excel 复盘表、解析经营指标、写入 PostgreSQL 数据库，并从数据库读取历史数据展示每日、每周、每月趋势。

项目不包含复杂用户系统、不接平台 API、不做 SKU 和库存分析。最护和碧维是不同品类品牌，看板展示各自经营状态，不做简单输赢对比。千川数据只作为投放补充分析，不默认计入总 GMV，避免和抖店重复。

## 项目结构

```text
.
├── app.py
├── db.py
├── requirements.txt
├── README.md
├── .gitignore
├── data/
├── output/
└── sample_data/
```

真实 Excel、密码、数据库连接串都不要提交到 GitHub。

## 本地运行

安装依赖：

```bash
pip install -r requirements.txt
```

设置环境变量：

```bash
export APP_PASSWORD="自行设置的访问密码"
export DATABASE_URL="postgresql+psycopg2://用户名:密码@主机:5432/数据库名"
```

启动：

```bash
streamlit run app.py
```

如果 `streamlit` 命令不可用：

```bash
python3 -m streamlit run app.py
```

也可以在本地创建 `.env`：

```text
APP_PASSWORD=自行设置的访问密码
DATABASE_URL=postgresql+psycopg2://用户名:密码@主机:5432/数据库名
```

## 配置项

应用按以下顺序读取配置：

- `APP_PASSWORD`：优先 `st.secrets["APP_PASSWORD"]`，其次环境变量 `APP_PASSWORD`
- `DATABASE_URL`：优先 `st.secrets["DATABASE_URL"]`，其次环境变量 `DATABASE_URL`

如果没有设置 `APP_PASSWORD`，页面会默认允许访问并显示安全提醒。

如果没有设置 `DATABASE_URL`，页面会提示“请配置数据库连接”，上传的 Excel 只能临时解析，不能长期保存。

## 数据库表

点击页面上的“初始化数据库”后，会通过 SQLAlchemy 创建 4 张表：

- `upload_batches`：记录每次上传行为
- `daily_metrics`：核心经营数据，一天、一个品牌、一个平台、一个渠道一行
- `raw_metrics`：保留解析后的原始长表
- `import_errors`：记录无法识别或解析失败的信息

`daily_metrics` 有唯一约束：

```text
date + brand + platform + channel
```

重复上传同一天、同品牌、同平台、同渠道的数据时，不新增重复行，而是更新已有数据。

## 连接 Supabase PostgreSQL

1. 登录 Supabase
2. 创建 Project
3. 打开 Project Settings
4. 进入 Database
5. 找到 Connection string
6. 选择 URI 格式
7. 将连接串改成 SQLAlchemy 可用格式：

```text
postgresql+psycopg2://postgres:你的密码@db.xxxxxx.supabase.co:5432/postgres
```

如果 Supabase 给的是 `postgres://...`，代码会自动兼容转换，但建议部署时直接使用 `postgresql+psycopg2://...`。

## Streamlit Community Cloud 部署

1. 创建 GitHub 仓库
2. 上传项目代码
3. 不要上传真实 Excel 数据
4. 打开 `share.streamlit.io`
5. 选择 GitHub 仓库
6. 入口文件选择 `app.py`
7. 在 Secrets 中设置 `APP_PASSWORD` 和 `DATABASE_URL`
8. 点击 Deploy
9. 获得长期访问链接

Streamlit Cloud 的 Secrets 示例：

```toml
APP_PASSWORD = "自行设置的访问密码"
DATABASE_URL = "postgresql+psycopg2://postgres:你的密码@db.xxxxxx.supabase.co:5432/postgres"
```

## 上传 Excel 入库

1. 打开网页并输入访问密码
2. 如果是首次部署，点击“初始化数据库”
3. 进入“上传数据”Tab
4. 上传一个或多个 Excel
5. 页面会展示解析预览
6. 点击“确认导入数据库”
7. 页面会显示新增数量、更新数量、失败/提示数量
8. 首页和历史趋势会从数据库读取 `daily_metrics`

支持根据文件名自动识别：

- 包含“最护”识别为品牌“最护”
- 包含“碧维”识别为品牌“碧维”
- 包含“抖店”识别为平台“抖店”
- 包含“拼多多”识别为平台“拼多多”
- 包含“千川”识别为平台“千川”

## 如何确认数据保存成功

页面方式：

- 导入后看到“新增数量 / 更新数量 / 失败数量”
- 回到“老板首页”或“历史趋势”Tab，能看到数据库历史数据
- 重复上传同一天数据时，“更新数量”会增加，而不是产生重复记录

数据库方式：

- Supabase Table Editor 中查看 `daily_metrics`
- 查看 `upload_batches` 是否有本次上传记录
- 查看 `raw_metrics` 是否有原始长表记录
- 如果有解析异常，查看 `import_errors`

## 安全注意

- 不要把真实 Excel 提交到 GitHub
- 不要把 `APP_PASSWORD` 写进代码
- 不要把 `DATABASE_URL` 写进代码
- `.env` 不要提交
- 这个密码只是轻量访问控制，不是企业级权限系统
