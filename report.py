"""Turn results/results.json into results/REPORT.md plus two charts.

    python report.py
"""
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"

# Reference palette (light mode): one hue per series, fixed order, text in ink tokens.
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]


def ms(r):
    if r.get("timeout"):
        return "timeout (>60 s)"
    v = r["exec_ms"]
    return f"{v:,.1f}" if v < 100 else f"{v:,.0f}"


def mb(b):
    if b < 2**20:
        return f"{b / 2**10:,.0f} KB"
    return f"{b / 2**20:,.1f} MB" if b < 2**30 else f"{b / 2**30:,.2f} GB"


def blocks(r):
    if r.get("timeout"):
        return "-"
    return f"{r['shared_hit'] + r['shared_read']:,}"


def scans(r):
    return "<br>".join(r.get("scans", [])) or "-"


def style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=8, length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def scenario_chart(data, path):
    scen = data["scenarios"]
    queries = list(scen[0]["queries"])
    names = [s["name"] for s in scen]
    cols = 2
    rows = math.ceil(len(queries) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(11, 2.3 * rows), facecolor=SURFACE)
    for ax, q in zip(axes.flat, queries):
        style(ax)
        vals = [s["queries"][q] for s in scen]
        finite = [v["exec_ms"] for v in vals if not v.get("timeout")]
        cap = max(finite) * 1.15 if finite else 1
        y = range(len(names))[::-1]
        for yi, v in zip(y, vals):
            if v.get("timeout"):
                ax.barh(yi, cap, height=0.6, color=GRID)
                ax.text(cap * 0.02, yi, "timeout > 60 s", va="center", fontsize=7.5, color=INK2)
            else:
                ax.barh(yi, v["exec_ms"], height=0.6, color=SERIES[0])
                ax.text(v["exec_ms"] + cap * 0.01, yi, ms(v) + " ms", va="center", fontsize=7.5, color=INK)
        ax.set_yticks(list(y), names, fontsize=8, color=INK)
        ax.set_xlim(0, cap * 1.25)
        ax.set_title(q, loc="left", fontsize=9.5, color=INK, fontweight="bold")
        ax.set_xlabel("execution time (ms, median)", fontsize=7.5, color=INK2)
    for ax in list(axes.flat)[len(queries):]:
        ax.set_visible(False)
    fig.suptitle("Query time by index configuration", x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def selectivity_chart(exp, path):
    series = {}
    for v in exp["variants"]:
        cfg, window = [p.strip() for p in v["label"].split("|")]
        series.setdefault(cfg, []).append((window, v))
    fig, ax = plt.subplots(figsize=(8, 4.2), facecolor=SURFACE)
    style(ax)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    for color, (cfg, pts) in zip(SERIES, series.items()):
        xs = list(range(len(pts)))
        ys = [p[1]["exec_ms"] for p in pts]
        ax.plot(xs, ys, color=color, linewidth=2, marker="o", markersize=5, label=cfg)
    windows = [p[0] for p in next(iter(series.values()))]
    ax.set_xticks(range(len(windows)), windows)
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
    ax.set_ylabel("execution time (ms, log scale)", fontsize=8, color=INK2)
    ax.set_xlabel("time window scanned", fontsize=8, color=INK2)
    ax.set_xlim(-0.3, len(windows) - 0.3)
    ax.legend(frameon=False, fontsize=8, loc="upper left", labelcolor=INK)
    ax.set_title("Selectivity sweep: count + sum(size) over a growing window", loc="left",
                 fontsize=10.5, color=INK)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main():
    data = json.loads((RESULTS / "results.json").read_text())
    meta = data["meta"]
    out = ["# Benchmark results", ""]
    out.append(f"PostgreSQL {meta['server_version']}, median of {meta['runs']} warm runs "
               f"(after one warm-up run). Parameters: `{meta['params']}`.")
    out.append("")
    out.append("| table | rows | heap size |")
    out.append("|---|---:|---:|")
    for t, v in meta["tables"].items():
        out.append(f"| {t} | {v['rows']:,} | {mb(v['bytes'])} |")
    out.append("")

    if "scenarios" in data:
        scen = data["scenarios"]
        scenario_chart(data, RESULTS / "query_times.png")
        out += ["## Query x index configuration", "", "![query times](query_times.png)", ""]
        out += ["### Indexes built per configuration", "",
                "| configuration | index | build time | size |", "|---|---|---:|---:|"]
        for s in scen:
            if not s["indexes"]:
                out.append(f"| {s['name']} | (none) | | |")
            for i in s["indexes"]:
                out.append(f"| {s['name']} | `{i['name']}` | {i['build_s']:.1f} s | {mb(i['bytes'])} |")
        out.append("")
        queries = list(scen[0]["queries"])
        out += ["### Execution time (ms)", "",
                "| query | " + " | ".join(s["name"] for s in scen) + " |",
                "|---|" + "---:|" * len(scen)]
        for q in queries:
            cells = []
            base = scen[0]["queries"][q]
            for s in scen:
                r = s["queries"][q]
                cell = ms(r)
                if not r.get("timeout") and not base.get("timeout") and s is not scen[0]:
                    cell += f" ({base['exec_ms'] / r['exec_ms']:.1f}x)"
                cells.append(cell)
            out.append(f"| {q} | " + " | ".join(cells) + " |")
        out += ["", "Speed-up in parentheses is relative to `none`.", ""]
        out += ["### Plans and buffers", "",
                "Buffers = shared blocks hit + read (8 KB pages touched). Full plans: `results/plans/`.", ""]
        for q in queries:
            out += [f"#### {q}", "", "| configuration | ms | buffers | rows removed by filter | access path |",
                    "|---|---:|---:|---:|---|"]
            for s in scen:
                r = s["queries"][q]
                rem = "-" if r.get("timeout") else f"{r['rows_removed_by_filter']:,.0f}"
                out.append(f"| {s['name']} | {ms(r)} | {blocks(r)} | {rem} | {scans(r)} |")
            out.append("")

    if "experiments" in data:
        out += ["## Experiments: when indexes don't help", ""]
        for e in data["experiments"]:
            out += [f"### {e['title']}", "", "```sql", e["sql"], "```", ""]
            if e.get("notes"):
                for k, v in e["notes"].items():
                    out.append(f"*{k}*: `{v}`  ")
                out.append("")
            if e["id"] == "selectivity":
                selectivity_chart(e, RESULTS / "selectivity.png")
                out += ["![selectivity sweep](selectivity.png)", ""]
            out += ["| variant | ms | buffers | rows removed (filter / recheck) | lossy blocks | access path |",
                    "|---|---:|---:|---:|---:|---|"]
            for v in e["variants"]:
                rem = "-" if v.get("timeout") else (
                    f"{v['rows_removed_by_filter']:,.0f} / {v['rows_removed_by_recheck']:,.0f}")
                lossy = "-" if v.get("timeout") else f"{v['lossy_heap_blocks']:,}"
                out.append(f"| {v['label']} | {ms(v)} | {blocks(v)} | {rem} | {lossy} | {scans(v)} |")
            if e["indexes"]:
                out.append("")
                out.append("Indexes: " + ", ".join(f"`{i['name']}` {mb(i['bytes'])} ({i['build_s']:.1f} s)"
                                                   for i in e["indexes"]))
            out.append("")

    (RESULTS / "REPORT.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {RESULTS / 'REPORT.md'}")


if __name__ == "__main__":
    main()
