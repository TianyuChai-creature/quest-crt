"""Start the service from a wheel or a source checkout."""

from pathlib import Path
from runpy import run_path


def main() -> None:
    server = Path(__file__).resolve().with_name("server.py")
    if not server.is_file():
        server = server.parent.parent / "server.py"
    run_path(str(server), run_name="__main__")


if __name__ == "__main__":
    main()
