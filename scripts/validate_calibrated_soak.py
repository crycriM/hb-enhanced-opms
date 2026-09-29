"""Offline immutable-policy gate before a prepared single-account soak."""
import argparse
from pathlib import Path
from opms.soak_calibration import validate_prepared

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("path", type=Path)
parser.add_argument("--account", required=True)
parser.add_argument("--coins", nargs="+", required=True)
parser.add_argument("--allow-shadow", action="store_true")
args = parser.parse_args()
validate_prepared(args.path, account=args.account, coins=args.coins, require_live=not args.allow_shadow)
print("calibrated single-account policy verified (no network, no orders)")
