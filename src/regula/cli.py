"""Typer CLI entry point. Thin wrapper over :mod:`regula.pipeline`.

Post-wind-back the CLI is intentionally narrow: ingest (run the
pipeline), inspect (render preview), diff (reproducibility check),
plus the per-stage runner and schema exporter. Chunking-era commands
(``preview --chunk-id``, ``validate``) have been removed.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(
    name="regula",
    help="Deterministic PDF → text-block extraction pipeline.",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True, style="bold red")


@app.command()
def ingest(
    pdf: Path | None = typer.Argument(
        None,
        help="PDF to process. With no --config, defaults are inferred from the filename.",
    ),
    config: str | None = typer.Option(
        None, "--config", help="Path to a per-document YAML config."
    ),
    out_dir: Path | None = typer.Option(
        None,
        "--out-dir",
        help=(
            "Where to write artifacts. Defaults to ./<doc_id>/ for "
            "inferred runs and output/<doc_id>/ for --config runs."
        ),
    ),
    no_fail: bool = typer.Option(
        False, "--no-fail", help="Exit 0 even if validation fails."
    ),
    no_preview: bool = typer.Option(
        False, "--no-preview", help="Skip writing preview.html at the end."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Clobber a non-empty output directory even if it doesn't look like a previous regula run.",
    ),
) -> None:
    """Extract blocks from a PDF.

    \b
      regula ingest my-doc.pdf            # zero-config; lenient defaults
      regula ingest --config foo.yaml     # full control via YAML
    """
    from regula.config import infer_config, load_config
    from regula.pipeline import Pipeline, PipelineError

    if (config is None) == (pdf is None):
        err_console.print("error: provide exactly one of <pdf> or --config")
        raise typer.Exit(code=1)

    if config is not None:
        cfg = load_config(config)
        default_root = None  # Pipeline default: output/<doc_id>/
    else:
        assert pdf is not None
        if not pdf.exists():
            err_console.print(f"error: file not found: {pdf}")
            raise typer.Exit(code=1)
        cfg = infer_config(pdf)
        default_root = Path.cwd() / cfg.doc_id

    output_root = out_dir.resolve() if out_dir else default_root
    pipeline = Pipeline(cfg, output_root=output_root)
    try:
        report = pipeline.run(force=force)
    except PipelineError as e:
        err_console.print(f"error: {e}")
        raise typer.Exit(code=1) from e

    typer.echo(
        f"✓ {cfg.doc_id}: validation {'passed' if report.passed else 'FAILED'} "
        f"({len(report.metrics)} metrics) → {pipeline.output_dir}"
    )
    if not report.passed:
        for m in report.metrics:
            if not m.passed:
                typer.echo(f"  ✗ {m.name}: value={m.value} threshold={m.threshold}")

    if not no_preview:
        from regula.inspect import write_preview

        preview_path = write_preview(pipeline.output_dir)
        typer.echo(f"  preview: {preview_path}")

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


@app.command(name="inspect")
def inspect_cmd(
    run_dir: Path | None = typer.Argument(
        None,
        help=(
            "Output directory of a previous run. Defaults to ./<doc_id>/ "
            "based on --config, or the current directory."
        ),
    ),
    config: str | None = typer.Option(
        None, "--config", help="Path to a per-document YAML config."
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Where to write the HTML. Defaults to <run-dir>/preview.html.",
    ),
) -> None:
    """Render the page-oriented HTML preview of an existing run."""
    from regula.config import load_config
    from regula.inspect import write_preview

    if run_dir is not None:
        output_dir = run_dir.resolve()
    elif config is not None:
        cfg = load_config(config)
        output_dir = (Path("output") / cfg.doc_id).resolve()
    else:
        output_dir = Path.cwd()

    required = ["blocks.jsonl", "pages.json", "document.json"]
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
