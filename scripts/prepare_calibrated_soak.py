"""Materialize ONE pair on ONE account. No credentials, network, or orders.

Default is shadow. --live requires a passing v2 report and creates configs
only; the runner still requires both existing mainnet/order confirmations.
"""
import argparse
import hashlib
import json
import shlex
from pathlib import Path

import yaml

from opms.soak_calibration import controller_from_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("coin", choices=("SOL", "ENA", "VVV"))
    parser.add_argument("--account", default="e2_mm1", choices=("e2_mm1", "e3_sub1"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if report.get("schema_version") != 2:
        parser.error("only corrected v2 calibration reports are accepted")
    with Path(report["capture_path"]).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != report["capture_sha256"]:
            parser.error("capture hash changed since calibration")
    result = next(r for r in report["results"] if r["coin"] == args.coin)
    config = controller_from_result(result, account=args.account, live=args.live)
    root = Path(__file__).resolve().parents[1]
    config["decision_log_path"] = str(root / "logs" / "hb_soak" / f"{config['id']}.decisions.jsonl")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)  # never overwrite another prepared run
    (output / "controllers").mkdir()
    (output / "scripts").mkdir()
    controller_name = f"{config['id']}.yml"
    script_name = f"opms_perp_mm_{args.account}_{args.coin.lower()}_calibrated.yml"
    script = dict(script_file_name="opms_perp_mm.py", controllers_config=[controller_name])
    (output / "controllers" / controller_name).write_text(yaml.safe_dump(config, sort_keys=False))
    (output / "scripts" / script_name).write_text(yaml.safe_dump(script, sort_keys=False))
    (output / "calibration.json").write_text(json.dumps(report, indent=2) + "\n")
    manifest = dict(coin=args.coin, account=args.account, live=args.live,
                    controller=controller_name, script=script_name, config=config,
                    start_equity=result["start_equity"])
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    env = dict(ACCOUNT_ID=args.account, SOAK_COINS=args.coin, SOAK_CONFIG_DIR=str(output),
               SOAK_SCRIPT_CONFIG=script_name, SOAK_LEVERAGE="3", SOAK_RISK_GATES="markout-only",
               SOAK_FLAT_TARGETS="1", SOAK_UPDATE_INTERVAL=str(result["update_interval"]),
               MIN_COLLATERAL=str(result["start_equity"]), MAX_DRAWDOWN_PCT="1.0")
    (output / "run.env").write_text("\n".join(f"export {key}={shlex.quote(value)}" for key, value in env.items()) + "\n")
    print(f"Prepared {'LIVE-GATED' if args.live else 'SHADOW ONLY'} {args.coin}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
