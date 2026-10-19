"""Cross-check against TSDuck's decoder. Skipped when `tstables` is not installed.

Verified independently on the dev sandbox with another SCTE-35 decoder (threefive), but
NOT yet run against TSDuck itself: if this fails on a machine with TSDuck, first check the
XML attribute formatting in the regexes below before suspecting the generated TS.
"""

import re
import shutil
import subprocess

import pytest

from nexencoder.inject import FRAME_RATES, SCENARIOS, build_ts, run_scenario

pytestmark = pytest.mark.skipif(shutil.which("tstables") is None,
                                reason="TSDuck (tstables) not installed")

PID = 0x1F0


def decode(tmp_path, scenario: str) -> str:
    events = run_scenario(SCENARIOS[scenario](), FRAME_RATES["29.97"])
    ts = tmp_path / f"{scenario}.ts"
    ts.write_bytes(build_ts(events, PID))
    proc = subprocess.run(["tstables", "--pid", str(PID), "--xml-output", "-", str(ts)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_basic_scenario_decodes_in_tsduck(tmp_path):
    xml = decode(tmp_path, "basic")
    assert xml.count("<splice_insert") == 2
    assert re.search(r'splice_event_id="(?:100|0x0*64)"', xml, re.I)
    assert re.search(r'duration="(?:2,?700,?000|0x0*2932e0)"', xml, re.I)


def test_cancel_scenario_decodes_in_tsduck(tmp_path):
    xml = decode(tmp_path, "cancel")
    assert re.search(r'splice_event_cancel="(?:true|1)"', xml, re.I)
