#!/usr/bin/env python3
"""每日每线路时效视图与健康检查。

输入:
  promise.csv  route,promise_hours
  tracks.csv   waybill,route,first_scan,delivered

输出:
  sla_view.csv  day,route,cnt,avg_diff,hit_rate
  health.txt    五项检查

规则:
  - 运单按首扫自然日归入当日线路分区
  - 无签收运单: 不计入 cnt/hit_rate 分母, 计 no_delivery
  - 首扫晚于签收(倒挂): 剔除级处置, 不计入, 计 excluded
  - 时效差 = 签收时刻 - 首扫时刻, 按分钟整数
  - 达标: 时效差 <= 承诺小时折算分钟(含等于)
  - 均值与占比按半数取偶(银行家舍入)
  - 视图覆盖全部 天x线路 组合, 无计入单的分区 cnt/avg_diff=0, hit_rate=0.00
  - 重刷幂等覆盖
"""
import argparse
import csv
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN

TS_FMT = "%Y-%m-%d %H:%M"
VIEW_HEADER = ["day", "route", "cnt", "avg_diff", "hit_rate"]
# 十四天折算分钟, 均值上界
MAX_AVG_BOUND = 14 * 24 * 60  # 20160


def parse_ts(value):
    value = value.strip()
    if not value:
        return None
    return datetime.strptime(value, TS_FMT)


def banker_round(value, places):
    """按半数取偶舍入, places 为小数位数; 返回 Decimal。"""
    quant = Decimal(1).scaleb(-places)
    return Decimal(value).quantize(quant, rounding=ROUND_HALF_EVEN)


def load_promise(path):
    promise = {}
    with open(path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            promise[row["route"].strip()] = int(row["promise_hours"])
    return promise


def main():
    ap = argparse.ArgumentParser(description="每日每线路 SLA 时效视图")
    ap.add_argument("--promise", required=True, help="promise.csv 路径")
    ap.add_argument("--tracks", required=True, help="tracks.csv 路径")
    ap.add_argument("--out-dir", required=True, help="输出目录")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    view_path = os.path.join(args.out_dir, "sla_view.csv")
    health_path = os.path.join(args.out_dir, "health.txt")

    promise = load_promise(args.promise)
    routes = sorted(promise.keys())

    # 分区聚合: (day_str, route) -> 统计
    diff_sum = defaultdict(int)       # 时效差合计(分钟)
    hit_cnt = defaultdict(int)        # 达标笔数
    cnt = defaultdict(int)            # 计入笔数
    days = set()

    total_rows = 0
    no_delivery_total = 0
    excluded_total = 0
    included_total = 0
    cross_day_total = 0               # 计入单中签收自然日 != 首扫自然日
    max_event_ts = None               # 全部轨迹行最大事件时刻

    with open(args.tracks, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total_rows += 1
            route = row["route"].strip()
            first_scan = parse_ts(row["first_scan"])
            delivered = parse_ts(row["delivered"])

            for ts in (first_scan, delivered):
                if ts is not None and (max_event_ts is None or ts > max_event_ts):
                    max_event_ts = ts

            day = first_scan.strftime("%Y-%m-%d")
            days.add(day)
            key = (day, route)

            if delivered is None:
                # 无签收: 不进 cnt 也不进 hit_rate 分母
                no_delivery_total += 1
                continue

            if first_scan > delivered:
                # 扫描晚于签收: 剔除级处置并计数
                excluded_total += 1
                continue

            # 计入单
            included_total += 1
            diff_minutes = int((delivered - first_scan).total_seconds() // 60)
            cnt[key] += 1
            diff_sum[key] += diff_minutes
            if diff_minutes <= promise[route] * 60:
                hit_cnt[key] += 1
            if delivered.date() != first_scan.date():
                cross_day_total += 1

    sorted_days = sorted(days)
    expected_rows = len(sorted_days) * len(routes)

    # 生成视图(幂等覆盖)
    avg_values = []
    actual_rows = 0
    with open(view_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(VIEW_HEADER)
        for day in sorted_days:
            for route in routes:
                key = (day, route)
                n = cnt.get(key, 0)
                if n == 0:
                    avg_diff = 0
                    hit_rate = Decimal("0.00")
                else:
                    avg_dec = banker_round(Decimal(diff_sum[key]) / Decimal(n), 0)
                    avg_diff = int(avg_dec)
                    rate = banker_round(
                        Decimal(hit_cnt[key]) * Decimal(100) / Decimal(n), 2
                    )
                    hit_rate = rate
                writer.writerow([day, route, n, avg_diff, f"{hit_rate:.2f}"])
                avg_values.append(avg_diff)
                actual_rows += 1

    # 健康检查: 每项以检查名开头给出数字结论
    checks = []

    # 1. 分区齐全: 应有行数 vs 实出行数
    checks.append(
        f"分区齐全: 应有行数 {expected_rows} 实出行数 {actual_rows}"
    )

    # 2. 计数对账: 计入合计 + 无签收合计 + 剔除合计, 以及轨迹总笔数
    checks.append(
        f"计数对账: 计入合计 {included_total} 无签收合计 {no_delivery_total} "
        f"剔除合计 {excluded_total} 轨迹总笔数 {total_rows}"
    )

    # 3. 极值界: 全部行均值最小/最大, 均值上界为十四天折算 20160 分钟
    min_avg = min(avg_values)
    max_avg = max(avg_values)
    checks.append(
        f"极值界: 均值最小值 {min_avg} 均值最大值 {max_avg} 均值上界 {MAX_AVG_BOUND}"
    )

    # 4. 水印行: 全部轨迹行最大事件时刻(首扫与签收都算, 无签收/倒挂也算)
    watermark = max_event_ts.strftime(TS_FMT) if max_event_ts else ""
    checks.append(f"水印行: 最大事件时刻 {watermark}")

    # 5. 迟到补偿账: 计入单中签收自然日 != 首扫自然日的笔数
    checks.append(f"迟到补偿账: 跨自然日计入单 {cross_day_total} 笔")

    # 缺任何一项视图判不合格并写明缺项
    required = ["分区齐全", "计数对账", "极值界", "水印行", "迟到补偿账"]
    present = {line.split(":", 1)[0] for line in checks}
    missing = [name for name in required if name not in present]
    if missing:
        checks.append("缺项: " + " ".join(missing) + " 不合格")

    with open(health_path, "w", encoding="utf-8") as f:
        f.write("\n".join(checks) + "\n")

    print(f"视图已写出: {view_path} ({actual_rows} 行)")
    print(f"健康报告: {health_path}")


if __name__ == "__main__":
    main()
