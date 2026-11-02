"""Click command structure for Spacekeeper."""

import click


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="spacekeeper")
def main() -> None:
    """Capture and restore macOS fullscreen window arrangements through yabai."""


@main.command()
@click.argument("name", default="default")
@click.option("--force", is_flag=True, help="Replace an existing named snapshot.")
def capture(name: str, force: bool) -> None:
    """Save the current arrangement as NAME (default: default).

    The default snapshot is replaced automatically. Other existing names
    require --force.
    """
    raise click.ClickException("Capture is not implemented yet.")


@main.command("list")
def list_snapshots() -> None:
    """List saved snapshots."""
    raise click.ClickException("Listing snapshots is not implemented yet.")


@main.command()
@click.argument("name", default="default")
def restore(name: str) -> None:
    """Restore snapshot NAME (default: default).

    Extra fullscreen windows absent from the snapshot will exit fullscreen.
    """
    raise click.ClickException("Restore is not implemented yet.")
