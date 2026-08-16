"""Test package.

Silences the telemetry console mirror for the whole suite. Every test runs
the real pipeline, so without this the results are buried under a few
hundred lines of the pipeline's own logging. The log FILES are still
written and asserted on - see TestObservability.
"""

import os

os.environ.setdefault("CI_TELEMETRY_QUIET", "1")
