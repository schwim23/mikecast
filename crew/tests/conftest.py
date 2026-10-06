"""Suite-wide test settings."""

import os

# Keep the suite off the network: mc_facts would otherwise query Wikidata the
# first time any writer agent or prompt is built. test_facts.py exercises the
# live-lookup path with a mocked response instead.
os.environ.setdefault("MIKECAST_FACTS_OFFLINE", "1")
