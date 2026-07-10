from __future__ import annotations

import re
import os
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

from db import get_engine, init_db, load_daily_metrics


load_dotenv(override=True)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_CSV = OUTPUT_DIR / "normalized_data.csv"

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


def get_default_year() -> int:
    env_year = os.getenv("DEFAULT_YEAR", "").strip()
    if env_year.isdigit() and len(env_year) == 4:
        return int(env_year)
    return date.today().year


def identify_year(text: str) -> int | None:
    cleaned = clean_text(text)
    patterns = [
        r"(20\d{2})\s*年\s*(?:1[0-2]|0?[1-9])\s*月",
        r"(20\d{2})[-./](?:1[0-2]|0?[1-9])",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if match:
            return int(match.group(1))
    return None


def identify_month(text: str) -> int | None:
    cleaned = clean_text(text)
    patterns = [
        r"20\d{2}\s*年\s*(1[0-2]|0?[1-9])\s*月份?",
        r"20\d{2}[-./](1[0-2]|0?[1-9])",
        r"(1[0-2]|0?[1-9])\s*月份?",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if match:
            month = int(match.group(1))
            if 1 <= month <= 12:
                return month
    return None


def identify_period(*parts: str) -> tuple[int, int | None]:
    text = " ".join(clean_text(part) for part in parts if part)
    return identify_year(text) or get_default_year(), identify_month(text)


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


def parse_date_cell(value, default_year: int | None = None, file_month: int | None = None) -> pd.Timestamp | None:
    default_year = default_year or get_default_year()
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

    text = re.sub(r"\(.+?\)", "", text)
    text = re.sub(r"（.+?）", "", text)
    normalized = text.replace("月", ".").replace("日", "")
    normalized = normalized.replace("号", "").replace("/", ".").replace("-", ".")

    full_match = re.fullmatch(r"(20\d{2})\.(\d{1,2})\.(\d{1,2})", normalized)
    if full_match:
        try:
            return pd.Timestamp(year=int(full_match.group(1)), month=int(full_match.group(2)), day=int(full_match.group(3)))
        except ValueError:
            return None

    match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", normalized)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        try:
            return pd.Timestamp(year=default_year, month=month, day=day)
        except ValueError:
            return None

    single_day = re.fullmatch(r"\d{1,2}", normalized)
    if single_day and file_month:
        try:
            return pd.Timestamp(year=default_year, month=file_month, day=int(normalized))
        except ValueError:
            return None
    if single_day:
        return None

    parsed = pd.to_datetime(text, errors="coerce")
    if pd.notna(parsed):
        parsed = pd.Timestamp(parsed).normalize()
        if parsed.year == 1900:
            parsed = parsed.replace(year=default_year)
        return parsed

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


def find_date_header(df: pd.DataFrame, default_year: int | None = None, file_month: int | None = None) -> tuple[int | None, dict[int, pd.Timestamp]]:
    best_row = None
    best_dates: dict[int, pd.Timestamp] = {}
    for row_idx in range(min(len(df), 40)):
        dates = {}
        for col_idx, value in enumerate(df.iloc[row_idx].tolist()):
            parsed = parse_date_cell(value, default_year=default_year, file_month=file_month)
            if parsed is not None:
                dates[col_idx] = parsed
        if len(dates) > len(best_dates):
            best_row = row_idx
            best_dates = dates
    if len(best_dates) < 2:
        return None, {}
    return best_row, best_dates


def parse_sheet(df: pd.DataFrame, filename: str, sheet_name: str) -> tuple[list[dict], str | None]:
    default_year, file_month = identify_period(filename, sheet_name)
    header_row, date_cols = find_date_header(df, default_year=default_year, file_month=file_month)
    if header_row is None:
        if file_month is None:
            return [], "未找到横向日期列，且无法从文件名或 sheet 名识别月份"
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
    missing: list[str] = []

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
        return raw_df, op_df, [], []
    sources = [(uploaded_file.name, uploaded_file) for uploaded_file in uploaded_files]
    return parse_excel_sources(sources)


def load_history_from_database() -> tuple[pd.DataFrame, str]:
    engine = get_engine()
    if engine is None:
        return pd.DataFrame(columns=STANDARD_COLUMNS), "未配置 DATABASE_URL，请管理员配置数据库连接。"
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


def inject_custom_css():
    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.6rem; padding-bottom: 2rem; }
        .main-title { font-size: 30px; font-weight: 700; color: #111827; margin-bottom: 4px; }
        .subtle-note { color: #6b7280; font-size: 14px; line-height: 1.6; margin-bottom: 14px; }
        .metric-card {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 16px 18px;
            box-shadow: 0 4px 14px rgba(15, 23, 42, 0.06);
            min-height: 112px;
            margin-bottom: 12px;
        }
        .metric-label { color: #6b7280; font-size: 13px; margin-bottom: 8px; }
        .metric-value { color: #111827; font-size: 27px; font-weight: 750; line-height: 1.25; }
        .metric-help { color: #9ca3af; font-size: 12px; margin-top: 6px; }
        .delta-pos { color: #059669; font-size: 13px; font-weight: 650; margin-top: 8px; }
        .delta-neg { color: #dc2626; font-size: 13px; font-weight: 650; margin-top: 8px; }
        .delta-flat { color: #6b7280; font-size: 13px; font-weight: 650; margin-top: 8px; }
        .section-title { color: #111827; font-size: 19px; font-weight: 700; margin: 18px 0 10px 0; }
        .info-box {
            background: #f8fafc;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 14px 16px;
            margin-bottom: 14px;
            color: #374151;
        }
        .alert-card {
            border-radius: 12px;
            padding: 14px 16px;
            margin-bottom: 12px;
            border: 1px solid #e5e7eb;
        }
        .alert-high { background: #fef2f2; border-color: #fecaca; }
        .alert-mid { background: #fff7ed; border-color: #fed7aa; }
        .alert-low { background: #eff6ff; border-color: #bfdbfe; }
        .tag { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 700; margin-left: 8px; }
        .tag-high { background: #dc2626; color: #fff; }
        .tag-mid { background: #f97316; color: #fff; }
        .tag-low { background: #2563eb; color: #fff; }
        div[data-testid="stPlotlyChart"] { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 10px; box-shadow: 0 4px 14px rgba(15, 23, 42, 0.04); }
        </style>
        """,
        unsafe_allow_html=True,
    )


def safe_float(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_number(value, digits: int = 0) -> str:
    number = safe_float(value)
    if number is None:
        return "暂无"
    return f"{number:,.{digits}f}"


def format_percent(value) -> str:
    number = safe_float(value)
    if number is None:
        return "暂无"
    return f"{number * 100:.1f}%"


def format_roi(value) -> str:
    number = safe_float(value)
    if number is None:
        return "暂无"
    return f"{number:.2f}"


def format_delta(current, previous) -> tuple[str, str]:
    current_num = safe_float(current)
    previous_num = safe_float(previous)
    if current_num is None or previous_num is None or previous_num == 0:
        return "暂无对比", "flat"
    change = (current_num - previous_num) / abs(previous_num)
    if change > 0:
        return f"+{change * 100:.1f}%", "pos"
    if change < 0:
        return f"{change * 100:.1f}%", "neg"
    return "0.0%", "flat"


def ratio(numerator, denominator) -> float | None:
    n = safe_float(numerator)
    d = safe_float(denominator)
    if n is None or d is None or d == 0:
        return None
    return n / d


def aggregate_metrics(df: pd.DataFrame) -> dict:
    result = {metric: None for metric in STANDARD_COLUMNS[4:]}
    if df.empty:
        return result
    working = df.copy()
    for col in STANDARD_COLUMNS[4:]:
        if col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce")

    for metric in MONEY_OR_COUNT_METRICS:
        if metric in working.columns:
            value = working[metric].sum(min_count=1)
            result[metric] = None if pd.isna(value) else float(value)

    result["roi"] = ratio(result.get("gmv"), result.get("ad_spend"))
    result["net_roi"] = ratio(result.get("net_gmv"), result.get("ad_spend"))
    result["refund_rate"] = ratio(result.get("refund_amount"), result.get("gmv"))
    if result["refund_rate"] is None and "refund_rate" in working.columns:
        mean_refund = working["refund_rate"].dropna().mean()
        result["refund_rate"] = None if pd.isna(mean_refund) else float(mean_refund)
    return result


def get_available_dates(df: pd.DataFrame) -> list[pd.Timestamp]:
    if df.empty or "date" not in df.columns:
        return []
    dates = pd.to_datetime(df["date"], errors="coerce").dropna().dt.normalize().drop_duplicates().sort_values()
    return list(dates)


def get_previous_available_date(df: pd.DataFrame, selected_date) -> pd.Timestamp | None:
    dates = [d for d in get_available_dates(df) if d < pd.Timestamp(selected_date).normalize()]
    return dates[-1] if dates else None


def filter_by_selected_date(df: pd.DataFrame, selected_date, mode: str) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    selected = pd.Timestamp(selected_date).normalize()
    working = df.copy()
    working["date"] = pd.to_datetime(working["date"], errors="coerce").dt.normalize()
    if mode == "本月累计":
        month_start = selected.replace(day=1)
        return working[(working["date"] >= month_start) & (working["date"] <= selected)]
    return working[working["date"] == selected]


def render_metric_card(title: str, value: str, delta: str | None = None, delta_state: str = "flat", help_text: str | None = None):
    delta_html = ""
    if delta is not None:
        delta_html = f'<div class="delta-{delta_state}">{delta}</div>'
    help_html = f'<div class="metric-help">{help_text}</div>' if help_text else ""
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">{title}</div>
            <div class="metric-value">{value}</div>
            {delta_html}
            {help_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_info_box(text: str):
    st.markdown(f'<div class="info-box">{text}</div>', unsafe_allow_html=True)


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
        st.warning("安全提醒：当前未设置 APP_PASSWORD，页面默认允许访问。部署前请在 Streamlit Secrets 中设置访问密码。")
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


def render_chart(df: pd.DataFrame, y_col: str, title: str, formatter: str | None = None):
    chart_df = df.dropna(subset=[y_col]).copy() if y_col in df.columns else pd.DataFrame()
    if chart_df.empty:
        render_info_box("当前筛选条件下暂无数据")
        return
    chart_df["分组"] = chart_df["brand"].astype(str) + "-" + chart_df["platform"].astype(str) + "-" + chart_df["channel"].astype(str)
    fig = px.line(chart_df, x="date", y=y_col, color="分组", markers=True, title=title)
    fig.update_layout(height=350, margin=dict(l=10, r=10, t=48, b=10), legend_title_text="", hovermode="x unified")
    fig.update_traces(line=dict(width=2.4), marker=dict(size=6))
    if formatter == "percent":
        fig.update_yaxes(tickformat=".1%")
    st.plotly_chart(fig, use_container_width=True)


def render_bar(df: pd.DataFrame, y_col: str, title: str, formatter: str | None = None):
    chart_df = df.dropna(subset=[y_col]).copy() if y_col in df.columns else pd.DataFrame()
    if chart_df.empty:
        render_info_box("当前筛选条件下暂无数据")
        return
    fig = px.bar(chart_df, x="channel", y=y_col, color="brand", barmode="group", title=title)
    fig.update_layout(height=340, margin=dict(l=10, r=10, t=48, b=10), legend_title_text="")
    if formatter == "percent":
        fig.update_yaxes(tickformat=".1%")
    st.plotly_chart(fig, use_container_width=True)


def render_date_controls(op_df: pd.DataFrame) -> tuple[pd.Timestamp | None, str, pd.Timestamp | None]:
    dates = get_available_dates(op_df)
    if not dates:
        return None, "本日数据", None
    latest = dates[-1]
    years = sorted({d.year for d in dates})
    default_year_index = years.index(latest.year)

    cols = st.columns([1, 1, 1.2, 1.3, 1.7, 1.7])
    year = cols[0].selectbox("年份", years, index=default_year_index)
    months = sorted({d.month for d in dates if d.year == year})
    default_month = latest.month if latest.year == year and latest.month in months else months[-1]
    month = cols[1].selectbox("月份", months, index=months.index(default_month), format_func=lambda m: f"{m}月")
    day_dates = [d for d in dates if d.year == year and d.month == month]
    default_day = latest if latest in day_dates else day_dates[-1]
    selected_date = cols[2].selectbox("日期", day_dates, index=day_dates.index(default_day), format_func=lambda d: pd.Timestamp(d).strftime("%Y-%m-%d"))
    mode = cols[3].radio("查看口径", ["本日数据", "本月累计"], horizontal=True)
    previous_date = get_previous_available_date(op_df, selected_date)
    cols[4].markdown(f"**当前查看日期**  \n{pd.Timestamp(selected_date).date()}")
    compare_text = "本月累计暂无对比" if mode == "本月累计" else (str(pd.Timestamp(previous_date).date()) if previous_date is not None else "暂无对比")
    cols[5].markdown(f"**对比日期**  \n{compare_text}")
    return pd.Timestamp(selected_date), mode, previous_date


def main_scope(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["platform"].isin(["抖店", "拼多多"])].copy() if not df.empty else df.copy()


def render_business_card(brand: str, platform: str, df: pd.DataFrame, previous_df: pd.DataFrame | None, mode: str):
    card_df = df[(df["brand"] == brand) & (df["platform"] == platform)] if not df.empty else pd.DataFrame()
    with st.container(border=True):
        st.markdown(f"#### {brand} - {platform}")
        if card_df.empty:
            st.info("暂无数据")
            return
        current = aggregate_metrics(card_df)
        previous = aggregate_metrics(previous_df[(previous_df["brand"] == brand) & (previous_df["platform"] == platform)]) if previous_df is not None and not previous_df.empty else {}
        gmv_delta, gmv_state = ("本月累计暂无对比", "flat") if mode == "本月累计" else format_delta(current.get("gmv"), previous.get("gmv"))
        roi_delta, roi_state = ("本月累计暂无对比", "flat") if mode == "本月累计" else format_delta(current.get("roi"), previous.get("roi"))
        cols = st.columns(3)
        with cols[0]:
            render_metric_card("GMV", format_number(current.get("gmv")), gmv_delta, gmv_state)
        with cols[1]:
            render_metric_card("单量", format_number(current.get("orders")))
        with cols[2]:
            render_metric_card("投放消耗", format_number(current.get("ad_spend")))
        cols = st.columns(3)
        with cols[0]:
            render_metric_card("ROI", format_roi(current.get("roi")), roi_delta, roi_state)
        with cols[1]:
            render_metric_card("净 ROI", format_roi(current.get("net_roi")))
        with cols[2]:
            render_metric_card("退款率", format_percent(current.get("refund_rate")))


def render_boss_home(op_df: pd.DataFrame):
    st.subheader("BOSS首页")
    if op_df.empty:
        render_info_box("暂无历史数据，请管理员在后台导入数据。")
        return

    selected_date, mode, previous_date = render_date_controls(op_df)
    if selected_date is None:
        render_info_box("暂无历史数据，请管理员在后台导入数据。")
        return

    current_df = filter_by_selected_date(op_df, selected_date, mode)
    if current_df.empty:
        st.warning("当前日期暂无数据，请选择其他日期。")
        return
    previous_df = filter_by_selected_date(op_df, previous_date, "本日数据") if previous_date is not None and mode == "本日数据" else pd.DataFrame()

    current_main = main_scope(current_df)
    previous_main = main_scope(previous_df)
    current = aggregate_metrics(current_main)
    previous = aggregate_metrics(previous_main)
    gmv_delta, gmv_state = ("本月累计暂无对比", "flat") if mode == "本月累计" else format_delta(current.get("gmv"), previous.get("gmv"))
    roi_delta, roi_state = ("本月累计暂无对比", "flat") if mode == "本月累计" else format_delta(current.get("roi"), previous.get("roi"))

    st.markdown('<div class="section-title">核心指标</div>', unsafe_allow_html=True)
    cols = st.columns(4)
    with cols[0]:
        render_metric_card("总 GMV", format_number(current.get("gmv")), help_text="不含千川，避免重复计算")
    with cols[1]:
        render_metric_card("总单量", format_number(current.get("orders")))
    with cols[2]:
        render_metric_card("总投放消耗", format_number(current.get("ad_spend")))
    with cols[3]:
        render_metric_card("整体 ROI", format_roi(current.get("roi")))
    cols = st.columns(4)
    with cols[0]:
        render_metric_card("净 ROI", format_roi(current.get("net_roi")))
    with cols[1]:
        render_metric_card("退款率", format_percent(current.get("refund_rate")))
    with cols[2]:
        render_metric_card("较上一有数据日 GMV 变化", gmv_delta, delta_state=gmv_state)
    with cols[3]:
        render_metric_card("较上一有数据日 ROI 变化", roi_delta, delta_state=roi_state)

    st.markdown('<div class="section-title">四个经营卡片</div>', unsafe_allow_html=True)
    rows = [[("最护", "抖店"), ("最护", "拼多多")], [("碧维", "抖店"), ("碧维", "拼多多")]]
    for row in rows:
        cols = st.columns(2)
        for col, (brand, platform) in zip(cols, row):
            with col:
                render_business_card(brand, platform, current_main, previous_main, mode)

    st.markdown('<div class="section-title">当前日期核心提醒</div>', unsafe_allow_html=True)
    alert_df = generate_alerts(current_df if mode == "本日数据" else current_df[current_df["date"] == selected_date])
    if alert_df.empty:
        render_info_box("当前日期暂无明显异常")
    else:
        render_alert_cards(alert_df.head(3), compact=True)


def render_trends(op_df: pd.DataFrame):
    st.subheader("历史趋势")
    if op_df.empty:
        render_info_box("暂无历史数据，请管理员在后台导入数据。")
        return
    min_date = op_df["date"].min().date()
    max_date = op_df["date"].max().date()
    cols = st.columns(5)
    date_range = cols[0].date_input("日期范围", value=(min_date, max_date), min_value=min_date, max_value=max_date)
    brand = cols[1].selectbox("品牌", ["全部", "最护", "碧维"])
    platform = cols[2].selectbox("平台", ["全部", "抖店", "拼多多", "千川"])
    channel = cols[3].selectbox("渠道", ["全部", "整体", "商品卡", "直播", "短视频", "千川"])
    period = cols[4].selectbox("趋势粒度", ["每日", "每周", "每月"])

    filtered = filter_df(op_df, brand, platform, channel, date_range)
    if filtered.empty:
        render_info_box("当前筛选条件下暂无数据")
        return
    trend_df = aggregate_for_period(filtered, period)
    render_chart(trend_df, "gmv", f"GMV {period}趋势")
    render_chart(trend_df, "orders", f"单量 {period}趋势")
    render_chart(trend_df, "ad_spend", f"投放消耗 {period}趋势")
    render_chart(trend_df, "roi", f"ROI {period}趋势")
    render_chart(trend_df, "refund_rate", f"退款率 {period}趋势", formatter="percent")


def render_channel_analysis(op_df: pd.DataFrame):
    st.subheader("渠道分析")
    if op_df.empty:
        render_info_box("暂无历史数据，请管理员在后台导入数据。")
        return
    min_date = op_df["date"].min().date()
    max_date = op_df["date"].max().date()
    date_range = st.date_input("日期范围", value=(min_date, max_date), min_value=min_date, max_value=max_date, key="channel_date_range")
    filtered = filter_df(op_df, "全部", "全部", "全部", date_range)
    if filtered.empty:
        render_info_box("当前筛选条件下暂无数据")
        return

    douyin_channels = ["整体", "商品卡", "直播", "短视频", "店铺号商品卡", "洗脸巾直播"]
    douyin_df = filtered[(filtered["platform"] == "抖店") & (filtered["channel"].isin(douyin_channels))].copy()
    if douyin_df.empty:
        render_info_box("暂无抖店渠道数据")
    else:
        channel_df = aggregate_for_period(douyin_df, "每日")
        channel_summary = []
        for keys, group in channel_df.groupby(["brand", "channel"], dropna=False):
            row = aggregate_metrics(group)
            row.update({"brand": keys[0], "channel": keys[1]})
            channel_summary.append(row)
        summary_df = pd.DataFrame(channel_summary)
        render_bar(summary_df, "gmv", "各渠道 GMV")
        render_bar(summary_df, "orders", "各渠道单量")
        render_bar(summary_df, "ad_spend", "各渠道投放消耗")
        render_bar(summary_df, "roi", "各渠道 ROI")
        render_bar(summary_df, "refund_rate", "各渠道退款率", formatter="percent")

    st.markdown('<div class="section-title">千川投放补充分析</div>', unsafe_allow_html=True)
    st.caption("千川只作为投放补充分析，不与抖店 GMV 合并计算。")
    qianchuan_df = filtered[filtered["platform"] == "千川"].copy()
    if qianchuan_df.empty:
        render_info_box("暂无千川数据")
    else:
        render_chart(qianchuan_df, "gmv", "千川成交趋势")
        render_chart(qianchuan_df, "net_gmv", "千川净成交趋势")
        render_chart(qianchuan_df, "ad_spend", "千川消耗趋势")
        render_chart(qianchuan_df, "roi", "千川 ROI 趋势")
        render_chart(qianchuan_df, "net_roi", "千川净 ROI 趋势")


def severity_class(severity: str) -> tuple[str, str]:
    if severity == "高":
        return "alert-high", "tag-high"
    if severity == "中":
        return "alert-mid", "tag-mid"
    return "alert-low", "tag-low"


def render_alert_cards(alerts: pd.DataFrame, compact: bool = False):
    if alerts.empty:
        render_info_box("当前筛选范围内暂无明显异常。")
        return
    for _, row in alerts.iterrows():
        box_class, tag_class = severity_class(str(row.get("严重程度", "低")))
        details = "" if compact else f"<div><b>可能原因：</b>{row.get('可能原因', '')}</div><div><b>建议动作：</b>{row.get('建议动作', '')}</div>"
        st.markdown(
            f"""
            <div class="alert-card {box_class}">
                <div><b>{row.get('标题', '')}</b><span class="tag {tag_class}">{row.get('严重程度', '')}</span></div>
                <div style="margin-top:8px;"><b>数据依据：</b>{row.get('数据依据', '')}</div>
                {details}
                <div style="margin-top:8px;color:#6b7280;font-size:13px;">
                    {row.get('brand', '')} / {row.get('platform', '')} / {row.get('channel', '')} / {row.get('date', '')}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_alerts(op_df: pd.DataFrame):
    st.subheader("自动复盘提醒")
    if op_df.empty:
        render_info_box("暂无历史数据，请管理员在后台导入数据。")
        return
    min_date = op_df["date"].min().date()
    max_date = op_df["date"].max().date()
    date_range = st.date_input("日期范围", value=(min_date, max_date), min_value=min_date, max_value=max_date, key="alert_date_range")
    filtered = filter_df(op_df, "全部", "全部", "全部", date_range)
    alerts = generate_alerts(filtered)
    if alerts.empty:
        render_info_box("当前筛选范围内暂无明显异常。")
        return
    severity_order = {"高": 0, "中": 1, "低": 2}
    alerts["排序"] = alerts["严重程度"].map(severity_order).fillna(9)
    alerts = alerts.sort_values(["排序", "date"], ascending=[True, False]).drop(columns=["排序"])
    render_alert_cards(alerts)


def main():
    st.set_page_config(page_title="电商经营复盘看板", layout="wide")
    inject_custom_css()
    st.markdown('<div class="main-title">电商经营复盘看板</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="subtle-note">最护和碧维是不同品类品牌，本看板展示各自经营状态，不做品牌输赢对比。<br>千川数据只作为投放补充分析，不默认计入总 GMV，避免和抖店重复。</div>',
        unsafe_allow_html=True,
    )

    if not require_password():
        return

    if st.button("刷新数据"):
        st.rerun()

    display_df, _history_message = load_history_from_database()

    tab_home, tab_trend, tab_channel, tab_alert = st.tabs(["BOSS首页", "历史趋势", "渠道分析", "自动复盘提醒"])

    if display_df.empty:
        with tab_home:
            render_info_box("暂无历史数据，请管理员在后台导入数据。")
        with tab_trend:
            render_info_box("暂无历史数据，请管理员在后台导入数据。")
        with tab_channel:
            render_info_box("暂无历史数据，请管理员在后台导入数据。")
        with tab_alert:
            render_info_box("暂无历史数据，请管理员在后台导入数据。")
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
