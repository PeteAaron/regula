"""Typer CLI entry point. Thin wrapper over the library API in :mod:`regula.pipeline`."""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(
    name="regula",
    help="Deterministic PDF → structured-chunks ingestion pipeline.",
    no_args_is_help=True,
)


@app.command()
def ingest(config: str = typer.Option(..., "--config", help="Path to a per-document YAML config.")) -> None:
    """Run the full pipeline end-to-end for one document."""
    raise NotImplementedError("Pipeline ingest not yet implemented (Phase 2).")


@app.command()
def stage(
    name: str = typer.Argument(..., help="Stage name to run."),
    config: str = typer.Option(..., "--config", help="Path to a per-document YAML config."),
) -> None:
    """Run a single stage, reading from the prior stage's intermediate output."""
    raise NotImplementedError("Single-stage execution not yet implemented (Phase 2).")


@app.command()
def validate(
    config: str = typer.Option(..., "--config", help="Path to a per-document YAML config."),
) -> None:
    """Re-run the validate stage against an existing run."""
    raise NotImplementedError("Validate not yet implemented (Phase 2).")


@app.command()
def preview(
    config: str = typer.Option(..., "--config"),
    chunk_id: str = typer.Option(..., "--chunk-id"),
) -> None:
    """Pretty-print a single chunk from an existing run."""
    raise NotImplementedError("Preview not yet implemented (Phase 2).")


@app.command()
def diff(
    a: str = typer.Argument(..., help="First output directory."),
    b: str = typer.Argument(..., help="Second output directory."),
) -> None:
    """Diff two pipeline runs (proves reproducibility)."""
    raise NotImplementedError("Diff not yet implemented (Phase 2).")


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
