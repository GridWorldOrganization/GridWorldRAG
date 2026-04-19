#!/usr/bin/env python3
"""
calc_workers.py - 最適な並列ワーカー数を計算し config.env に設定する

PC のスペック（CPU コア数・空きメモリ）と共有ドライブ数から
最適なワーカー数を算出する。

使い方:
    python calc_workers.py          # 計算して config.env に書き込み
    python calc_workers.py --dry-run  # 計算結果の表示のみ
"""

import argparse
import os
import re
import subprocess
import sys


def get_cpu_cores():
    """利用可能な CPU コア数を取得する。"""
    result = subprocess.run(["sysctl", "-n", "hw.ncpu"], capture_output=True, text=True)
    return int(result.stdout.strip())


def get_available_memory_gb():
    """利用可能なメモリ (GB) を取得する。"""
    # 全メモリ
    result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
    total_bytes = int(result.stdout.strip())
    total_gb = total_bytes / (1024 ** 3)

    # vm_stat で空きページを取得
    result = subprocess.run(["vm_stat"], capture_output=True, text=True)
    lines = result.stdout.strip().split("\n")
    page_size = 16384  # Apple Silicon default
    free_pages = 0
    inactive_pages = 0
    for line in lines:
        if "page size of" in line:
            page_size = int(re.search(r"(\d+)", line).group(1))
        elif "Pages free" in line:
            free_pages = int(re.search(r"(\d+)", line).group(1))
        elif "Pages inactive" in line:
            inactive_pages = int(re.search(r"(\d+)", line).group(1))

    available_gb = (free_pages + inactive_pages) * page_size / (1024 ** 3)
    return total_gb, available_gb


def count_shared_drives():
    """shared_drives_whitelist.txt のドライブ数をカウントする。"""
    from pathlib import Path
    whitelist_path = Path(__file__).parent / "shared_drives_whitelist.txt"
    if not whitelist_path.exists():
        return 0
    count = 0
    with open(whitelist_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                count += 1
    return count


def calc_optimal_workers(cpu_cores, available_gb, n_drives):
    """最適なワーカー数を算出する。"""
    MODEL_SIZE_GB = 0.5  # モデル + ワーカーオーバーヘッド
    SYSTEM_RESERVE_CORES = 2
    SYSTEM_RESERVE_GB = 4
    API_RATE_LIMIT = 10  # Google Drive API の実用上限

    max_by_cpu = max(1, cpu_cores - SYSTEM_RESERVE_CORES)
    max_by_ram = max(1, int((available_gb - SYSTEM_RESERVE_GB) / MODEL_SIZE_GB))
    max_by_api = API_RATE_LIMIT
    max_by_drives = max(1, n_drives)

    optimal = min(max_by_cpu, max_by_ram, max_by_api, max_by_drives)

    return optimal, {
        "cpu_cores": cpu_cores,
        "max_by_cpu": max_by_cpu,
        "available_gb": available_gb,
        "max_by_ram": max_by_ram,
        "max_by_api": max_by_api,
        "n_drives": n_drives,
        "max_by_drives": max_by_drives,
    }


def update_config_env(workers):
    """config.env の PARALLEL_WORKERS を更新する。"""
    from pathlib import Path
    config_path = Path(__file__).parent / "config.env"

    if not config_path.exists():
        print(f"エラー: {config_path} が見つかりません")
        sys.exit(1)

    content = config_path.read_text()

    if "PARALLEL_WORKERS=" in content:
        content = re.sub(r"PARALLEL_WORKERS=\d+", f"PARALLEL_WORKERS={workers}", content)
    else:
        content = content.rstrip("\n") + f"\n\n# 並列ワーカー数（calc_workers.py で自動設定）\nPARALLEL_WORKERS={workers}\n"

    config_path.write_text(content)


def main():
    parser = argparse.ArgumentParser(description="最適な並列ワーカー数を計算")
    parser.add_argument("--dry-run", action="store_true", help="計算結果の表示のみ")
    args = parser.parse_args()

    cpu_cores = get_cpu_cores()
    total_gb, available_gb = get_available_memory_gb()
    n_drives = count_shared_drives()

    optimal, details = calc_optimal_workers(cpu_cores, available_gb, n_drives)

    print("=" * 50)
    print("GridWorldRAG - ワーカー数計算")
    print("=" * 50)
    print(f"\n  PC スペック:")
    print(f"    CPU コア数:     {details['cpu_cores']}")
    print(f"    全メモリ:       {total_gb:.0f} GB")
    print(f"    空きメモリ:     {details['available_gb']:.1f} GB")
    print(f"    共有ドライブ数: {details['n_drives']}")
    print(f"\n  制約:")
    print(f"    CPU 上限:       {details['max_by_cpu']} (コア数 - 2)")
    print(f"    RAM 上限:       {details['max_by_ram']} (空き / 0.5GB)")
    print(f"    API 上限:       {details['max_by_api']}")
    print(f"    ドライブ数:     {details['max_by_drives']}")
    print(f"\n  → 最適ワーカー数: {optimal}")

    if args.dry_run:
        print("\n[ドライラン] config.env への書き込みはスキップしました")
        return

    update_config_env(optimal)
    print(f"\n  config.env に PARALLEL_WORKERS={optimal} を設定しました")
    print("=" * 50)


if __name__ == "__main__":
    main()
