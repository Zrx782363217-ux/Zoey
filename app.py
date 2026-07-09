from __future__ import annotations

import re
import os
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

from db import (
    create_upload_batch,
    get_engine,
    get_database_url,
    init_db,
    insert_import_errors,
    insert_raw_metrics,
    load_daily_metrics,
    update_upload_batch,
    upsert_daily_metrics,
)


load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
SAMPLE_DATA_DIR = BASE_DIR / "sample_data"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_CSV = OUTPUT_DIR / "normalized_data.csv"

EXPECTED_FILES = [
    "碧维抖店7月复盘数据.xlsx",
    "碧维拼多多运营复盘表7月份.xlsx",
    "最护抖店7月复盘数据.xlsx",
    "最护拼多多7月复盘表.xlsx",
    "最护千川7月复盘数据.xlsx",
]

STANDARD_COLUMNS = [
    "date",
    "brand",
    "platform",
    "channel",
    "gmv",
    "net_gmv",
    "orders",
    "ad_spend",
    "roi",
    "net_roi",
    "refund_rate",
    "refund_amount",
    "conversion_rate",
    "click_to_order_rate",
    "commission",
]

DISPLAY_COLUMNS = STANDARD_COLUMNS.copy()

METRIC_ALIASES = {
    "gmv": ["GMV", "整体成交", "成交金额", "支付金额", "销售额"],
    "net_gmv": ["净成交", "净GMV", "净销售额"],
    "orders": ["单量", "订单量", "成交单量", "支付订单数"],
    "ad_spend": ["投放消耗", "付费总消耗", "整体消耗", "消耗", "广告消耗"],
    "roi": ["ROI", "整体ROI", "付费ROI"],
    "net_roi": ["净ROI", "净roi"],
    "refund_rate": ["退款率"],
    "refund_amount": ["退款金额"],
    "conversion_rate": ["成交转化率"],
    "click_to_order_rate": ["商品点击-成交率"],
    "commission": ["平台佣金"],
}

CHANNEL_KEYWORDS = ["店铺号商品卡", "洗脸巾直播", "商品卡", "短视频", "直播", "千川", "整体"]
RATE_METRICS = {"refund_rate", "conversion_rate", "click_to_order_rate"}
ROI_METRICS = {"roi", "net_roi"}
MONEY_OR_COUNT_METRICS = {
    "gmv",
    "net_gmv",
    "orders",
    "ad_spend",
    "refund_amount",
    "commission",
}


def identify_brand(filename: str) -> str:
    if "最护" in filename:
        return "最护"
    if "碧维" in filename:
        return "碧维"
    return "未知品牌"


def identify_platform(filename: str) -> str:
    if "抖店" in filename:
        return "抖店"
    if "拼多多" in filename:
        return "拼多多"
    if "千川" in filename:
        return "千川"
    return "未知平台"


def clean_text(value) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    text = text.replace("\n", "").replace("\r", "").replace(" ", "")
    return "" if text.lower() in {"nan", "none"} else text


def normalize_metric_name(text: str) -> str:
    return re.sub(r"[\s:_：/\\（）()【】\[\]-]", "", clean_text(text)).lower()


def map_metric(metric: str) -> str | None:
    normalized = normalize_metric_name(metric)
    if not normalized:
        return None
    for standard, aliases in METRIC_ALIASES.items():
        for alias in aliases:
            if normalize_metric_name(alias) in normalized:
                return standard
    return None


def detect_channel(*parts: str) -> str:
    text = " ".join(clean_text(part) for part in parts if part)
    for keyword in CHANNEL_KEYWORDS:
        if keyword in text:
            return keyword
    return "整体"


def parse_date_cell(value) -> pd.Timestamp | None:
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.normalize()
    if hasattr(value, "date") and not isinstance(value, str):
        try:
            return pd.Timestamp(value).normalize()
        except Exception:
            pass

    text = clean_text(value)
    if not text:
        return None

    text = text.replace("月", ".").replace("日", "")
    text = text.replace("号", "").replace("/", ".").replace("-", ".")
    text = re.sub(r"\(.+?\)", "", text)
    text = re.sub(r"（.+?）", "", text)

    parsed = pd.to_datetime(text, errors="coerce")
    if pd.notna(parsed):
        if parsed.year == 1900:
            parsed = parsed.replace(year=date.today().year)
        return pd.Timestamp(parsed).normalize()

    match = re.search(r"(?:(20\d{2})\.)?(\d{1,2})\.(\d{1,2})", text)
    if match:
        year = int(match.group(1) or date.today().year)
        month = int(match.group(2))
        day = int(match.group(3))
        try:
            return pd.Timestamp(year=year, month=month, day=day)
        except ValueError:
            return None

    return None


def clean_number(value, metric_std: str | None = None) -> float | None:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = clean_text(value)
        if not text or text in {"-", "—", "--", "/", "无"}:
            return None
        percent = "%" in text
        text = (
            text.replace(",", "")
            .replace("，", "")
            .replace("￥", "")
            .replace("¥", "")
            .replace("%", "")
            .replace("元", "")
            .replace("单", "")
        )
        multiplier = 1.0
        if "万" in text:
            multiplier = 10000.0
            text = text.replace("万", "")
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return None
        number = float(match.group()) * multiplier
        if percent:
            number = number / 100.0

    if metric_std in RATE_METRICS and number > 1:
        return number / 100.0
    if metric_std in ROI_METRICS and number > 20:
        return number / 100.0
    return number


def find_date_header(df: pd.DataFrame) -> tuple[int | None, dict[int, pd.Timestamp]]:
    best_row = None
    best_dates: dict[int, pd.Timestamp] = {}
    for row_idx in range(min(len(df), 40)):
        dates = {}
        for col_idx, value in enumerate(df.iloc[row_idx].tolist()):
            parsed = parse_date_cell(value)
            if parsed is not None:
                dates[col_idx] = parsed
        if len(dates) > len(best_dates):
            best_row = row_idx
            best_dates = dates
    if len(best_dates) < 2:
        return None, {}
    return best_row, best_dates


def parse_sheet(df: pd.DataFrame, filename: str, sheet_name: str) -> tuple[list[dict], str | None]:
    header_row, date_cols = find_date_header(df)
    if header_row is None:
        return [], "未找到横向日期列"

    min_date_col = min(date_cols)
    brand = identify_brand(filename)
    platform = identify_platform(filename)
    rows = []

    for row_idx in range(header_row + 1, len(df)):
        row = df.iloc[row_idx]
        left_cells = [clean_text(v) for v in row.iloc[:min_date_col].tolist()]
        left_text = " ".join(cell for cell in left_cells if cell)
        if not left_text:
            continue

        metric_candidates = [cell for cell in left_cells if cell]
        metric = metric_candidates[-1] if metric_candidates else ""
        metric_std = map_metric(metric)
        channel = detect_channel(sheet_name, left_text)

        for col_idx, parsed_date in date_cols.items():
            raw_value = row.iloc[col_idx]
            value = clean_number(raw_value, metric_std)
            if value is None:
                continue
            rows.append(
                {
                    "date": parsed_date.date().isoformat(),
                    "brand": brand,
                    "platform": platform,
                    "channel": channel,
                    "metric": metric,
                    "metric_std": metric_std or "",
                    "value": value,
                    "source_file": filename,
                    "source_sheet": sheet_name,
                }
            )

    if not rows:
        return [], "找到日期列，但未识别到有效指标数据"
    return rows, None


def empty_dataframes() -> tuple[pd.DataFrame, pd.DataFrame]:
    raw_columns = ["date", "brand", "platform", "channel", "metric", "value", "source_file", "source_sheet"]
    return pd.DataFrame(columns=raw_columns), pd.DataFrame(columns=STANDARD_COLUMNS)


def export_normalized_data(raw_df: pd.DataFrame) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    export_df = raw_df.drop(columns=["metric_std"], errors="ignore")
    export_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")


def parse_excel_sources(sources: list[tuple[str, object]]) -> tuple[pd.DataFrame, pd.DataFrame, list[str], list[str]]:
    OUTPUT_DIR.mkdir(exist_ok=True)

    all_rows = []
    warnings = []
    filenames = [name for name, _ in sources]
    missing = [name for name in EXPECTED_FILES if name not in filenames]

    for filename, file_obj in sources:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        try:
            workbook = pd.read_excel(file_obj, sheet_name=None, header=None, engine="openpyxl")
        except Exception as exc:
            warnings.append(f"{filename} 读取失败：{exc}")
            continue

        for sheet_name, df in workbook.items():
            try:
                rows, reason = parse_sheet(df, filename, str(sheet_name))
                if rows:
                    all_rows.extend(rows)
                else:
                    warnings.append(f"{filename} / {sheet_name}：该 sheet 未识别（{reason}）")
            except Exception as exc:
                warnings.append(f"{filename} / {sheet_name}：该 sheet 未识别（{exc}）")

    raw_df = pd.DataFrame(
        all_rows,
        columns=["date", "brand", "platform", "channel", "metric", "metric_std", "value", "source_file", "source_sheet"],
    )

    if raw_df.empty:
        export_normalized_data(raw_df)
        return raw_df, pd.DataFrame(columns=STANDARD_COLUMNS), warnings, missing

    raw_df["date"] = pd.to_datetime(raw_df["date"], errors="coerce")
    raw_df = raw_df.dropna(subset=["date"])
    export_normalized_data(raw_df)

    op_df = build_operating_table(raw_df)
    return raw_df, op_df, warnings, missing


def load_uploaded_data(uploaded_files) -> tuple[pd.DataFrame, pd.DataFrame, list[str], list[str]]:
    if not uploaded_files:
        raw_df, op_df = empty_dataframes()
        export_normalized_data(raw_df)
        return raw_df, op_df, [], EXPECTED_FILES.copy()
    sources = [(uploaded_file.name, uploaded_file) for uploaded_file in uploaded_files]
    return parse_excel_sources(sources)


@st.cache_data(show_spinner=False)
def load_sample_data() -> tuple[pd.DataFrame, pd.DataFrame, list[str], list[str]]:
    SAMPLE_DATA_DIR.mkdir(exist_ok=True)
    sources = [(path.name, path) for path in sorted(SAMPLE_DATA_DIR.glob("*.xlsx"))]
    if not sources:
        raw_df, op_df = empty_dataframes()
        export_normalized_data(raw_df)
        return raw_df, op_df, ["sample_data 文件夹中暂无示例 Excel"], EXPECTED_FILES.copy()
    return parse_excel_sources(sources)


def save_upload_to_database(
    raw_df: pd.DataFrame,
    op_df: pd.DataFrame,
    warnings: list[str],
    uploaded_files,
    uploader_name: str | None,
) -> tuple[bool, dict[str, int | str]]:
    engine = get_engine()
    if engine is None:
        return False, {"message": "请配置数据库连接。本次上传只完成临时解析，未写入数据库。", "inserted": 0, "updated": 0, "failed": 1}

    try:
        init_db(engine)
        file_names = [file.name for file in uploaded_files]
        initial_status = "partial" if warnings else "success"
        batch_id = create_upload_batch(
            file_names=file_names,
            uploader_name=uploader_name,
            status=initial_status,
            message="上传已接收，开始解析入库。",
            engine=engine,
        )
        raw_count = insert_raw_metrics(raw_df, batch_id, engine=engine)
        upsert_counts = upsert_daily_metrics(op_df, batch_id, raw_df=raw_df, engine=engine)
        error_count = insert_import_errors(warnings, batch_id, engine=engine)
        status = "partial" if warnings else "success"
        message = (
            f"入库完成：新增 {upsert_counts['inserted']} 条，更新 {upsert_counts['updated']} 条，"
            f"原始指标 {raw_count} 条，失败/提示 {error_count} 条。"
        )
        if batch_id is not None:
            update_upload_batch(batch_id, status=status, message=message, engine=engine)
        return True, {
            "message": message,
            "inserted": upsert_counts["inserted"],
            "updated": upsert_counts["updated"],
            "failed": error_count,
        }
    except Exception as exc:
        try:
            batch_id = create_upload_batch(
                file_names=[file.name for file in uploaded_files],
                uploader_name=uploader_name,
                status="failed",
                message=str(exc),
                engine=engine,
            )
            insert_import_errors([str(exc)], batch_id, engine=engine)
        except Exception:
            pass
        return False, {"message": f"数据库写入失败：{exc}", "inserted": 0, "updated": 0, "failed": 1}


def load_history_from_database() -> tuple[pd.DataFrame, str]:
    engine = get_engine()
    if engine is None:
        return pd.DataFrame(columns=STANDARD_COLUMNS), "未配置 DATABASE_URL，当前只能查看本次上传或示例数据。"
    try:
        init_db(engine)
        history_df = load_daily_metrics(engine=engine)
        if history_df.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS), "数据库已连接，但还没有历史经营数据。"
        for col in STANDARD_COLUMNS:
            if col not in history_df.columns:
                history_df[col] = pd.NA
        history_df["date"] = pd.to_datetime(history_df["date"], errors="coerce")
        return history_df, f"已从数据库读取 {len(history_df)} 条历史经营数据。"
    except Exception as exc:
        return pd.DataFrame(columns=STANDARD_COLUMNS), f"数据库读取失败：{exc}"


def build_operating_table(raw_df: pd.DataFrame) -> pd.DataFrame:
    mapped = raw_df.copy()
    mapped["metric_std"] = mapped["metric"].apply(map_metric)
    mapped = mapped.dropna(subset=["metric_std"])
    if mapped.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    records = []
    group_cols = ["date", "brand", "platform", "channel"]
    for keys, group in mapped.groupby(group_cols, dropna=False):
        row = dict(zip(group_cols, keys))
        row["source_file"] = "；".join(sorted({str(v) for v in group["source_file"].dropna()}))
        row["source_sheet"] = "；".join(sorted({str(v) for v in group["source_sheet"].dropna()}))
        for metric in STANDARD_COLUMNS[4:]:
            metric_values = group.loc[group["metric_std"] == metric, "value"].dropna()
            if metric_values.empty:
                row[metric] = pd.NA
            elif metric in RATE_METRICS | ROI_METRICS:
                row[metric] = metric_values.mean()
            else:
                row[metric] = metric_values.sum()
        records.append(row)

    op_df = pd.DataFrame(records)
    for col in STANDARD_COLUMNS:
        if col not in op_df.columns:
            op_df[col] = pd.NA
    for col in ["source_file", "source_sheet"]:
        if col not in op_df.columns:
            op_df[col] = pd.NA
    op_df = op_df[STANDARD_COLUMNS + ["source_file", "source_sheet"]].sort_values(["date", "brand", "platform", "channel"])

    for col in STANDARD_COLUMNS[4:]:
        op_df[col] = pd.to_numeric(op_df[col], errors="coerce")

    can_calc_roi = op_df["gmv"].notna() & op_df["ad_spend"].notna() & (op_df["ad_spend"] != 0)
    op_df.loc[can_calc_roi, "roi"] = op_df.loc[can_calc_roi, "gmv"] / op_df.loc[can_calc_roi, "ad_spend"]
    can_calc_net_roi = op_df["net_gmv"].notna() & op_df["ad_spend"].notna() & (op_df["ad_spend"] != 0)
    op_df.loc[can_calc_net_roi, "net_roi"] = op_df.loc[can_calc_net_roi, "net_gmv"] / op_df.loc[can_calc_net_roi, "ad_spend"]
    return op_df


def fmt_number(value, digits: int = 0) -> str:
    if pd.isna(value):
        return "暂无"
    return f"{float(value):,.{digits}f}"


def fmt_ratio(value) -> str:
    if pd.isna(value):
        return "暂无"
    return f"{float(value) * 100:.1f}%"


def fmt_roi(value) -> str:
    if pd.isna(value):
        return "暂无"
    return f"{float(value):.2f}"


def delta_text(current, previous, as_ratio: bool = False) -> str:
    if pd.isna(current) or pd.isna(previous) or previous == 0:
        return "暂无对比"
    change = (current - previous) / abs(previous)
    return f"{change * 100:+.1f}%"


def aggregate_daily(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)
    rows = []
    for keys, group in df.groupby(["date", "brand", "platform", "channel"], dropna=False):
        row = dict(zip(["date", "brand", "platform", "channel"], keys))
        for metric in MONEY_OR_COUNT_METRICS:
            row[metric] = group[metric].sum(min_count=1)
        for metric in RATE_METRICS | ROI_METRICS:
            row[metric] = group[metric].mean()
        if pd.notna(row.get("gmv")) and pd.notna(row.get("ad_spend")) and row.get("ad_spend") not in [0, None]:
            row["roi"] = row["gmv"] / row["ad_spend"]
        if pd.notna(row.get("net_gmv")) and pd.notna(row.get("ad_spend")) and row.get("ad_spend") not in [0, None]:
            row["net_roi"] = row["net_gmv"] / row["ad_spend"]
        rows.append(row)
    return pd.DataFrame(rows)


def latest_and_previous(df: pd.DataFrame) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    if df.empty or df["date"].dropna().empty:
        return None, None
    dates = sorted(df["date"].dropna().unique())
    latest = dates[-1]
    previous = dates[-2] if len(dates) >= 2 else None
    return latest, previous


def make_metric_card(label: str, value: str, help_text: str | None = None):
    st.metric(label, value, help=help_text)


def get_app_password() -> str:
    secret_password = ""
    try:
        secret_password = st.secrets.get("APP_PASSWORD", "")
    except Exception:
        secret_password = ""
    return secret_password or os.getenv("APP_PASSWORD", "")


def require_password() -> bool:
    password = get_app_password()
    if not password:
        st.warning("安全提醒：当前未设置 APP_PASSWORD，页面默认允许访问。部署前请在 Streamlit Secrets 或 Render 环境变量中设置访问密码。")
        return True

    if st.session_state.get("authenticated"):
        return True

    st.subheader("访问验证")
    entered = st.text_input("请输入访问密码", type="password")
    if st.button("进入看板"):
        if entered == password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("密码不正确")
    return False


def render_line_chart(df: pd.DataFrame, y_col: str, title: str, formatter: str | None = None):
    chart_df = df.dropna(subset=[y_col]).copy()
    if chart_df.empty:
        st.info(f"{title}：暂无数据")
        return
    chart_df["分组"] = chart_df["brand"] + "-" + chart_df["platform"] + "-" + chart_df["channel"]
    fig = px.line(chart_df, x="date", y=y_col, color="分组", markers=True, title=title)
    fig.update_layout(height=360, margin=dict(l=10, r=10, t=50, b=10), legend_title_text="")
    if formatter == "percent":
        fig.update_yaxes(tickformat=".1%")
    st.plotly_chart(fig, use_container_width=True)


def render_bar_chart(df: pd.DataFrame, y_col: str, title: str, formatter: str | None = None):
    chart_df = df.dropna(subset=[y_col]).copy()
    if chart_df.empty:
        st.info(f"{title}：暂无数据")
        return
    fig = px.bar(chart_df, x="channel", y=y_col, color="brand", barmode="group", title=title)
    fig.update_layout(height=340, margin=dict(l=10, r=10, t=50, b=10), legend_title_text="")
    if formatter == "percent":
        fig.update_yaxes(tickformat=".1%")
    st.plotly_chart(fig, use_container_width=True)


def filter_df(df: pd.DataFrame, brand: str, platform: str, channel: str, date_range) -> pd.DataFrame:
    filtered = df.copy()
    if date_range and len(date_range) == 2:
        start, end = pd.to_datetime(date_range[0]), pd.to_datetime(date_range[1])
        filtered = filtered[(filtered["date"] >= start) & (filtered["date"] <= end)]
    if brand != "全部":
        filtered = filtered[filtered["brand"] == brand]
    if platform != "全部":
        filtered = filtered[filtered["platform"] == platform]
    if channel != "全部":
        filtered = filtered[filtered["channel"] == channel]
    return filtered


def aggregate_for_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    if df.empty or period == "每日":
        return df.copy()

    working = df.copy()
    working["date"] = pd.to_datetime(working["date"], errors="coerce")
    working = working.dropna(subset=["date"])
    if period == "每周":
        working["period_date"] = working["date"].dt.to_period("W-SUN").dt.start_time
    elif period == "每月":
        working["period_date"] = working["date"].dt.to_period("M").dt.start_time
    else:
        return working

    rows = []
    group_cols = ["period_date", "brand", "platform", "channel"]
    for keys, group in working.groupby(group_cols, dropna=False):
        row = {
            "date": keys[0],
            "brand": keys[1],
            "platform": keys[2],
            "channel": keys[3],
        }
        for metric in MONEY_OR_COUNT_METRICS:
            row[metric] = group[metric].sum(min_count=1)
        for metric in RATE_METRICS | ROI_METRICS:
            row[metric] = group[metric].mean()
        if pd.notna(row.get("gmv")) and pd.notna(row.get("ad_spend")) and row.get("ad_spend") not in [0, None]:
            row["roi"] = row["gmv"] / row["ad_spend"]
        if pd.notna(row.get("net_gmv")) and pd.notna(row.get("ad_spend")) and row.get("ad_spend") not in [0, None]:
            row["net_roi"] = row["net_gmv"] / row["ad_spend"]
        rows.append(row)
    return pd.DataFrame(rows)


def brand_reason(brand: str, alert_type: str) -> str:
    if brand == "最护":
        return "重点检查洗脸巾在抖店直播、商品卡、短视频和千川投放中的流量承接、转化链路和退款风险。"
    if brand == "碧维":
        return "重点检查抹布在拼多多基本盘、抖店起量、低价转化、投放效率和退款风险上的变化。"
    return "检查流量、转化、投放效率和售后风险。"


def generate_alerts(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    alerts = []
    sorted_df = df.sort_values(["brand", "platform", "channel", "date"])
    for (brand, platform, channel), group in sorted_df.groupby(["brand", "platform", "channel"]):
        group = group.sort_values("date").reset_index(drop=True)
        for idx, row in group.iterrows():
            prev = group.iloc[idx - 1] if idx > 0 else None
            context = {
                "brand": brand,
                "platform": platform,
                "channel": channel,
                "date": row["date"].date().isoformat() if pd.notna(row["date"]) else "",
            }

            gmv = row.get("gmv")
            roi = row.get("roi")
            ad_spend = row.get("ad_spend")
            refund_rate = row.get("refund_rate")
            conversion_rate = row.get("conversion_rate")

            if prev is not None:
                prev_gmv = prev.get("gmv")
                prev_roi = prev.get("roi")
                prev_refund = prev.get("refund_rate")
                prev_conversion = prev.get("conversion_rate")

                if pd.notna(gmv) and pd.notna(prev_gmv) and prev_gmv > 0 and (gmv - prev_gmv) / prev_gmv < -0.1:
                    alerts.append(
                        {
                            **context,
                            "标题": "销售下滑",
                            "严重程度": "中",
                            "数据依据": f"GMV 较前一日下降 {abs((gmv - prev_gmv) / prev_gmv) * 100:.1f}%",
                            "可能原因": brand_reason(brand, "销售下滑"),
                            "建议动作": "检查流量、直播节奏、商品承接、价格活动等因素。",
                        }
                    )
                if pd.notna(roi) and pd.notna(prev_roi) and prev_roi > 0 and (roi - prev_roi) / prev_roi < -0.1:
                    alerts.append(
                        {
                            **context,
                            "标题": "投放效率下降",
                            "严重程度": "中",
                            "数据依据": f"ROI 较前一日下降 {abs((roi - prev_roi) / prev_roi) * 100:.1f}%",
                            "可能原因": brand_reason(brand, "投放效率下降"),
                            "建议动作": "检查投放计划、素材、人群、出价和成交转化。",
                        }
                    )
                if (
                    pd.notna(conversion_rate)
                    and pd.notna(prev_conversion)
                    and prev_conversion > 0
                    and (conversion_rate - prev_conversion) / prev_conversion < -0.1
                ):
                    alerts.append(
                        {
                            **context,
                            "标题": "转化承接问题",
                            "严重程度": "中",
                            "数据依据": f"成交转化率较前一日下降 {abs((conversion_rate - prev_conversion) / prev_conversion) * 100:.1f}%",
                            "可能原因": brand_reason(brand, "转化承接问题"),
                            "建议动作": "检查商品价格、主图、详情页、评价、客服和流量质量。",
                        }
                    )
                if (
                    pd.notna(gmv)
                    and pd.notna(prev_gmv)
                    and gmv > prev_gmv
                    and pd.notna(refund_rate)
                    and pd.notna(prev_refund)
                    and refund_rate > prev_refund
                ):
                    alerts.append(
                        {
                            **context,
                            "标题": "增长质量风险",
                            "严重程度": "中",
                            "数据依据": f"GMV 增长，同时退款率从 {prev_refund * 100:.1f}% 升至 {refund_rate * 100:.1f}%",
                            "可能原因": brand_reason(brand, "增长质量风险"),
                            "建议动作": "检查新增流量质量，避免 GMV 增长但售后风险扩大。",
                        }
                    )

            if pd.notna(ad_spend) and ad_spend > 0 and (pd.isna(gmv) or gmv == 0):
                alerts.append(
                    {
                        **context,
                        "标题": "无效消耗",
                        "严重程度": "高",
                        "数据依据": f"投放消耗 {ad_spend:.0f}，GMV 为 0 或为空",
                        "可能原因": brand_reason(brand, "无效消耗"),
                        "建议动作": "暂停或降低低效投放，检查素材、落地页、商品价格和转化承接。",
                    }
                )
            if pd.notna(refund_rate) and refund_rate > 0.1:
                alerts.append(
                    {
                        **context,
                        "标题": "退款风险",
                        "严重程度": "高",
                        "数据依据": f"退款率 {refund_rate * 100:.1f}%，超过 10%",
                        "可能原因": brand_reason(brand, "退款风险"),
                        "建议动作": "检查商品预期差、详情页表达、客服承接、质量问题和售后原因。",
                    }
                )
            if pd.notna(roi) and roi > 5 and pd.notna(gmv) and gmv < 1000:
                alerts.append(
                    {
                        **context,
                        "标题": "高效率低规模",
                        "严重程度": "低",
                        "数据依据": f"ROI {roi:.2f}，但 GMV 仅 {gmv:.0f}",
                        "可能原因": brand_reason(brand, "高效率低规模"),
                        "建议动作": "可以小幅测试放量，但要观察退款率和转化稳定性。",
                    }
                )
    return pd.DataFrame(alerts)


def render_boss_home(op_df: pd.DataFrame):
    st.subheader("老板首页")
    main_df = op_df[op_df["platform"].isin(["抖店", "拼多多"])].copy()
    latest, previous = latest_and_previous(main_df)
    if latest is None:
        st.info("暂无可展示的经营数据。请先上传 Excel 复盘表，或勾选使用示例数据。")
        return

    st.caption(f"最新有数据日期：{pd.Timestamp(latest).date().isoformat()}。总 GMV 默认不包含千川，避免与抖店重复。")

    latest_df = main_df[main_df["date"] == latest]
    prev_df = main_df[main_df["date"] == previous] if previous is not None else pd.DataFrame()

    total_gmv = latest_df["gmv"].sum(min_count=1)
    total_orders = latest_df["orders"].sum(min_count=1)
    total_spend = latest_df["ad_spend"].sum(min_count=1)
    total_net_gmv = latest_df["net_gmv"].sum(min_count=1)
    total_roi = total_gmv / total_spend if pd.notna(total_gmv) and pd.notna(total_spend) and total_spend else latest_df["roi"].mean()
    total_net_roi = total_net_gmv / total_spend if pd.notna(total_net_gmv) and pd.notna(total_spend) and total_spend else latest_df["net_roi"].mean()
    total_refund_rate = latest_df["refund_rate"].mean()

    prev_gmv = prev_df["gmv"].sum(min_count=1) if not prev_df.empty else pd.NA
    prev_spend = prev_df["ad_spend"].sum(min_count=1) if not prev_df.empty else pd.NA
    prev_roi = prev_gmv / prev_spend if pd.notna(prev_gmv) and pd.notna(prev_spend) and prev_spend else pd.NA

    cols = st.columns(4)
    cols[0].metric("总 GMV", fmt_number(total_gmv), delta_text(total_gmv, prev_gmv))
    cols[1].metric("总单量", fmt_number(total_orders), None)
    cols[2].metric("总投放消耗", fmt_number(total_spend), None)
    cols[3].metric("整体 ROI", fmt_roi(total_roi), delta_text(total_roi, prev_roi))

    cols = st.columns(4)
    cols[0].metric("净 ROI", fmt_roi(total_net_roi))
    cols[1].metric("退款率", fmt_ratio(total_refund_rate))
    cols[2].metric("较前一日 GMV 变化", delta_text(total_gmv, prev_gmv))
    cols[3].metric("较前一日 ROI 变化", delta_text(total_roi, prev_roi))

    st.markdown("### 四个经营卡片")
    for brand, platform in [("最护", "抖店"), ("最护", "拼多多"), ("碧维", "抖店"), ("碧维", "拼多多")]:
        card_df = main_df[(main_df["brand"] == brand) & (main_df["platform"] == platform)]
        latest_card = card_df[card_df["date"] == latest]
        prev_card = card_df[card_df["date"] == previous] if previous is not None else pd.DataFrame()
        with st.container(border=True):
            st.markdown(f"#### {brand} - {platform}")
            if latest_card.empty:
                st.info("暂无数据")
                continue
            gmv = latest_card["gmv"].sum(min_count=1)
            orders = latest_card["orders"].sum(min_count=1)
            spend = latest_card["ad_spend"].sum(min_count=1)
            net_gmv = latest_card["net_gmv"].sum(min_count=1)
            roi = gmv / spend if pd.notna(gmv) and pd.notna(spend) and spend else latest_card["roi"].mean()
            net_roi = net_gmv / spend if pd.notna(net_gmv) and pd.notna(spend) and spend else latest_card["net_roi"].mean()
            refund_rate = latest_card["refund_rate"].mean()
            old_gmv = prev_card["gmv"].sum(min_count=1) if not prev_card.empty else pd.NA
            old_spend = prev_card["ad_spend"].sum(min_count=1) if not prev_card.empty else pd.NA
            old_roi = old_gmv / old_spend if pd.notna(old_gmv) and pd.notna(old_spend) and old_spend else pd.NA
            cols = st.columns(4)
            cols[0].metric("GMV", fmt_number(gmv), delta_text(gmv, old_gmv))
            cols[1].metric("单量", fmt_number(orders))
            cols[2].metric("投放消耗", fmt_number(spend))
            cols[3].metric("ROI", fmt_roi(roi), delta_text(roi, old_roi))
            cols = st.columns(3)
            cols[0].metric("净 ROI", fmt_roi(net_roi))
            cols[1].metric("退款率", fmt_ratio(refund_rate))
            cols[2].metric("较前一日 GMV / ROI", f"{delta_text(gmv, old_gmv)} / {delta_text(roi, old_roi)}")


def render_trends(op_df: pd.DataFrame):
    st.subheader("趋势分析")
    if op_df.empty:
        st.info("暂无数据")
        return
    min_date = op_df["date"].min().date()
    max_date = op_df["date"].max().date()
    cols = st.columns(5)
    date_range = cols[0].date_input("日期范围", value=(min_date, max_date), min_value=min_date, max_value=max_date)
    brand = cols[1].selectbox("品牌", ["全部"] + sorted(op_df["brand"].dropna().unique().tolist()))
    platform = cols[2].selectbox("平台", ["全部"] + sorted(op_df["platform"].dropna().unique().tolist()))
    channel_options = ["全部", "整体", "商品卡", "直播", "短视频", "千川"]
    channel = cols[3].selectbox("渠道", channel_options)
    period = cols[4].selectbox("趋势粒度", ["每日", "每周", "每月"])

    filtered = filter_df(op_df, brand, platform, channel, date_range)
    trend_df = aggregate_for_period(filtered, period)
    render_line_chart(trend_df, "gmv", f"GMV {period}趋势图")
    render_line_chart(trend_df, "orders", f"单量 {period}趋势图")
    render_line_chart(trend_df, "ad_spend", f"投放消耗 {period}趋势图")
    render_line_chart(trend_df, "roi", f"ROI {period}趋势图")
    render_line_chart(trend_df, "net_roi", f"净 ROI {period}趋势图")
    render_line_chart(trend_df, "refund_rate", f"退款率 {period}趋势图", formatter="percent")


def render_channel_analysis(op_df: pd.DataFrame):
    st.subheader("渠道分析")
    if op_df.empty:
        st.info("暂无数据")
        return

    douyin_channels = ["整体", "商品卡", "直播", "短视频", "店铺号商品卡", "洗脸巾直播"]
    douyin_df = op_df[(op_df["platform"] == "抖店") & (op_df["channel"].isin(douyin_channels))].copy()
    if douyin_df.empty:
        st.info("暂无抖店渠道数据")
    else:
        latest, _ = latest_and_previous(douyin_df)
        latest_douyin = douyin_df[douyin_df["date"] == latest]
        st.caption(f"抖店渠道最新日期：{pd.Timestamp(latest).date().isoformat()}")
        render_bar_chart(latest_douyin, "gmv", "各渠道 GMV 对比")
        render_bar_chart(latest_douyin, "orders", "各渠道单量对比")
        render_bar_chart(latest_douyin, "ad_spend", "各渠道投放消耗对比")
        render_bar_chart(latest_douyin, "roi", "各渠道 ROI 对比")
        render_bar_chart(latest_douyin, "refund_rate", "各渠道退款率对比", formatter="percent")

    qianchuan_df = op_df[op_df["platform"] == "千川"].copy()
    st.markdown("### 千川投放补充分析")
    st.caption("千川只作为投放补充分析，不与抖店 GMV 合并计算。")
    if qianchuan_df.empty:
        st.info("暂无千川数据")
    else:
        render_line_chart(qianchuan_df, "gmv", "千川成交趋势")
        render_line_chart(qianchuan_df, "net_gmv", "千川净成交趋势")
        render_line_chart(qianchuan_df, "ad_spend", "千川消耗趋势")
        render_line_chart(qianchuan_df, "roi", "千川 ROI 趋势")
        render_line_chart(qianchuan_df, "net_roi", "千川净 ROI 趋势")


def render_alerts(op_df: pd.DataFrame):
    st.subheader("自动复盘提醒")
    alerts = generate_alerts(op_df)
    if alerts.empty:
        st.success("当前没有触发规则提醒。")
        return
    severity_order = {"高": 0, "中": 1, "低": 2}
    alerts["排序"] = alerts["严重程度"].map(severity_order).fillna(9)
    alerts = alerts.sort_values(["排序", "date"], ascending=[True, False]).drop(columns=["排序"])
    st.dataframe(
        alerts[
            [
                "date",
                "brand",
                "platform",
                "channel",
                "标题",
                "严重程度",
                "数据依据",
                "可能原因",
                "建议动作",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )


def render_data_preview(raw_df: pd.DataFrame, op_df: pd.DataFrame, warnings: list[str], missing: list[str]):
    with st.expander("数据预览与解析提示", expanded=False):
        if missing:
            st.warning("以下预期文件暂未找到：" + "、".join(missing))
        if warnings:
            st.info("解析提示：")
            for item in warnings[:30]:
                st.write(f"- {item}")
            if len(warnings) > 30:
                st.write(f"- 还有 {len(warnings) - 30} 条提示未展示")
        st.write(f"标准长表导出位置：`{OUTPUT_CSV}`")
        st.markdown("#### 标准长表预览")
        st.dataframe(raw_df.head(100), use_container_width=True, hide_index=True)
        st.markdown("#### 经营指标表预览")
        st.dataframe(op_df.head(100), use_container_width=True, hide_index=True)


def render_database_setup() -> bool:
    with st.expander("数据库连接", expanded=not bool(get_database_url())):
        database_url = get_database_url()
        if not database_url:
            st.error("请配置数据库连接")
            st.caption("在 Streamlit Cloud 的 Secrets 或部署环境变量中设置 DATABASE_URL。")
            return False

        st.success("已检测到 DATABASE_URL。")
        if st.button("初始化数据库"):
            try:
                init_db()
                st.success("数据库初始化完成，必要数据表已创建。")
            except Exception as exc:
                st.error(f"数据库初始化失败：{exc}")
                return False
        return True


def render_upload_tab() -> tuple[pd.DataFrame, pd.DataFrame, list[str], list[str]]:
    st.subheader("上传数据")
    st.info("上传的 Excel 会被解析并写入数据库；原始 Excel 文件本身不会被永久保存。真实经营数据不要提交到 GitHub。")
    uploader_name = st.text_input("上传人（可选）", value="", placeholder="例如：运营同事姓名")
    uploaded_files = st.file_uploader(
        "上传 Excel 复盘表",
        type=["xlsx"],
        accept_multiple_files=True,
        help="支持一次上传多个 Excel，系统会按文件名识别最护、碧维、抖店、拼多多、千川。",
    )
    if not uploaded_files:
        raw_df, op_df = empty_dataframes()
        export_normalized_data(raw_df)
        st.warning("请上传 Excel 复盘表。")
        return raw_df, op_df, [], EXPECTED_FILES.copy()

    unknown_files = [
        file.name
        for file in uploaded_files
        if identify_brand(file.name) == "未知品牌" or identify_platform(file.name) == "未知平台"
    ]
    if unknown_files:
        st.warning("以下文件名未能完整识别品牌或平台：" + "、".join(unknown_files))

    with st.spinner("正在解析上传的 Excel..."):
        raw_df, op_df, warnings, missing = load_uploaded_data(uploaded_files)
    render_data_preview(raw_df, op_df, warnings, missing)

    if op_df.empty:
        st.warning("上传文件已读取，但没有解析出可入库的经营指标。")
    elif st.button("确认导入数据库", type="primary"):
        with st.spinner("正在写入数据库..."):
            ok, result = save_upload_to_database(raw_df, op_df, warnings, uploaded_files, uploader_name.strip() or None)
        if ok:
            st.success(result["message"])
            cols = st.columns(3)
            cols[0].metric("新增数量", result["inserted"])
            cols[1].metric("更新数量", result["updated"])
            cols[2].metric("失败/提示数量", result["failed"])
        else:
            st.error(result["message"])
    return raw_df, op_df, warnings, missing


def main():
    st.set_page_config(page_title="电商经营复盘看板", layout="wide")
    st.title("电商经营复盘看板")
    st.caption("最护和碧维是不同品类品牌，本看板展示各自经营状态，不做品牌输赢对比。")
    st.caption("千川数据只作为投放补充分析，不默认计入总 GMV，避免和抖店重复。")

    if not require_password():
        return

    render_database_setup()

    tab_home, tab_trend, tab_upload, tab_channel, tab_alert = st.tabs(["老板首页", "历史趋势", "上传数据", "渠道分析", "自动复盘提醒"])

    with tab_upload:
        raw_df, op_df, warnings, missing = render_upload_tab()

    history_df, history_message = load_history_from_database()
    if history_message:
        st.caption(history_message)

    display_df = history_df

    if display_df.empty:
        with tab_home:
            st.warning("暂无历史数据。请到“上传数据”Tab 上传 Excel，并点击“确认导入数据库”。")
        with tab_trend:
            st.info("数据库中暂无可展示的历史趋势。")
        with tab_channel:
            st.info("数据库中暂无渠道数据。")
        with tab_alert:
            st.info("数据库中暂无可生成提醒的数据。")
        return

    with tab_home:
        render_boss_home(display_df)
    with tab_trend:
        render_trends(display_df)
    with tab_channel:
        render_channel_analysis(display_df)
    with tab_alert:
        render_alerts(display_df)


if __name__ == "__main__":
    main()
