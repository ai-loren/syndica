"""`python -m syndica` entry point."""

from syndica.cli.main import main


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == '__main__':
    entrypoint()
