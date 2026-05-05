"Aurex AI Signature Strategy — EMA + Sweep + FVG + Fibonacci confluence engine."
__version__ = "1.0.0"

# Enforce namespace isolation at import time.
# Raises ImportError immediately if any legacy HFM_AUTO_TRADER module is loaded.
from aurex_ai.core.isolation_guard import assert_clean_namespace as _guard
_guard()
del _guard
