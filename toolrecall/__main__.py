"""Allow `python -m toolrecall` to work — delegates to the CLI entrypoint."""
from toolrecall.cli import main

if __name__ == "__main__":
    main()