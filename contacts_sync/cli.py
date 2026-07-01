import typer

app = typer.Typer()


@app.callback()
def main():
    """contacts-sync: sync contacts between Google, iCloud, and Microsoft."""


@app.command()
def version():
    typer.echo("contacts-sync 0.1.0")


if __name__ == "__main__":
    app()
