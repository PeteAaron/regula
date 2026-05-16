"""Typer CLI entry point. Thin wrapper over the library API in :mod:`regula.pipeline`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.pretty import Pretty

app = typer.Typer(
    name="regula",
    help="Deterministic PDF → structured-chunks ingestion pipeline.",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True, style="bold red")


@app.command()
def ingest(
    config: str = typer.Option(..., "--config", help="Path to a per-document YAML config."),
    no_fail: bool = typer.Option(False, "--no-fail", help="Exit 0 even if validation fails."),
) -> None:
    """Run the full pipeline end-to-end for one document."""
    from regula.config import load_config
    from regula.pipeline import Pipeline

    cfg = load_config(config)
    pipeline = Pipeline(cfg)
    report = pipeline.run()
    counts = {m.name: m.value for m in report.metrics}
    typer.echo(
        f"✓ {cfg.doc_id}: validation {'passed' if report.passed else 'FAILED'} "
        f"({len(report.metrics)} metrics) → {pipeline.output_dir}"
    )
    if not report.passed:
        for m in report.metrics:
            if not m.passed:
                typer.echo(f"  ✗ {m.name}: value={m.value} threshold={m.threshold}")
    if not report.passed and not no_fail:
        raise typer.Exit(code=1)


@app.command()
def stage(
    name: str = typer.Argument(..., help="Stage name to run."),
    config: str = typer.Option(..., "--config", help="Path to a per-document YAML config."),
) -> None:
    """Run a single stage, reading from prior stages' intermediate output."""
    from regula.config import load_config
    from regula.pipeline import Pipeline, PipelineError

    cfg = load_config(config)
    pipeline = Pipeline(cfg)
    try:
        report = pipeline.run_stage(name)
    except PipelineError as e:
        err_console.print(f"error: {e}")
        raise typer.Exit(code=1) from e
    typer.echo(
        f"✓ stage {name}: ok={report.ok} duration={report.duration_seconds:.3f}s "
        f"counts={dict(report.counts)}"
    )
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def validate(
    config: str = typer.Option(..., "--config", help="Path to a per-document YAML config."),
    no_fail: bool = typer.Option(False, "--no-fail", help="Exit 0 even if validation fails."),
) -> None:
    """Re-run the validate stage against an existing run."""
    from regula.config import load_config
    from regula.pipeline import Pipeline, PipelineError

    cfg = load_config(config)
    pipeline = Pipeline(cfg)
    try:
        report = pipeline.revalidate()
    except PipelineError as e:
        err_console.print(f"error: {e}")
        raise typer.Exit(code=1) from e
    typer.echo(
        f"✓ revalidated: passed={report.passed} metrics={len(report.metrics)}"
    )
    if not report.passed and not no_fail:
        raise typer.Exit(code=1)


@app.command()
def preview(
    config: str = typer.Option(..., "--config"),
    chunk_id: str = typer.Option(..., "--chunk-id"),
) -> None:
    """Pretty-print a single chunk from an existing run."""
    from regula.config import load_config

    cfg = load_config(config)
    chunks_path = Path("output") / cfg.doc_id / "chunks.jsonl"
    if not chunks_path.exists():
        err_console.print(f"error: {chunks_path} not found — run `regula ingest` first")
        raise typer.Exit(code=1)
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if data.get("chunk_id") == chunk_id:
            console.print(Pretty(data, expand_all=True))
            return
    err_console.print(f"chunk not found: {chunk_id}")
    raise typer.Exit(code=1)


@app.command(name="inspect")
def inspect_cmd(
    config: str = typer.Option(..., "--config", help="Path to a per-document YAML config."),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Where to write the HTML. Defaults to output/<doc_id>/preview.html.",
    ),
) -> None:
    """Render a diagnostic HTML preview of an existing run.

    Walks chunks in order_index sequence, lays them out as a recomposed
    document, and surfaces every extracted property (chunk_id, section
    path, references with resolution status, defined terms, source spans)
    inline. Internal references render as clickable anchor links so
    reading-order and target-resolution can be spot-checked by clicking
    through. Defined terms are highlighted with a tooltip showing the
    glossary definition. Every chunk has a ``show JSON`` toggle.
    """
    from regula.config import load_config
    from regula.inspect import write_preview

    cfg = load_config(config)
    output_dir = Path("output") / cfg.doc_id
    required = [
        "chunks.jsonl",
        "toc.json",
        "references_index.json",
        "glossary.json",
        "document.json",
        "deferred.json",
    ]
    missing = [n for n in required if not (output_dir / n).exists()]
    if missing:
        err_console.print(
            f"error: {output_dir} is missing {missing} — run `regula ingest` first"
        )
        raise typer.Exit(code=1)
    target = write_preview(output_dir, out)
    typer.echo(f"✓ wrote {target}")


@app.command()
def diff(
    a: str = typer.Argument(..., help="First output directory."),
    b: str = typer.Argument(..., help="Second output directory."),
) -> None:
    """Diff two pipeline runs (proves reproducibility)."""
    from regula._diff import compare_outputs

    diffs = compare_outputs(Path(a), Path(b))
    if not diffs:
        typer.echo("identical")
        return
    for d in diffs:
        typer.echo(f"  {d.path}: {d.message}")
    typer.echo(f"{len(diffs)} difference(s)")
    raise typer.Exit(code=1)


@app.command(name="export-schemas")
def export_schemas_cmd(
    out: Path = typer.Option(Path("schemas"), "--out", help="Output directory."),
) -> None:
    """Export Pydantic models to committed JSON Schema files."""
    from regula._schema_export import export_schemas

    written = export_schemas(out)
    typer.echo(f"Wrote {len(written)} schemas to {out}/")
    for path in written:
        typer.echo(f"  {path.name}")


if __name__ == "__main__":
    app()
