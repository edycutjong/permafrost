"""permafrost-edge — the vaccine fridge that argues for its own contents.

Toolkit surface (COMPLEXITY §4): ``EdgeDaemon``, ``RuleBundle`` (with
``.verify()``), ``ChainLogger``, ``DiagnoserClient``, ``run_replay`` and
``verify_chain``.
"""

from .chain import ChainLogger, ChainReport, verify_chain
from .daemon import DaemonConfig, EdgeDaemon
from .link import DiagnoserClient
from .replay import ReplayResult, run_replay
from .rules import ReflexEngine, RuleBundle, RuleBundleInvalid, RuleBundleRejected
from .storage import EdgeStore, Reading
from .verdict import ExcursionVerdict

__version__ = "1.0.0"

__all__ = [
    "ChainLogger",
    "ChainReport",
    "DaemonConfig",
    "DiagnoserClient",
    "EdgeDaemon",
    "EdgeStore",
    "ExcursionVerdict",
    "Reading",
    "ReflexEngine",
    "ReplayResult",
    "RuleBundle",
    "RuleBundleInvalid",
    "RuleBundleRejected",
    "run_replay",
    "verify_chain",
    "__version__",
]
