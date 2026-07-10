"""Allow `python -m dictumc` to invoke the CLI (v5)."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dictumc_cli import main
sys.exit(main())
