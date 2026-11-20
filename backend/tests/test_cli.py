"""The CLI runs in a fresh process (no API routers imported), which once broke foreign-key resolution."""

import os
import subprocess
import sys


def test_cli_creates_and_seeds_a_business_in_a_fresh_process():
    env = {**os.environ}
    run = [sys.executable, "-m", "ordexa.cli"]
    r1 = subprocess.run(
        [*run, "create-tenant", "--code", "cli-test", "--name", "CLI Test", "--password", "cli-pass-123"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r1.returncode == 0, r1.stderr
    r2 = subprocess.run([*run, "seed-demo", "--code", "cli-test"], env=env, capture_output=True, text=True)
    assert r2.returncode == 0, r2.stderr
    assert "seeded 10 products and 5 customers" in r2.stdout
    r3 = subprocess.run(
        [*run, "create-tenant", "--code", "cli-test", "--name", "Dup", "--password", "cli-pass-123"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r3.returncode != 0 and "already exists" in r3.stderr
