"""Print the independently known tag list as a JSON array."""

from __future__ import annotations

import json

from fixture import expected_tags

if __name__ == "__main__":
    print(json.dumps(expected_tags()))
