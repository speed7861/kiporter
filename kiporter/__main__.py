import sys

if len(sys.argv) > 1:  # any argument: command line (python -m kiporter search LM358 --json)
    from .cli import main as cli_main
    sys.exit(cli_main())

from .gui import main

main()
