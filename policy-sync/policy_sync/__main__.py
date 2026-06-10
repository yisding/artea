import sys

from .config import ConfigError
from .server import main

try:
    main()
except ConfigError as e:
    print(f"policy-sync: fatal: {e}", file=sys.stderr)
    sys.exit(1)
