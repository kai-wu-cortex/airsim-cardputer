#!/usr/bin/env python3
"""Build a local Debian package; never uploads or touches the AirSIM project."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'dist'
DIST.mkdir(exist_ok=True)
os.umask(0o022)
with tempfile.TemporaryDirectory(prefix='airsim-cp-package-') as temporary:
    stage = Path(temporary)
    share = stage / 'usr/share/airsim-cardputer'
    share.mkdir(parents=True)
    shutil.copytree(ROOT / 'airsim_cp', share / 'airsim_cp', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    bin_dir = stage / 'usr/bin'; bin_dir.mkdir(parents=True)
    launcher = bin_dir / 'airsim-cardputer'
    launcher.write_text('#!/bin/sh\nset -eu\numask 077\nexport PYTHONPATH=/usr/share/airsim-cardputer\nexport PYTHONDONTWRITEBYTECODE=1\nexec /usr/bin/python3 -m airsim_cp.app "$@"\n')
    launcher.chmod(0o755)
    desktop = stage / 'usr/share/APPLaunch/applications'; desktop.mkdir(parents=True)
    (desktop / 'airsim-cardputer.desktop').write_text('''[Desktop Entry]
Type=Application
Name=AirSIM Cardputer
Comment=AirSIM phone and SMS client
Exec=/usr/bin/airsim-cardputer
Icon=share/images/airsim-cardputer.png
Terminal=false
Categories=Network;
''')
    icon = ROOT / 'assets/icon.png'
    if icon.exists():
        shutil.copy2(icon, share / 'icon.png')
        # APPLaunch resolves its own image paths and carousel size variants.
        images = stage / 'usr/share/APPLaunch/share/images'
        images.mkdir(parents=True)
        shutil.copy2(icon, images / 'airsim-cardputer.png')
        with Image.open(icon) as source:
            for size in (80, 100):
                source.convert('RGBA').resize((size, size), Image.Resampling.LANCZOS).save(images / f'airsim-cardputer_{size}.png')
    doc = stage / 'usr/share/doc/airsim-cardputer'; doc.mkdir(parents=True)
    shutil.copy2(ROOT / 'README.md', doc / 'README.md')
    shutil.copy2(ROOT / 'FEATURE-PARITY.md', doc / 'FEATURE-PARITY.md')
    for name in ('LICENSE','NOTICE'):
        shutil.copy2(ROOT / name, doc / name)
    control = stage / 'DEBIAN'; control.mkdir()
    (control / 'control').write_text('''Package: airsim-cardputer
Version: 0.3.13-1
Section: net
Priority: optional
Architecture: arm64
Maintainer: Kyle <kai-wu-cortex@users.noreply.github.com>
Depends: python3 (>= 3.11), python3-pil, python3-zeroconf, python3-cryptography, python3-evdev, python3-bleak (>= 0.22), pipewire-bin, libpipewire-0.3-modules, libspa-0.2-modules, wireplumber, fonts-noto-cjk
Description: Independent AirSIM local client for Cardputer Zero
 Authenticated pairing, calls, SMS and foreground PCM audio.
 Requires an Android phone running AirSIM. No cloud mode.
''')
    for directory, _, files in os.walk(stage):
        Path(directory).chmod(0o755)
        for name in files:
            path = Path(directory) / name
            path.chmod(0o755 if path == launcher else 0o644)
    output = DIST / 'airsim-cardputer_0.3.13-1_arm64.deb'
    subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(output)], check=True)
    print(output)
