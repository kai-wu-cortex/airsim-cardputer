"""Reuse the original AirSIM iOS icon; do not draw a different brand mark."""
from pathlib import Path
import shutil
from PIL import Image
assets=Path(__file__).resolve().parents[1]/'assets'
source=assets/'airsim-original.png'
with Image.open(source) as image:
    assert image.width==image.height and 128<=image.width<=512
shutil.copyfile(source,assets/'icon.png')
