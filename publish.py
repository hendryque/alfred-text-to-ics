#!/usr/bin/env python3
"""Build the .alfredworkflow bundle from HEAD and publish it as a GitHub release.

    ./publish.py --check    run the guards, build nothing
    ./publish.py 1.2.0      guards, then tag v1.2.0 and upload the bundle

The zip is built from the committed tree, not the working directory, so the
asset provably matches the tag. Standard library plus git and gh only.
"""

import io
import plistlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path


def run(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def die(msg):
    raise SystemExit(f"publish: {msg}")


def head_bytes(path):
    r = subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True)
    if r.returncode != 0:
        die(f"{path} is not in HEAD")
    return r.stdout


def guards():
    if run("git", "status", "--porcelain").stdout.strip():
        die("working tree is dirty; commit or stash first")

    files = run("git", "ls-tree", "--name-only", "HEAD", "workflow/").stdout.split()
    scripts = [f for f in files if f.endswith(".py")]
    expected = {"workflow/info.plist", "workflow/icon.png"} | set(scripts)
    if len(scripts) != 1 or set(files) != expected:
        die(f"workflow/ must hold exactly info.plist, icon.png and one script, found: {files}")
    script = scripts[0]

    if head_bytes(script) != head_bytes(script.replace("workflow/", "src/")):
        die(f"{script} differs from its src/ counterpart; sync them first")

    try:
        plist = plistlib.loads(head_bytes("workflow/info.plist"))
    except Exception as exc:
        die(f"info.plist does not parse: {exc}")

    version = plist.get("version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        die(f"info.plist version {version!r} is not X.Y.Z")

    for key, value in plist.get("variables", {}).items():
        if value:
            die(f"variable {key} has a baked-in value")
        if key.endswith(("KEY", "TOKEN")) and key not in plist.get("variablesdontexport", []):
            die(f"secret-looking variable {key} is not in variablesdontexport")

    # The READMEs promise the Command Line Tools Python (3.9) is enough.
    checker = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    r = run(checker, "-m", "py_compile", script)
    if r.returncode != 0:
        die(f"{script} does not compile under {checker}:\n{r.stderr}")

    name = plist["name"]
    bundle = "Alfred-" + name.replace(" ", "-") + ".alfredworkflow"
    return script, version, bundle


def main():
    if len(sys.argv) != 2:
        die(__doc__.strip())
    script, version, bundle = guards()
    print(f"guards OK: {bundle} {version}")
    if sys.argv[1] == "--check":
        return
    if sys.argv[1] != version:
        die(f"asked to publish {sys.argv[1]} but info.plist says {version}")

    if run("git", "rev-parse", "HEAD").stdout != run("git", "rev-parse", "origin/main").stdout:
        die("HEAD is not origin/main; push first so the tag lands on published history")
    tag = f"v{version}"
    if run("gh", "release", "view", tag).returncode == 0:
        die(f"release {tag} already exists")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in ("workflow/icon.png", f"workflow/{Path(script).name}", "workflow/info.plist"):
            z.writestr(Path(path).name, head_bytes(path))
    Path(bundle).write_bytes(buf.getvalue())
    print(f"built {bundle} ({len(buf.getvalue())} bytes) from HEAD")

    r = run("gh", "release", "create", tag, bundle, "--generate-notes")
    if r.returncode != 0:
        die(f"gh release create failed:\n{r.stderr}")
    print(r.stdout.strip())


if __name__ == "__main__":
    main()
