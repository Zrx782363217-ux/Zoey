# 电商经营复盘看板

这是一个 Streamlit 只读经营看板。前端只展示 Supabase PostgreSQL 中的历史数据，不提供 Excel 上传入口。每天的数据由管理员在本地通过 `import_excel.py` 导入数据库。

最护和碧维是不同品类品牌，本看板展示各自经营状态，不做简单输赢对比。千川数据只作为投放补充分析，不默认计入总 GMV，避免和抖店重复。

## 项目结构

```text
.
├── app.py
├── db.py
├── import_excel.py
├── requirements.txt
├── README.md
├── .gitignore
├── .env.example
├── data/
│   ├── .gitkeep
│   └── README.md
├── output/
│   └── .gitkeep
└── sample_data/
    └── .gitkeep
```

## 前端部署

1. GitHub 只上传代码。
2. 不上传真实 Excel。
3. Streamlit Community Cloud 设置 `APP_PASSWORD` 和 `DATABASE_URL`。
4. 前端只展示数据，不提供上传入口。

Streamlit Cloud Secrets 示例：

```toml
APP_PASSWORD = "你的前端访问密码"
DATABASE_URL = "postgresql+psycopg2://postgres:你的数据库密码@db.xxxxxx.supabase.co:5432/postgres"
```

主文件路径：

```text
app.py
```

如果你把整个项目文件夹放进仓库子目录，则主文件路径填写：

```text
ecommerce-visual-dashboard/app.py
```

## Supabase PostgreSQL

在 Supabase 创建项目后，进入 Project Settings -> Database，复制 Connection string。

推荐格式：

```text
postgresql+psycopg2://postgres:你的数据库密码@db.xxxxxx.supabase.co:5432/postgres
```

如果 Supabase 给的是 `postgres://...`，代码会自动转换为 SQLAlchemy 可用格式。

首次导入时，后台脚本会自动创建：

- `upload_batches`
- `daily_metrics`
- `raw_metrics`
- `import_errors`

`daily_metrics` 有唯一约束：

```text
date + brand + platform + channel
```

重复导入同一天、同品牌、同平台、同渠道的数据时，会更新已有数据，不会重复新增。

## 后台导入

1. 本地创建 `.env`：

```text
DATABASE_URL=postgresql+psycopg2://postgres:你的数据库密码@db.xxxxxx.supabase.co:5432/postgres
APP_PASSWORD=你的前端访问密码
DEFAULT_YEAR=2026
```

2. 把 Excel 放进 `data` 文件夹。

3. 安装依赖：

```bash
pip install -r requirements.txt
```

4. 正式导入前先 dry-run 检查日期范围：

```bash
python import_excel.py --dry-run
```

dry-run 只解析和打印预览，不写入数据库。重点检查每个文件的识别月份和解析日期范围，例如 6 月文件应显示 `2026-06-01` 到 `2026-06-30`。

5. 确认日期范围无误后运行导入脚本：

```bash
python import_excel.py
```

也可以在 Mac 上双击 `run_import.command` 导入。首次使用前需要授权：

```bash
chmod +x run_import.command
```

授权后，双击 `run_import.command` 即可自动进入项目目录并运行：

```bash
python3 import_excel.py
```

6. 导入成功后刷新 Streamlit 网页。

命令行会打印：

- 读取了哪些文件
- 成功解析多少条
- 新增多少条
- 更新多少条
- 失败多少条
- 失败原因

导入脚本会自动读取 `data` 文件夹下所有 `.xlsx` 和 `.xls` 文件，并跳过 `~$` 开头的 Excel 临时文件。不再要求固定上传某个月份的文件。

## 日期识别规则

- 文件名包含 `6月` 或 `6月份` 时，按 6 月解析
- 文件名包含 `7月` 或 `7月份` 时，按 7 月解析
- 其他月份同理
- 文件名包含 `2026年6月`、`2026-06`、`2026.6` 时，使用文件名中的年份
- 文件名没有年份时，优先使用 `.env` 中的 `DEFAULT_YEAR`
- 没有 `DEFAULT_YEAR` 时，使用当前年份
- 日期列是 `6.1` 时解析为 `DEFAULT_YEAR-06-01`
- 日期列是 `1`、`2`、`3` 时，结合文件名识别到的月份解析

如果文件名、sheet 名和日期列都无法识别月份，脚本不会默认写成 7 月，而会给出解析提示。

## 如果之前误导入过数据

如果之前错误导入过 6 月数据，并且怀疑覆盖了 7 月数据，不要让脚本自动清库。最简单的修复方式是：

1. 你确认后手动清空 `daily_metrics` 和 `raw_metrics` 测试数据
2. 用修复后的脚本先运行 `python import_excel.py --dry-run` 检查 6 月日期
3. 正式重新导入 6 月
4. 再重新导入 7 月
5. 刷新网页，确认可以分别看到 6 月和 7 月趋势

## 本地预览前端

```bash
export APP_PASSWORD="你的前端访问密码"
export DATABASE_URL="postgresql+psycopg2://postgres:你的数据库密码@db.xxxxxx.supabase.co:5432/postgres"
streamlit run app.py
```

前端只读取 `daily_metrics` 并展示：

- 老板首页
- 历史趋势
- 渠道分析
- 自动复盘提醒

如果数据库暂无数据，页面只显示：

```text
暂无历史数据，请管理员在后台导入数据。
```

## 安全注意

- 不要提交 `.env`
- 不要提交真实 Excel
- 不要把 `APP_PASSWORD` 写进代码
- 不要把 `DATABASE_URL` 写进代码
- Streamlit 前端只做轻量密码保护，不是复杂用户权限系统
