---
name: data-analysis
description: Exploratory data analysis on tabular files (CSV, Excel, Parquet). Use when the task involves loading a dataset, summarizing or profiling it, computing aggregations, or producing charts. Triggers on "analyze this CSV", "summarize the data", "what's the distribution of", "plot", "correlation", "pivot".
---

# Data analysis

A repeatable workflow for tabular data tasks. The agent's Python environment
already has `pandas`, `numpy`, `matplotlib`, and `openpyxl` installed.

## Workflow

1. **Load & inspect.** Read the file with the right reader (`pd.read_csv`,
   `pd.read_excel`, `pd.read_parquet`). Immediately print `df.shape`,
   `df.dtypes`, `df.head()`, and `df.isna().sum()` so you understand the data
   before touching it.
2. **Validate assumptions.** Coerce obviously-wrong dtypes (dates parsed as
   strings, numerics as objects). Note missing values and outliers explicitly
   rather than silently dropping them.
3. **Answer the question.** Use vectorized pandas (`groupby`, `pivot_table`,
   `merge`) over Python loops. State the aggregation you computed in words.
4. **Visualize when it clarifies.** Save figures to the working directory with
   `plt.savefig("<name>.png", dpi=120, bbox_inches="tight")` and reference the
   filename in your answer. Do not call `plt.show()` (headless).
5. **Report.** End with a short, plain-language summary of findings and the
   paths of any files you produced.

## Conventions

- Never fabricate numbers — every figure in the answer must come from code you
  actually ran.
- Prefer reproducibility: paste the key pandas snippet you used so the result
  can be re-derived.
